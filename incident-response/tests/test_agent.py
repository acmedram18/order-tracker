import json
import subprocess

import pytest

from incident_response import agent
from incident_response.agent import (
    CONCLUSION_PROMPT,
    AgentRunner,
    build_command,
    build_conclusion_command,
    build_prompt,
    parse_events,
)
from incident_response.evidence import Evidence

from conftest import NOW, grafana_alert, grafana_payload


def context(**kwargs):
    from incident_response.payload import parse_webhook

    return parse_webhook(json.dumps(grafana_payload(grafana_alert(**kwargs))).encode(), NOW, 900)[0]


def files_in(directory):
    """Mirror what store.write_bundle returns: real Paths inside one directory."""
    return {
        "bundle": directory / "bundle.md",
        "logs": directory / "logs.json",
        "traces": directory / "traces.json",
    }


def test_prompt_names_endpoint_window_and_metrics(tmp_path):
    from incident_response.config import Settings
    from incident_response.evidence import Evidence

    settings = Settings(agent_enabled=True, agent_dir=tmp_path)
    found = Evidence(metrics={"errors_5xx": "5", "request_rate": "0.4"})
    prompt = build_prompt(settings, context(), found)
    assert "/api/orders/{order_id}" in prompt
    assert "5xx responses in window: 5" in prompt
    assert "Request rate in window: 0.4" in prompt
    assert "Do not modify any code until" in prompt
    assert "ten tool calls" in prompt
    assert "No fault found" in prompt
    assert str(tmp_path) in prompt


def test_command_places_prompt_before_file_flags(tmp_path):
    """`-f` is greedy: putting the prompt after it makes opencode read it as a path."""
    from incident_response.config import Settings
    from incident_response.evidence import Evidence

    command = build_command(
        Settings(agent_enabled=True, agent_dir=tmp_path), context(), Evidence(), files_in(tmp_path)
    )
    assert command[:3] == ["opencode", "run", "--auto"]
    assert "--format" in command and "json" in command

    prompt_index = next(
        index for index, item in enumerate(command) if item.startswith("You are investigating")
    )
    first_flag_index = command.index("-f")
    assert prompt_index < first_flag_index, "prompt must precede -f"

    assert command.count("-f") == 3
    assert command[command.index("--dir") + 1] == str(tmp_path)


def test_command_includes_model_when_configured(tmp_path):
    from incident_response.config import Settings
    from incident_response.evidence import Evidence

    command = build_command(
        Settings(agent_enabled=True, agent_dir=tmp_path, agent_model="opencode/some-model"),
        context(),
        Evidence(),
        files_in(tmp_path),
    )
    assert command[command.index("--model") + 1] == "opencode/some-model"


def test_parse_events_extracts_session_text_and_cost():
    output = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_1"}),
            json.dumps({"type": "text", "sessionID": "ses_1", "part": {"text": "found it"}}),
            json.dumps({"type": "step_finish", "sessionID": "ses_1", "part": {"cost": 0.42}}),
            "not json at all",
        ]
    )
    parsed = parse_events(output)
    assert parsed["session_id"] == "ses_1"
    assert parsed["text"] == "found it"
    assert parsed["cost"] == 0.42
    assert parsed["has_answer"] is True


def test_last_text_is_the_conclusion_not_the_whole_transcript():
    output = "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": "let me look around"}}),
            json.dumps({"type": "text", "part": {"text": "## Hypothesis\nno bug here"}}),
        ]
    )
    parsed = parse_events(output)
    assert parsed["has_answer"] is True
    assert parsed["last_text"] == "## Hypothesis\nno bug here"
    assert "let me look around" in parsed["text"]


def test_text_before_a_tool_call_is_not_an_answer():
    output = "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": "let me check"}}),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "tool": "bash",
                        "state": {"status": "error", "error": "SchemaError(Missing key)"},
                    },
                }
            ),
            json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        ]
    )
    parsed = parse_events(output)
    assert parsed["has_answer"] is False
    assert parsed["tool_errors"] == ["bash: SchemaError(Missing key)"]


def test_conclusion_command_continues_the_session():
    from incident_response.config import Settings

    command = build_conclusion_command(Settings(), "ses_abc")
    assert command[:3] == ["opencode", "run", "--auto"]
    assert command[command.index("--session") + 1] == "ses_abc"
    assert command[-1] == CONCLUSION_PROMPT
    assert "-f" not in command


def test_runner_skips_when_disabled(tmp_path, store):
    from incident_response.config import Settings

    runner = AgentRunner(Settings(agent_enabled=False, agent_dir=tmp_path))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])
    assert result["status"] == "disabled"
    assert store.get_incident(incident["id"])["agent_status"] == "disabled"


def test_alerts_without_an_endpoint_still_launch_the_agent(tmp_path, store, monkeypatch):
    """Launching on every firing alert is the point of the service, endpoint or not."""
    from incident_response.config import Settings

    launched = []
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    answer = json.dumps({"type": "text", "part": {"text": "nothing is actually broken"}})
    monkeypatch.setattr(
        agent,
        "execute",
        lambda command, cwd, env, timeout: launched.append(command) or (0, answer, "", False),
    )
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path))
    endpointless = context(handler=None, fingerprint="notest")
    assert endpointless.endpoint is None
    _, incident = store.open_incident(endpointless, "2026-10-04T03:10:00+00:00")
    result = runner.run(endpointless, Evidence(), files_in(tmp_path), store, incident["id"])

    assert result["status"] == "finished"
    assert len(launched) == 1
    assert store.get_incident(incident["id"])["agent_status"] == "finished"


