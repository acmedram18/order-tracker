"""Launches the coding assistant headlessly to investigate an incident.

The invocation is ``opencode run --auto --format json "<prompt>" -f <files>``.
Two details are load bearing and were verified against opencode 1.18.34:

* the prompt must come *before* ``-f``, otherwise the greedy array option
  swallows the prompt and treats it as a filename;
* ``--auto`` is required, because without it the run stalls on an approval
  prompt, which is useless in a headless context.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATUS_DISABLED = "disabled"
STATUS_SKIPPED = "skipped"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_NO_ANSWER = "no_answer"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_BUSY = "busy"

TEMPLATE = """You are investigating a production incident in the order-tracker service.

Alert: {alertname} (severity: {severity})
Affected endpoint: {endpoint}
Time window: {window_start} -> {window_end} ({window_seconds}s)
5xx responses in window: {errors}
Request rate in window: {rate}

Attached evidence:
- bundle.md  human-readable summary: alert, metrics, logs, traces, deep links
- logs.json  structured log lines from Loki
- traces.json OpenTelemetry traces from Tempo

The service source lives under app/ in the repository root ({repo}).
Metric definitions are in app/telemetry.py, routes in app/main.py.

Work in this order:
1. Read bundle.md first for the shape of the incident.
2. Identify the failing endpoint and the requests that failed.
3. Use logs.json and traces.json to find the erroring or slowest span.
4. Read only the code the evidence points at, and form a hypothesis that
   explains the 5xx.
5. State that hypothesis and the evidence supporting it in your response.

Budget: this is an automated run with a hard wall-clock limit. Stay under about
ten tool calls. Read bundle.md, then jump straight to the files the evidence
names. Do not tour the repository and do not run the full test suite; run one
targeted test only if you actually changed code.

Do not modify any code until you have stated a hypothesis backed by evidence.
Then make the smallest change that addresses the root cause.

If the evidence shows no fault, say so and stop. "No fault found" is a complete,
acceptable answer, and saying it early is better than searching longer.
"""


def build_prompt(settings, alert, evidence) -> str:
    metrics = getattr(evidence, "metrics", None) or {}
    return TEMPLATE.format(
        alertname=alert.alertname,
        severity=alert.severity,
        endpoint=alert.endpoint or "unknown",
        window_start=alert.starts_at.isoformat(),
        window_end=(alert.ends_at.isoformat() if alert.ends_at else "now"),
        window_seconds=alert.window_seconds,
        errors=metrics.get("errors_5xx", "unknown"),
        rate=metrics.get("request_rate", "unknown"),
        repo=settings.agent_dir,
    )


def build_command(settings, alert, evidence, files: dict) -> list:
    command = [
        settings.agent_command,
        "run",
        "--auto",
        "--format",
        "json",
    ]
    if settings.agent_model:
        command += ["--model", settings.agent_model]
    command += ["--dir", str(settings.agent_dir)]
    command += ["--title", f"incident {alert.alertname} {alert.fingerprint}"]
    command.append(build_prompt(settings, alert, evidence))
    for key in ("bundle", "logs", "traces"):
        path = files.get(key)
        if path:
            command += ["-f", str(path)]
    return command


def parse_events(output: str) -> dict:
    """Pull the session id, transcript, tool errors and cost out of ``--format json``."""
    session_id = None
    texts: list[str] = []
    tool_errors: list[str] = []
    cost = None
    last_text_at = None
    last_tool_at = None

    for index, line in enumerate((output or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or event.get("sessionID")
        part = event.get("part") or {}
        kind = event.get("type")

        if kind == "text" and part.get("text"):
            texts.append(part["text"])
            last_text_at = index
        elif kind == "tool_use":
            last_tool_at = index
            state = part.get("state") or {}
            if state.get("status") == "error" and state.get("error"):
                tool_errors.append(f"{part.get('tool', 'tool')}: {state['error']}")
        elif kind == "step_finish":
            value = part.get("cost")
            if isinstance(value, (int, float)):
                cost = value

    # The agent answered only if it spoke *after* its last tool call. Ending on a
    # tool call means it ran out of turns mid-thought and never concluded.
    has_answer = last_text_at is not None and (
        last_tool_at is None or last_text_at > last_tool_at
    )
    return {
        "session_id": session_id,
        "text": "\n".join(texts).strip(),
        "last_text": texts[-1].strip() if texts else "",
        "cost": cost,
        "has_answer": has_answer,
        "tool_errors": tool_errors,
    }


CONCLUSION_PROMPT = """Your investigation ended without a final answer.

Do not call any tools. Do not read any files. Using only what you already
established in this session, reply now with your conclusion, in plain text:

- affected endpoint
- the evidence you found
- your hypothesis for the cause
- any change you made to the code, and the test result

If you could not determine the cause, say so plainly and state what you ruled out.
Answer with the conclusion only."""


def build_conclusion_command(settings, session_id: str) -> list:
    """Continue the same session and demand a text answer, with no tools."""
    command = [settings.agent_command, "run", "--auto", "--format", "json"]
    if session_id:
        command += ["--session", session_id]
    command.append(CONCLUSION_PROMPT)
    return command


def execute(command: list, cwd: str, env: dict, timeout: float):
    """Run the command, returning partial output even when it has to be killed.

    ``subprocess.run`` raises on timeout without attaching anything it captured on
    POSIX, which would throw away exactly the output needed to explain the timeout.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr, True


