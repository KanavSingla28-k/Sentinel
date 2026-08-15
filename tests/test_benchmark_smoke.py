"""Smoke-run the Phase 14 benchmark harness at tiny scale (Phase 14).

The harness is exercised end-to-end through a subprocess so pytest's asyncio
loop and the harness's own asyncio.run never share an event loop. The smoke
run is `slow`-marked (existing slow CI job, real Redis service) and
`integration`-marked (inherits the `redis_client` auto-skip). It asserts the
harness produces a well-formed report, not any performance value.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sentinel.redis import SentinelRedis

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "benchmarks" / "benchmark.py"
CELL_IDS = {"B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"}
ENVIRONMENT_KEYS = (
    "git_commit",
    "platform",
    "python_version",
    "cpu_processor",
    "cpu_count",
    "redis_version",
    "timestamp",
)


@pytest.mark.integration
async def test_bench_smoke_runs_and_reports_sane_statistics(
    redis_client: SentinelRedis, tmp_path: Path
) -> None:
    out = tmp_path / "smoke.json"
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--smoke", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["smoke"] is True
    assert all(key in report["environment"] for key in ENVIRONMENT_KEYS)
    assert {cell["id"] for cell in report["cells"]} == CELL_IDS
    assert len(report["cells"]) == len(CELL_IDS) * len(
        {cell["concurrency"] for cell in report["cells"]}
    )
    for cell in report["cells"]:
        aggregate = cell["aggregate"]
        latency = aggregate["latency_us"]
        assert 0 <= latency["p50_us"] <= latency["p95_us"] <= latency["p99_us"]
        assert aggregate["over_limit"] == 0
        assert aggregate["throughput_ops_per_sec"] > 0
        assert sum(aggregate["counts"].values()) == aggregate["ops_total"]
