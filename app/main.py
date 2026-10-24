import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from opentelemetry import trace
from pydantic import BaseModel, Field

from app.telemetry import (
    configure_logging,
    configure_metrics,
    configure_otel_logs,
    configure_otel_metrics,
    configure_tracing,
    logger,
    order_status_changes_total,
    orders_created_total,
    record_lookup,
    set_express_orders_overdue,
    shutdown_telemetry,
    TRACER_NAME,
)


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}
EXPRESS_DELIVERY_DAYS = 2
LOOKUP_ROUTE = "/api/orders/{order_id}"

tracer = trace.get_tracer(TRACER_NAME)


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def estimated_delivery(order):
    placed_at = datetime.fromisoformat(order["created_at"])
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=timezone.utc)
    return placed_at + timedelta(days=EXPRESS_DELIVERY_DAYS)


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        order["estimated_delivery"] = estimated_delivery(order).date().isoformat()
    return order


def count_orders():
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def count_overdue_express():
    now = datetime.now(timezone.utc)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM orders WHERE priority = 'express' AND status != 'delivered'"
        ).fetchall()
    overdue = 0
    for row in rows:
        try:
            estimate = estimated_delivery(dict(row))
        except ValueError:
            continue
        if now > estimate:
            overdue += 1
    return overdue


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    set_express_orders_overdue(count_overdue_express())
    logger.info("order_tracker_started", orders=count_orders(), db_path=str(DB_PATH))
    try:
        yield
    finally:
        await shutdown_telemetry()


app = FastAPI(title="Order Tracker", lifespan=lifespan)

configure_logging()
configure_otel_logs()
configure_tracing(app)
configure_metrics(app)
configure_otel_metrics()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    logger.debug("orders_listed", count=len(rows))
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, request: Request = None):
    route = LOOKUP_ROUTE
    if request is not None:
        route = getattr(request.scope.get("route"), "path", None) or request.url.path
    started = time.perf_counter()
    with tracer.start_as_current_span("order.lookup") as span:
        with connect() as db:
            row = db.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
        if row is None:
            status_code = 404
        else:
            status_code = 200
        span.set_attribute("http.route", route)
        span.set_attribute("http.status_code", status_code)
        span.set_attribute("order.id", order_id)
        duration = time.perf_counter() - started
        record_lookup(route, status_code, duration)
        if row is None:
            logger.warning(
                "order_not_found",
                order_id=order_id,
                route=route,
                http_status_code=status_code,
                method="get",
            )
            raise HTTPException(404, "Order not found")
        logger.info(
            "order_lookup",
            order_id=order_id,
            route=route,
            http_status_code=status_code,
            duration_seconds=round(duration, 6),
        )
        return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    orders_created_total.labels(priority=order.priority).inc()
    set_express_orders_overdue(count_overdue_express())
    logger.info("order_created", order_id=order_id, priority=order.priority)
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        logger.warning("order_not_found", order_id=order_id, method="patch")
        raise HTTPException(404, "Order not found")
    order_status_changes_total.labels(status=update.status).inc()
    set_express_orders_overdue(count_overdue_express())
    logger.info("order_status_changed", order_id=order_id, status=update.status)
    return get_order(order_id)