class AgentRunner:
    """Runs the assistant with a bounded number of concurrent invocations."""

    def __init__(self, settings):
        self.settings = settings
        self._semaphore = threading.Semaphore(max(1, settings.agent_max_concurrent))
        self._lock = threading.Lock()
        self._running: set = set()

    def is_running(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._running

    def run(self, alert, evidence, files: dict, store, incident_id: str) -> dict:
        if not self.settings.agent_enabled:
            store.set_agent_result(incident_id, STATUS_DISABLED)
            return {"status": STATUS_DISABLED}

        if shutil.which(self.settings.agent_command) is None:
            logger.warning("agent command not found: %s", self.settings.agent_command)
            store.set_agent_result(incident_id, STATUS_SKIPPED)
            return {"status": STATUS_SKIPPED, "reason": "command not found"}

        if not self._semaphore.acquire(blocking=False):
            store.set_agent_result(incident_id, STATUS_BUSY)
            return {"status": STATUS_BUSY, "reason": "another investigation is running"}

        with self._lock:
            self._running.add(alert.fingerprint)
        store.set_agent_result(incident_id, STATUS_RUNNING)
        started = time.monotonic()
        try:
            command = build_command(self.settings, alert, evidence, files)
            env = dict(os.environ)
            env.setdefault("NO_COLOR", "1")
            returncode, stdout, stderr, timed_out = execute(
                command,
                cwd=str(self.settings.agent_dir),
                env=env,
                timeout=self.settings.agent_timeout,
            )
            parsed = parse_events(stdout)
            log_path = (
                Path(files["bundle"]).parent / "agent.log" if files.get("bundle") else None
            )
            if log_path:
                log_path.write_text(
                    "$ "
                    + " ".join(command)
                    + f"\n\n--- stdout (timed_out={timed_out}) ---\n"
                    + stdout
                    + ("\n--- stderr ---\n" + stderr if stderr else ""),
                    encoding="utf-8",
                )

            if timed_out:
                store.set_agent_result(
                    incident_id,
                    STATUS_TIMEOUT,
                    parsed["session_id"],
                    returncode,
                    summary=parsed["text"][-2000:] or None,
                    error=f"exceeded {self.settings.agent_timeout}s",
                )
                return {
                    "status": STATUS_TIMEOUT,
                    "duration_s": round(time.monotonic() - started, 2),
                    "session_id": parsed["session_id"],
                    "cost": parsed["cost"],
                    "reason": f"exceeded {self.settings.agent_timeout}s",
                    "output": parsed["text"][-2000:],
                }

            summary = parsed["last_text"]
            session_id = parsed["session_id"]
            cost = parsed["cost"]
            tool_errors = parsed["tool_errors"]
            answer_passed = parsed["has_answer"]

            # Exit code 0 is not the same as an answer. When the run ends on a tool
            # call, the model stopped mid-thought, so ask it to state a conclusion
            # in the same session with tools off the table.
            if returncode == 0 and not answer_passed:
                logger.info(
                    "agent produced no answer, requesting conclusion: incident=%s session=%s tool_errors=%d",
                    incident_id,
                    session_id,
                    len(tool_errors),
                )
                conclusion = build_conclusion_command(self.settings, session_id)
                code2, out2, err2, timed_out2 = execute(
                    conclusion,
                    cwd=str(self.settings.agent_dir),
                    env=env,
                    timeout=min(self.settings.agent_timeout, self.settings.agent_conclusion_timeout),
                )
                if log_path:
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            "\n$ "
                            + " ".join(conclusion)
                            + f"\n\n--- stdout (timed_out={timed_out2}) ---\n"
                            + out2
                            + ("\n--- stderr ---\n" + err2 if err2 else "")
                        )
                follow_up = parse_events(out2)
                if follow_up["last_text"]:
                    summary = follow_up["last_text"]
                    session_id = follow_up["session_id"] or session_id
                    cost = (follow_up["cost"] or 0) + (cost or 0)
                    answer_passed = True
                if tool_errors:
                    logger.warning(
                        "agent tool errors on incident %s: %s", incident_id, tool_errors[:5]
                    )

            if returncode != 0:
                status = STATUS_FAILED
                error = (stderr or f"exit code {returncode}")[-2000:]
            elif answer_passed:
                status = STATUS_FINISHED
                error = None
            else:
                status = STATUS_NO_ANSWER
                error = "; ".join(tool_errors) or "agent stopped without a final answer"

            store.set_agent_result(
                incident_id,
                status,
                session_id,
                returncode,
                summary=summary or None,
                error=error,
            )
            return {
                "status": status,
                "exit_code": returncode,
                "session_id": session_id,
                "cost": cost,
                "duration_s": round(time.monotonic() - started, 2),
                "has_answer": answer_passed,
                "tool_errors": tool_errors,
                "error": error,
                "output": summary,
                "stderr": stderr[-2000:],
            }
        except Exception as exc:  # noqa: BLE001 - never break the webhook path
            logger.exception("agent launch failed")
            store.set_agent_result(incident_id, STATUS_FAILED, error=str(exc)[:2000])
            return {"status": STATUS_FAILED, "error": str(exc)}
        finally:
            with self._lock:
                self._running.discard(alert.fingerprint)
            self._semaphore.release()
