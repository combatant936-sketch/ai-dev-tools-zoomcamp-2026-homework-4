import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import telemetry

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

# ---------------------------------------------------------------------------
# Metric instruments – created after configure() so the MeterProvider is set.
# ---------------------------------------------------------------------------
_meter = None
_request_counter = None
_order_lookup_duration = None


def _init_metrics() -> None:
    global _meter, _request_counter, _order_lookup_duration
    _meter = telemetry.get_meter()

    # Counts every HTTP request; dimensions: http.route + http.status_code
    _request_counter = _meter.create_counter(
        name="http.server.request.count",
        description="Total number of HTTP requests",
        unit="1",
    )

    # Histogram for order-lookup latency (GET /api/orders and GET /api/orders/{id})
    _order_lookup_duration = _meter.create_histogram(
        name="order.lookup.duration",
        description="Duration of order lookup operations",
        unit="ms",
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

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


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # OTel must be configured before anything else (including metrics init)
    telemetry.configure()
    _init_metrics()
    telemetry.instrument_app(_app)
    
    # Uvicorn resets logger levels on startup. Set it here so it persists!
    logger.setLevel(logging.INFO)
    
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: record http.server.request.count for every response
# ---------------------------------------------------------------------------

@app.middleware("http")
async def record_request_metric(request: Request, call_next):
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        status_code = 500
        raise
    finally:
        # Determine the matched route template (e.g. /api/orders/{order_id})
        route = request.scope.get("route")
        route_path = route.path if route else request.url.path

        if _request_counter is not None:
            _request_counter.add(
                1,
                {
                    "http.route": route_path,
                    "http.status_code": str(status_code),
                    "http.method": request.method,
                },
            )

    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("list_orders") as span:
        start = datetime.now(timezone.utc)
        with connect() as db:
            rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        result = [as_dict(row) for row in rows]
        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

        span.set_attribute("db.row_count", len(result))
        if _order_lookup_duration is not None:
            _order_lookup_duration.record(elapsed_ms, {"operation": "list_orders"})

        logger.info("list_orders returned %d orders", len(result))
        return result


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("get_order") as span:
        span.set_attribute("order.id", order_id)
        start = datetime.now(timezone.utc)
        with connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

        if row is None:
            span.set_attribute("order.found", False)
            logger.warning("get_order: order_id=%s not found", order_id)
            if _order_lookup_duration is not None:
                _order_lookup_duration.record(elapsed_ms, {"operation": "get_order", "found": "false"})
            raise HTTPException(404, "Order not found")

        span.set_attribute("order.found", True)
        span.set_attribute("order.status", row["status"])
        span.set_attribute("order.priority", row["priority"])
        if _order_lookup_duration is not None:
            _order_lookup_duration.record(elapsed_ms, {"operation": "get_order", "found": "true"})

        logger.info("get_order: order_id=%s status=%s", order_id, row["status"])
        return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("create_order") as span:
        if order.priority not in {"standard", "express"}:
            raise HTTPException(422, "Priority must be standard or express")
        order_id = str(uuid4())
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.priority", order.priority)
        with connect() as db:
            db.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, order.customer, order.item, order.priority, "received",
                 datetime.now(timezone.utc).isoformat()),
            )
        logger.info("create_order: order_id=%s customer=%s priority=%s", order_id, order.customer, order.priority)
        return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("update_status") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.new_status", update.status)
        if update.status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        with connect() as db:
            cursor = db.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (update.status, order_id),
            )
        if cursor.rowcount == 0:
            logger.warning("update_status: order_id=%s not found", order_id)
            raise HTTPException(404, "Order not found")
        logger.info("update_status: order_id=%s -> %s", order_id, update.status)
        return get_order(order_id)
