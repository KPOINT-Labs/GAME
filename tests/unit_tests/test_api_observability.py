"""
Per-route latency metrics, the "API Overview" dashboard and the API alert
rules stay consistent with the app.

promtool validation of the PromQL itself needs a Prometheus binary and was
run against the dev Prometheus container; these tests guard the wiring.
"""

import json
import re
from pathlib import Path

import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import HTTP_LATENCY_BUCKETS, app

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "docker/grafana/dashboards/api-overview.json"
RULES = ROOT / "docker/prometheus/rules/api-rules.yml"
K8S_RULES = ROOT / "kubernetes/configmaps/api-prometheus-rules.yaml"
ACTIVITY = "/api/v1/games/{gameId}/users/{externalUserId}/activity"


def _bucket_bounds(metrics_text, handler):
    return [
        re.search(r'le="([^"]+)"', line).group(1)
        for line in metrics_text.splitlines()
        if line.startswith("http_request_duration_seconds_bucket")
        and f'handler="{handler}"' in line
    ]


def test_per_route_latency_histogram_uses_fine_buckets():
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/api/v1/kpi/health_check")

    bounds = _bucket_bounds(client.get("/metrics").text, "/api/v1/kpi/health_check")

    assert bounds == [str(float(b)) for b in HTTP_LATENCY_BUCKETS] + ["+Inf"]
    # Fine resolution below 100ms, where most endpoints answer.
    assert sum(1 for b in HTTP_LATENCY_BUCKETS if b <= 0.1) >= 5


def test_dashboard_is_valid_and_targets_existing_metrics():
    dashboard = json.loads(DASHBOARD.read_text())

    assert dashboard["uid"] == "game-api-overview"
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    assert exprs, "dashboard has no queries"
    for expr in exprs:
        assert re.search(
            r"http_(request_duration_seconds|requests_total|response_size_bytes)", expr
        )
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids)), "panel ids must be unique"


def test_alert_rules_parse_and_kubernetes_copy_is_in_sync():
    rules = yaml.safe_load(RULES.read_text())
    config_map = yaml.safe_load(K8S_RULES.read_text())

    names = [r["alert"] for g in rules["groups"] for r in g["rules"]]
    assert names == [
        "ApiRouteLatencyP95High",
        "ApiActivityLatencyP95High",
        "ApiRoute5xxRateHigh",
    ]
    assert yaml.safe_load(config_map["data"]["api-rules.yml"]) == rules


def test_activity_route_template_in_dashboard_and_rules_exists():
    templates = {r.path for r in app.routes if isinstance(r, APIRoute)}

    assert ACTIVITY in templates
    assert ACTIVITY in DASHBOARD.read_text()
    assert ACTIVITY in RULES.read_text()