def test_runner_retries_for_a_conclusion_when_no_answer(tmp_path, store, monkeypatch):
    """Exit code 0 is not an answer: ending on a tool call must trigger a retry."""
    from incident_response.config import Settings

    silent = "\n".join(
        [
            json.dumps({"type": "text", "sessionID": "ses_x", "part": {"text": "let me check"}}),
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "ses_x",
                    "part": {
                        "tool": "bash",
                        "state": {"status": "error", "error": "SchemaError(Missing key)"},
                    },
                }
            ),
            json.dumps({"type": "step_finish", "sessionID": "ses_x", "part": {"reason": "stop"}}),
        ]
    )
    answer = json.dumps(
        {"type": "text", "sessionID": "ses_x", "part": {"text": "Conclusion: no fault found."}}
    )
    calls = []

    def fake_execute(command, cwd, env, timeout):
        calls.append(command)
        return (0, silent, "", False) if len(calls) == 1 else (0, answer, "", False)

    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    monkeypatch.setattr(agent, "execute", fake_execute)
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])

    assert len(calls) == 2, "should have asked for a conclusion"
    assert calls[1][calls[1].index("--session") + 1] == "ses_x"
    assert result["status"] == "finished"
    assert result["has_answer"] is True
    assert result["output"] == "Conclusion: no fault found."

    row = store.get_incident(incident["id"])
    assert row["agent_summary"] == "Conclusion: no fault found."
    # both passes are in the log
    assert "let me check" in (tmp_path / "agent.log").read_text()
    assert "Conclusion" in (tmp_path / "agent.log").read_text()


def test_runner_reports_no_answer_when_the_retry_is_also_silent(tmp_path, store, monkeypatch):
    from incident_response.config import Settings

    silent = json.dumps(
        {
            "type": "tool_use",
            "part": {"tool": "bash", "state": {"status": "error", "error": "SchemaError"}},
        }
    )
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    monkeypatch.setattr(agent, "execute", lambda *a, **k: (0, silent, "", False))
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    (tmp_path / "bundle.md").write_text("# bundle", encoding="utf-8")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])

    assert result["status"] == "no_answer"
    row = store.get_incident(incident["id"])
    assert row["agent_status"] == "no_answer"
    assert "SchemaError" in row["agent_error"]


def test_runner_skips_when_binary_missing(tmp_path, store, monkeypatch):
    from incident_response.config import Settings

    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])
    assert result["status"] == "skipped"


def test_runner_parses_successful_run(tmp_path, store, monkeypatch):
    from incident_response.config import Settings

    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    stdout = "\n".join(
        [
            json.dumps({"type": "text", "sessionID": "ses_x", "part": {"text": "root cause found"}}),
            json.dumps(
                {"type": "step_finish", "sessionID": "ses_x", "part": {"cost": 0.1, "reason": "stop"}}
            ),
        ]
    )
    captured = {}

    def fake_execute(command, cwd, env, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        return 0, stdout, "", False

    monkeypatch.setattr(agent, "execute", fake_execute)
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])

    assert result["status"] == "finished"
    assert result["session_id"] == "ses_x"
    assert captured["timeout"] == 300.0
    assert store.get_incident(incident["id"])["agent_status"] == "finished"


def test_runner_records_failure_exit_code(tmp_path, store, monkeypatch):
    from incident_response.config import Settings

    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    monkeypatch.setattr(agent, "execute", lambda command, cwd, env, timeout: (2, "", "boom", False))
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])
    assert result["status"] == "failed"
    assert store.get_incident(incident["id"])["agent_exit_code"] == 2


def test_runner_handles_timeout(tmp_path, store, monkeypatch):
    from incident_response.config import Settings

    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    monkeypatch.setattr(agent, "execute", lambda command, cwd, env, timeout: (-9, "partial", "", True))
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path, agent_timeout=1.0))
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    (tmp_path / "bundle.md").write_text("# bundle", encoding="utf-8")
    result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])
    assert result["status"] == "timeout"
    assert store.get_incident(incident["id"])["agent_status"] == "timeout"
    # the whole point of the log is to explain the timeout
    log = (tmp_path / "agent.log").read_text()
    assert "partial" in log
    assert "timed_out=True" in log


def test_second_concurrent_run_is_rejected(tmp_path, store, monkeypatch):
    from incident_response.config import Settings

    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/opencode")
    runner = AgentRunner(Settings(agent_enabled=True, agent_dir=tmp_path, agent_max_concurrent=1))

    # occupy the single slot
    assert runner._semaphore.acquire(blocking=False)
    try:
        _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
        result = runner.run(context(), Evidence(), files_in(tmp_path), store, incident["id"])
        assert result["status"] == "busy"
    finally:
        runner._semaphore.release()
