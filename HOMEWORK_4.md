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
| Incident Responder *(run locally — see Q5)* | <http://localhost:8001> | Receives Grafana webhooks, runs AI agent |

---

## Fresh Reset — Test with Clean Data Every Time

To wipe all stored data (database, metrics, logs, traces) and start from scratch:

```bash
# Stop everything and delete all volumes
docker compose down -v

# Rebuild images and start the full stack fresh
docker compose up --build -d --wait
```

> **Why `-v`?** The `orders` volume holds the SQLite database. Deleting it forces the app
> to recreate the three seed orders (`standard-1001`, `express-1002`, `standard-1003`)
> on next startup — so every test run starts from the same known state.

**One-liner reset + health check:**

```bash
docker compose down -v && docker compose up --build -d --wait && curl http://localhost:8000/healthz
```

After a fresh reset, re-run each question's `curl` command in order. The responder (Q5/Q6)
must also be restarted manually if it was running:

```bash
# Stop the old responder (Ctrl+C), then restart it:
cd incident-response
uvicorn main:app --port 8001
```

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

The responder is an API built in [`incident-response/`](incident-response/). Because it needs to launch the `agy` (Antigravity IDE) CLI, it must be run **locally on your host machine** (not inside Docker).

**1. Start the responder (in a new terminal):**

```bash
cd incident-response
pip install -r requirements.txt  # Or use: uv run uvicorn main:app --port 8001
uvicorn main:app --port 8001
```

When it receives a `POST /alerts` webhook from Grafana it:

1. Queries **Loki** for recent logs on the affected endpoint
2. Queries **Prometheus** for the 5xx count over the last 15 minutes
3. Saves a JSON incident report to `incident-response/incidents/<timestamp>.json`
4. Runs **two-phase auto-remediation**:
   - **Phase 1**: Launches the `agy` IDE agent (opens a chat window for visibility)
   - **Phase 2**: Directly scans and patches `app/main.py` for the known bug

**2. Test it manually** with a fake firing alert (run in a separate terminal):

```bash
curl -X POST http://localhost:8001/alerts \
  -H "Content-Type: application/json" \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification"}}]}'
```

**Answer (Check the responder terminal and the response file):**
You will see `[responder] launching IDE agent: ...` and a response file created at
`incident-response/incidents/<timestamp>_response.txt` ending with:

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
webhook to the responder at `http://host.docker.internal:8001/alerts` (configured in
[`observability/grafana/provisioning/alerting/contactpoints.yaml`](observability/grafana/provisioning/alerting/contactpoints.yaml)
and routed in
[`observability/grafana/provisioning/alerting/policies.yaml`](observability/grafana/provisioning/alerting/policies.yaml)).

Watch the responder terminal — you will see:

```
[responder] incident saved -> incidents\<timestamp>.json
[responder] launching IDE agent: antigravity-ide.cmd chat --mode agent ...
[responder] remediation result:
[agent] IDE agent launched – check the Antigravity IDE window for details.
[patch] FIXED app/main.py:
  - replaced: placed_at.replace(day=placed_at.day + 2)
  + with:     placed_at + timedelta(days=2)

CONCLUSION: Auto-patched app/main.py. Run 'docker compose up --build -d --wait' to deploy the fix.
```

The full response is also saved at `incident-response/incidents/<timestamp>_response.txt`.

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
├── compose.yaml             # Full stack: app + otelcol + prometheus + loki + tempo + grafana
├── Dockerfile               # Builds the app image
└── pyproject.toml           # App Python dependencies
```
