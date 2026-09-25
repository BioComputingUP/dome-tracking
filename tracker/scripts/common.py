"""
common.py — shared helpers for the DOME tracker (uptime + repository activity).

Reads config from tracker/ and reads/writes only the --data directory (the
`tracker-data` branch checkout in CI). Derived from the ECD uptime tracker
(github.com/gavinf97/ECD, uptime/).

Conventions:
  - log() writes progress to stderr; stdout is kept for short summaries.
  - All timestamps are UTC ISO-8601 with seconds precision.
  - JSON files are written atomically (temp file + rename).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

TRACKER_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = TRACKER_ROOT / "resources.yml"

USER_AGENT = "DOME-Tracker/0.1 (+https://github.com/BioComputingUP/dome-tracking)"
# Target gap between checks; coverage is measured against it. Runs are triggered every 20 min
# from outside GitHub (workflow_dispatch), with GitHub crons as a best-effort backup, so extra
# runs are normal and harmless: coverage is capped at 100%. Each daily file records the
# interval in force that day, so changing it later does not distort past coverage.
INTERVAL_MINUTES = 20
# No run for this long -> monitoring is reported as STALE (stopped), independent of the interval,
# so a late run or two is not mistaken for a dead schedule.
STALE_AFTER_MINUTES = 180
# state.json keeps the timestamps of recent runs so every surface can show runs in the last 24 h.
RECENT_RUNS_HOURS = 48
RECENT_RUNS_MAX = 200

# Upper bounds (ms) of the latency histogram buckets; one extra overflow bucket.
LATENCY_BUCKETS_MS = [250, 500, 1000, 2000, 5000, 10000]
N_BUCKETS = len(LATENCY_BUCKETS_MS) + 1

STATES = ("up", "challenged", "down")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "resource"


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def effective_url(res: dict) -> str:
    """The URL actually checked: check.url overrides url."""
    return (res.get("check") or {}).get("url") or res["url"]


def load_resources(config: Path, enabled_only: bool = True) -> list[dict]:
    with open(config, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    resources = doc.get("resources") or []
    ids = [r["id"] for r in resources]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate resource ids in {config}: {sorted(dupes)}")
    if enabled_only:
        resources = [r for r in resources if r.get("enabled", True)]
    return resources


# --- data directory layout (the tracker-data branch) -------------------------

def daily_path(data_dir: Path, day: date) -> Path:
    return data_dir / "daily" / f"{day:%Y}" / f"{day:%Y-%m-%d}.json"


def state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def events_path(data_dir: Path) -> Path:
    return data_dir / "events.jsonl"


def activity_path(data_dir: Path) -> Path:
    return data_dir / "activity.json"


def activity_history_path(data_dir: Path) -> Path:
    return data_dir / "activity_history.jsonl"


def runs_within(recent_runs: list[str], now: datetime, hours: float) -> int:
    cutoff = now - timedelta(hours=hours)
    return sum(1 for ts in recent_runs if parse_iso(ts) > cutoff)


def empty_counts() -> dict:
    return {"checks": 0, "up": 0, "challenged": 0, "down": 0,
            "latency_sum_ms": 0, "latency_hist": [0] * N_BUCKETS}


def bucket_index(latency_ms: float) -> int:
    for i, bound in enumerate(LATENCY_BUCKETS_MS):
        if latency_ms <= bound:
            return i
    return len(LATENCY_BUCKETS_MS)


def hist_percentile(hist: list[int], q: float) -> str | None:
    """Approximate percentile from the bucket histogram, as a label like '≤500 ms'."""
    total = sum(hist)
    if not total:
        return None
    target = q * total
    running = 0
    for i, n in enumerate(hist):
        running += n
        if running >= target:
            if i < len(LATENCY_BUCKETS_MS):
                return f"≤{LATENCY_BUCKETS_MS[i]} ms"
            return f">{LATENCY_BUCKETS_MS[-1]} ms"
    return f">{LATENCY_BUCKETS_MS[-1]} ms"


def daterange(start: date, end: date):
    """Inclusive range of dates."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)
