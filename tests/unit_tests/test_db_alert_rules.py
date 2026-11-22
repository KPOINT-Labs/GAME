"""Database (CloudNativePG) alert rules: parse, and pass their promtool unit
tests (tests/prometheus/db-rules.test.yml) when Docker is available."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_MAP = ROOT / "kubernetes/configmaps/db-prometheus-rules.yaml"
PROMTOOL_TESTS = ROOT / "tests/prometheus/db-rules.test.yml"
PROMETHEUS_IMAGE = "prom/prometheus:v3.7.3"


def _rules_text() -> str:
    return yaml.safe_load(CONFIG_MAP.read_text())["data"]["db-rules.yml"]


def test_db_alert_rules_parse_and_cover_archiving_and_backups():
    rules = yaml.safe_load(_rules_text())
    names = [r["alert"] for g in rules["groups"] for r in g["rules"]]
    assert names == [
        "CnpgWalArchivingFailing",
        "CnpgWalArchivingStalled",
        "CnpgWalArchiveBacklog",
        "CnpgBackupTooOld",
        "CnpgLastBackupFailed",
    ]
    for group in rules["groups"]:
        for rule in group["rules"]:
            assert rule["labels"]["component"] == "database"
            assert "by (namespace, cluster)" in rule["expr"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")
def test_db_alert_rules_pass_promtool_unit_tests(tmp_path):
    (tmp_path / "db-rules.yml").write_text(_rules_text())
    shutil.copy(PROMTOOL_TESTS, tmp_path / "db-rules.test.yml")
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                # pytest's tmp dir is private to this user
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{tmp_path}:/r",
                "-w",
                "/r",
                "--entrypoint",
                "promtool",
                PROMETHEUS_IMAGE,
                "test",
                "rules",
                "db-rules.test.yml",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"docker unavailable: {exc}")
    if "Cannot connect to the Docker daemon" in result.stderr:  # pragma: no cover
        pytest.skip("docker daemon unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
