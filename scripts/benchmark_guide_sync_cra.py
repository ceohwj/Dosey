"""Measure CR-A's synchronous 201 path with a synthetic delayed executor.

Call ``run_benchmark`` from the isolated real-PostgreSQL integration harness.
The caller supplies a phase-timing reader for the request-scoped execution
instance. This helper does not initialize a database or call a Provider.
"""

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol


class GuideHttpClient(Protocol):
    async def post(self, path: str, *, json: dict[str, str]) -> Any: ...


@dataclass(frozen=True)
class PhaseTimes:
    intake_seconds: float
    execution_seconds: float
    finalize_seconds: float


def _percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percent
    lower = math.floor(rank)
    upper = math.ceil(rank)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


async def run_benchmark(
    *,
    client: GuideHttpClient,
    prescription_id: str,
    set_executor_delay_ms: Callable[[int], None],
    read_phase_times: Callable[[], PhaseTimes],
    output_path: Path,
    metadata: dict[str, str | int],
    warmup: int = 5,
    samples: int = 20,
) -> dict[str, Any]:
    """Return/write raw samples and p50/p95 for 0, 500 and 2000 ms cases."""

    if warmup < 0 or samples < 2:
        raise ValueError("warmup must be nonnegative and samples must be at least two")
    cases: list[dict[str, Any]] = []
    for delay_ms in (0, 500, 2000):
        set_executor_delay_ms(delay_ms)
        for ordinal in range(warmup + samples):
            start = perf_counter()
            response = await client.post("/api/v1/guides", json={"prescription_id": prescription_id})
            http_seconds = perf_counter() - start
            if response.status_code != 201:
                raise RuntimeError(f"benchmark stopped: HTTP {response.status_code} at {delay_ms} ms")
            if ordinal < warmup:
                continue
            phases = read_phase_times()
            if min(phases.intake_seconds, phases.execution_seconds, phases.finalize_seconds) < 0:
                raise ValueError("phase timings must be nonnegative")
            cases.append(
                {
                    "delay_ms": delay_ms,
                    "sample": ordinal - warmup + 1,
                    "http_seconds": http_seconds,
                    **asdict(phases),
                }
            )
    summary = {
        str(delay): {
            key: {
                "p50": _percentile([row[key] for row in cases if row["delay_ms"] == delay], 0.5),
                "p95": _percentile([row[key] for row in cases if row["delay_ms"] == delay], 0.95),
            }
            for key in ("http_seconds", "intake_seconds", "execution_seconds", "finalize_seconds")
        }
        for delay in (0, 500, 2000)
    }
    result = {
        "scope": "synthetic CR-A plumbing; excludes live retrieval, Provider, and medical quality",
        "metadata": metadata,
        "warmup_per_case": warmup,
        "samples_per_case": samples,
        "cases": cases,
        "summary_seconds": summary,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
