# Homework 4 — AI Dev Tools Zoomcamp 2026

Observability, alerting, and automatic incident response for the Order Tracker service.

---

## Prerequisites

| Tool | Notes |
|------|-------|
| Docker + Compose | Any recent version with the `compose` plugin |
| `curl` | For manual testing |

---

## Start the Full Stack

```bash
docker compose up --build -d --wait
```

All services that come up:

| Service | URL | Purpose |
|---------|-----|---------|
| Order Tracker | <http://localhost:8000> | The app being monitored |
| Grafana | <http://localhost:3000> | Dashboards + alerts |
| Prometheus | <http://localhost:9090> | Metrics storage |
| Loki | <http://localhost:3100> | Log storage |
| Tempo | <http://localhost:3200> | Trace storage |
| OTel Collector | gRPC `4317` / HTTP `4318` | Receives all telemetry from the app |
| Incident Responder | <http://localhost:8001> | Receives Grafana webhooks, runs AI agent |

---

## Question 1 — Run the app

Start the stack and hit the health check:

```bash
docker compose up --build -d --wait
curl http://localhost:8000/healthz
```

**Answer:** `{"status":"ok"}`

---

## Question 2 — Instrument one endpoint

The app's [`app/telemetry.py`](app/telemetry.py) sets up OpenTelemetry for all three signals
(metrics, logs, traces). A middleware in [`app/main.py`](app/main.py) records
`http.server.request.count` with `http.route` and `http.status_code` dimensions on every
response.

Rebuild and call an order that exists:

```bash
docker compose up --build -d --wait
curl -i http://localhost:8000/api/orders/standard-1001
```

Find the request metric in the app logs:

```bash
docker compose logs app | grep -i "status_code\|StatusCode\|http.server"
```

**Answer:** `200` — `standard-1001` exists, the lookup succeeds.

---

## Question 3 — Build the telemetry pipeline

The observability stack is wired in [`compose.yaml`](compose.yaml) and configured under
[`observability/`](observability/):

```
observability/
├── otelcol/config.yaml        # Collector fans out: metrics→Prometheus, logs→Loki, traces→Tempo
├── prometheus/prometheus.yaml # Scrapes the Collector's /metrics endpoint
├── loki/loki.yaml             # Log aggregation
├── tempo/tempo.yaml           # Distributed tracing backend
└── grafana/
    ├── dashboards/order-tracker.json   # Pre-built dashboard (request counts + errors)
    └── provisioning/                   # Auto-wired datasources, dashboards, alerts
```

Rebuild, then call an order that does **not** exist:

```bash
docker compose up --build -d --wait
curl -i http://localhost:8000/api/orders/standard-1002
```

Open Grafana and verify the metric, log, and trace all appear:

1. **Dashboard** → <http://localhost:3000/d/order-tracker-overview>  
   Find the request count panel — it will show a `404` for `/api/orders/{order_id}`.

2. **Logs** → Grafana Explore → Datasource: Loki  
   Query: `{service_name="order-tracker"}`

3. **Traces** → Grafana Explore → Datasource: Tempo  
   Search by `service.name = order-tracker`

**Answer:** `404`

---

## Question 4 — Configure the alert

The alert is automatically provisioned from
[`observability/grafana/provisioning/alerting/rules.yaml`](observability/grafana/provisioning/alerting/rules.yaml).

**What it watches:**

```promql
sum by (http_route) (
  increase(
    order_tracker_http_server_request_count_total{http_status_code=~"5.."}[5m]
  )
)
or vector(0)
```

The `or vector(0)` ensures the alert is **Normal** (not "No data") when there are no 5xx
responses. The annotation includes the affected endpoint, the 5-minute time window, and a
direct link to the Grafana dashboard.

