# Incident Responder

Receives Grafana webhook alerts at `POST /alerts` on port 8001.

## What it does

1. **Collects context** – queries Loki for recent logs and Prometheus for 5xx metrics for the affected endpoint
2. **Saves a structured incident report** to `incidents/<timestamp>.json`
3. **Launches the coding assistant** (`antigravity-ide chat --mode agent`) in headless mode with the full incident context
4. **Saves the agent response** to `incidents/<timestamp>_response.txt`

## Setup

```bash
pip install fastapi uvicorn httpx
```

## Run

```bash
cd incident-response
python -m uvicorn main:app --port 8001
```

## Environment variables

| Variable        | Default                        | Description                     |
|-----------------|--------------------------------|---------------------------------|
| `LOKI_URL`      | `http://localhost:3100`        | Loki base URL                   |
| `PROMETHEUS_URL`| `http://localhost:9090`        | Prometheus base URL             |
| `GRAFANA_URL`   | `http://localhost:3000`        | Grafana base URL (for links)    |
| `INCIDENTS_DIR` | `incidents/`                   | Where reports are saved         |
| `WORKSPACE_DIR` | parent directory of this file  | Repo root passed to the agent   |
| `AGY_CMD`       | `antigravity-ide.cmd`          | Path to the coding assistant CLI|

## Alert webhook payload (Grafana format)

```json
{
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "5xx Responses Detected",
        "http_route": "/api/orders/{order_id}"
      },
      "annotations": {
        "summary": "5xx errors on /api/orders/{order_id}",
        "description": "..."
      }
    }
  ]
}
```
