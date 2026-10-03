"""
incident-response/main.py

Receives Grafana webhook alerts at POST /alerts (port 8001).
For each firing alert it:
  1. Pulls recent logs from Loki for the affected endpoint.
  2. Pulls recent traces (span-metrics) from Prometheus.
  3. Writes a structured incident report to incidents/<timestamp>.json.
  4. Launches the coding assistant (antigravity-ide chat) in headless/agent
     mode with the full incident context so it can investigate and respond.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LOKI_URL        = os.getenv("LOKI_URL",        "http://localhost:3100")
PROMETHEUS_URL  = os.getenv("PROMETHEUS_URL",  "http://localhost:9090")
GRAFANA_URL     = os.getenv("GRAFANA_URL",     "http://localhost:3000")
INCIDENTS_DIR   = Path(os.getenv("INCIDENTS_DIR", "incidents"))
WORKSPACE       = os.getenv("WORKSPACE_DIR",   str(Path(__file__).parent.parent))
AGY_CMD         = os.getenv("AGY_CMD",         "antigravity-ide.cmd")

INCIDENTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Incident Responder", version="1.0.0")

# ---------------------------------------------------------------------------
# Helpers – data gathering
# ---------------------------------------------------------------------------

def _loki_logs(endpoint: str, minutes: int = 15) -> list[str]:
    """Fetch recent log lines from Loki for the given http_route."""
    now_ns  = int(datetime.now(timezone.utc).timestamp() * 1e9)
    ago_ns  = now_ns - minutes * 60 * int(1e9)
    query   = f'{{service_name="order-tracker"}} |= "{endpoint}"' if endpoint else '{service_name="order-tracker"}'
    try:
        r = httpx.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "start": str(ago_ns), "end": str(now_ns), "limit": 50},
            timeout=5,
        )
        r.raise_for_status()
        values = r.json().get("data", {}).get("result", [])
        lines = []
        for stream in values:
            for _, msg in stream.get("values", []):
                lines.append(msg)
        return lines[-30:]          # keep last 30 lines
    except Exception as exc:
        return [f"[loki error: {exc}]"]


def _prom_5xx(endpoint: str, minutes: int = 15) -> str:
    """Query Prometheus for 5xx count on the endpoint over the window."""
    route_filter = f', http_route="{endpoint}"' if endpoint else ""
    expr = (
        f'sum(increase(order_tracker_http_server_request_count_total'
        f'{{http_status_code=~"5.."{route_filter}}}[{minutes}m]))'
    )
    try:
        r = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": expr},
            timeout=5,
        )
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if results:
            return results[0]["value"][1]
        return "0"
    except Exception as exc:
        return f"[prom error: {exc}]"


def _build_incident(alert: dict) -> dict:
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    endpoint    = labels.get("http_route", labels.get("endpoint", ""))
    alertname   = labels.get("alertname", "unknown")
    summary     = annotations.get("summary", "(no summary)")
    description = annotations.get("description", "")
    ts          = datetime.now(timezone.utc).isoformat()

    logs        = _loki_logs(endpoint)
    count_5xx   = _prom_5xx(endpoint)
    dashboard   = f"{GRAFANA_URL}/d/order-tracker-overview"

    return {
        "timestamp":    ts,
        "alertname":    alertname,
        "status":       alert.get("status", "unknown"),
        "endpoint":     endpoint or "(unknown)",
        "summary":      summary,
        "description":  description,
        "metrics": {
            "5xx_count_last_15m": count_5xx,
        },
        "recent_logs":  logs,
        "dashboard":    dashboard,
        "labels":       labels,
        "annotations":  annotations,
    }


def _save_incident(incident: dict) -> Path:
    slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = INCIDENTS_DIR / f"{slug}.json"
    path.write_text(json.dumps(incident, indent=2))
    return path


def _build_prompt(incident: dict, report_path: Path) -> str:
    logs_block = "\n".join(f"  {l}" for l in incident["recent_logs"]) or "  (none)"
    return textwrap.dedent(f"""\
        # Incident Report – {incident["alertname"]}

        You are the on-call responder for the order-tracker service.
        An alert has fired. Investigate and provide a concise summary.

        ## Alert details
        - **Alert name**: {incident["alertname"]}
        - **Status**: {incident["status"]}
        - **Summary**: {incident["summary"]}
        - **Affected endpoint**: {incident["endpoint"]}
        - **5xx errors (last 15 min)**: {incident["metrics"]["5xx_count_last_15m"]}
        - **Dashboard**: {incident["dashboard"]}
        - **Full report**: {report_path}

        ## Recent logs (last 30 lines)
        {logs_block}

        ## Task
        1. Determine the likely cause of the alert based on the data above.
        2. Suggest the immediate remediation steps.
        3. State whether this needs escalation to a developer.
        4. End your response with a one-line conclusion prefixed: `CONCLUSION:`.

        Work inside the repository at {WORKSPACE}.
    """)


# ---------------------------------------------------------------------------
# Background handler
# ---------------------------------------------------------------------------

def handle_alert(alert: dict) -> None:
    incident   = _build_incident(alert)
    path       = _save_incident(incident)
    prompt     = _build_prompt(incident, path)

    print(f"[responder] incident saved -> {path}")
    print(f"[responder] launching agent for: {incident['alertname']}")

    # Write prompt to a file; pass as positional arg to the agent
    prompt_file = INCIDENTS_DIR / f"{path.stem}_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    import os as _os
    env = _os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [AGY_CMD, "chat", "--mode", "agent", "--reuse-window", prompt]
    result = subprocess.run(
        cmd,
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
    )

    response_text = result.stdout or result.stderr or "(no output)"
    response_path = INCIDENTS_DIR / f"{path.stem}_response.txt"
    response_path.write_text(response_text, encoding="utf-8")

    incident["agent_response"] = response_text
    path.write_text(json.dumps(incident, indent=2), encoding="utf-8")

    print(f"[responder] agent finished -> {response_path}")
    print("[responder] --- agent response ---")
    print(response_text[-2000:])   # last 2000 chars to stdout


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts")
async def receive_alerts(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    alerts  = payload.get("alerts", [])

    firing = [a for a in alerts if a.get("status") == "firing"]
    if not firing:
        return JSONResponse({"accepted": 0, "reason": "no firing alerts"})

    for alert in firing:
        background_tasks.add_task(handle_alert, alert)

    return JSONResponse({"accepted": len(firing)})