Run the same call as Q3 (it's a 404, not a 5xx):

```bash
curl -i http://localhost:8000/api/orders/standard-1002
```

Check alert state in Grafana → **Alerting → Alert rules** → "5xx Responses Detected".

**Answer:** `Normal`

---

## Question 5 — Build the automatic responder

The responder is the `responder` service in [`compose.yaml`](compose.yaml), built from
[`incident-response/`](incident-response/). It starts automatically with the stack.

When it receives a `POST /alerts` webhook from Grafana it:

1. Queries **Loki** for recent logs on the affected endpoint
2. Queries **Prometheus** for the 5xx count over the last 15 minutes
3. Saves a JSON incident report to `incident-response/incidents/<timestamp>.json`
4. Launches the `agy` coding assistant in headless mode with the full incident context

**Test it manually** with a fake firing alert:

```bash
curl -X POST http://localhost:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{
    "alerts":[{
      "status":"firing",
      "labels":{"alertname":"ResponderTest","test":"true"},
      "annotations":{"summary":"Test notification; no incident to fix"}
    }]
  }'
```

Watch the agent run:

```bash
docker compose logs responder -f
```

**Answer (last line):**

```
CONCLUSION: No action required as this is a test notification.
```

---

## Question 6 — Watch the agent fix the incident

### What makes this request problematic

`express-1002` was created on the last day of the previous month. The `order_detail()`
function computes an estimated delivery date using `replace(day=placed_at.day + 2)`.  
When `placed_at.day + 2` exceeds the number of days in that month (e.g. Oct 31 → day 33),
Python raises `ValueError: day is out of range for month`, causing an **HTTP 500**.

### Step 1 — Trigger the error

```bash
curl -i http://localhost:8000/api/orders/express-1002
```

You should see `HTTP/1.1 500 Internal Server Error`.  
If the alert does not fire on the first try, repeat the request a few times within 5 minutes.

### Step 2 — Wait for Grafana to fire the alert

The alert evaluates every **1 minute**. Once it detects 5xx responses it fires and sends a
webhook to the responder at `http://responder:8001/alerts` (configured in
[`observability/grafana/provisioning/alerting/contactpoints.yaml`](observability/grafana/provisioning/alerting/contactpoints.yaml)
and routed in
[`observability/grafana/provisioning/alerting/policies.yaml`](observability/grafana/provisioning/alerting/policies.yaml)).

Watch the responder receive it and the agent start:

```bash
docker compose logs responder -f
```

### Step 3 — Verify the fix

After the agent patches `order_detail()` to use `timedelta(days=2)` instead of `replace()`,
rebuild and test:

```bash
docker compose up --build -d --wait
curl -i http://localhost:8000/api/orders/express-1002
```

The response should now be `HTTP/1.1 200 OK` with a valid `estimated_delivery` date.

**Answer:** The express delivery date calculation tried to use a day that does not exist in that month.

---

## Stopping the Stack

```bash
docker compose down        # keep data volumes
docker compose down -v     # also delete all data
```

---

## File Map

```
order-tracker/
├── app/
│   ├── main.py              # FastAPI app + OTel middleware + all routes
│   └── telemetry.py         # OTel setup (traces, metrics, logs → OTLP or console)
├── incident-response/
│   ├── Dockerfile           # Builds the responder image
│   ├── requirements.txt     # fastapi, uvicorn, httpx
│   ├── main.py              # Webhook receiver + Loki/Prom queries + agy launcher
│   └── incidents/           # JSON incident reports + agent responses (volume-mounted)
├── observability/
│   ├── otelcol/config.yaml
│   ├── prometheus/prometheus.yaml
│   ├── loki/loki.yaml
│   ├── tempo/tempo.yaml
│   └── grafana/
│       ├── dashboards/order-tracker.json
│       └── provisioning/
│           ├── datasources/datasources.yaml
│           ├── dashboards/dashboards.yaml
│           └── alerting/
│               ├── rules.yaml          # 5xx alert rule
│               ├── contactpoints.yaml  # email + webhook to responder
│               └── policies.yaml       # routes 5xx alerts to webhook
├── compose.yaml             # Full stack: app + otelcol + prometheus + loki + tempo + grafana + responder
├── Dockerfile               # Builds the app image
└── pyproject.toml           # App Python dependencies
```
