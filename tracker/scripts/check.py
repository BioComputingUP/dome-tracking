#!/usr/bin/env python3
"""
check.py — run one uptime check pass over every enabled resource.

For each resource URL: GET (redirects followed, first 64 KB of the body read),
classify the outcome as up / challenged / down, re-check failures once after a
short delay, then fold the results into the data directory:

  daily/YYYY/YYYY-MM-DD.json   per-resource counters + latency histogram for the day
  state.json                   current state per resource (since, consecutive downs, last detail)
  events.jsonl                 state changes, and changes of failure reason while down

Classification (see tracker/README.md):
  up          final 2xx/3xx, or 401/403/429 and other non-404 4xx (server responding, access restricted)
  challenged  bot-challenge interstitial (Cloudflare, Anubis, DDoS-Guard) — server is responding
  down        404/410 on the check URL (flagged url_review), 5xx, timeout, DNS, TLS or connection error

Usage:
    python check.py --data DIR [--config tracker/resources.yml] [--retry-delay 30]
                    [--workers 24] [--limit N] [--only id1,id2] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (DEFAULT_CONFIG, INTERVAL_MINUTES, RECENT_RUNS_HOURS,  # noqa: E402
                    RECENT_RUNS_MAX, USER_AGENT, bucket_index, daily_path, effective_url,
                    empty_counts, events_path, iso, load_json, load_resources, log, parse_iso,
                    state_path, utcnow, write_json)

MAX_BODY_BYTES = 64 * 1024
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 20
MAX_TOTAL_SECONDS = 40

CF_CHALLENGE = re.compile(r"<title>\s*Just a moment\.\.\.\s*</title>|/cdn-cgi/challenge-platform/", re.I)
ANUBIS = re.compile(r"Making sure you(?:&#39;|&#x27;|'|’)re not a bot|\.within\.website/x/cmd/anubis", re.I)
DDOS_GUARD = re.compile(r"<title>\s*DDoS-Guard\s*</title>", re.I)
GENERIC_CHALLENGE = re.compile(r"Checking your browser before accessing", re.I)

# SSL failures that a browser would also refuse -> down. Anything else (typically an
# incomplete intermediate chain, which browsers repair via AIA) is retried unverified.
TLS_FATAL = ("certificate has expired", "hostname mismatch", "doesn't match",
             "self-signed certificate", "self signed certificate", "wrong version number")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT,
                          "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})
        _local.session = s
    return s


def detect_challenge(code: int | None, headers: dict, body: str) -> str | None:
    """Return the challenge provider name if the response is a bot-challenge page."""
    if headers.get("cf-mitigated", "").lower() == "challenge" or CF_CHALLENGE.search(body):
        return "cloudflare"
    if ANUBIS.search(body):
        return "anubis"
    if DDOS_GUARD.search(body) or (code in (403, 503) and "ddos-guard" in headers.get("server", "").lower()):
        return "ddos-guard"
    if GENERIC_CHALLENGE.search(body):
        return "browser-check"
    return None


def classify(code: int | None, headers: dict | None = None, body: str = "",
             error_kind: str | None = None) -> dict:
    """Map a fetch outcome to {state, reason, url_review}. Pure function (unit tested)."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if error_kind:
        return {"state": "down", "reason": error_kind, "url_review": False}
    challenge = detect_challenge(code, headers, body or "")
    if challenge:
        return {"state": "challenged", "reason": f"challenge:{challenge}", "url_review": False}
    if code in (404, 410):
        return {"state": "down", "reason": f"http_{code}", "url_review": True}
    if code is None or code >= 500:
        return {"state": "down", "reason": f"http_{code}", "url_review": False}
    if code >= 400:
        return {"state": "up", "reason": f"http_{code}", "url_review": False}
    return {"state": "up", "reason": "ok", "url_review": False}


def _error_kind(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout) or "read timed out" in text:
        return "read_timeout"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirect_loop"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if any(s in text for s in ("name or service not known", "nodename nor servname",
                                   "failed to resolve", "name resolution", "getaddrinfo failed")):
            return "dns_error"
        if "connection refused" in text:
            return "connection_refused"
        return "connection_error"
    return "request_error"


def fetch(url: str, verify: bool = True) -> dict:
    """GET url; return {code, headers, body, latency_ms, final_url} or {error_kind, error}."""
    t0 = time.monotonic()
    try:
        with _session().get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True,
                            stream=True, verify=verify) as r:
            chunks, size = [], 0
            for chunk in r.iter_content(8192):
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BODY_BYTES or time.monotonic() - t0 > MAX_TOTAL_SECONDS:
                    break
            body = b"".join(chunks)[:MAX_BODY_BYTES].decode(r.encoding or "utf-8", errors="replace")
            return {"code": r.status_code, "headers": dict(r.headers), "body": body,
                    "latency_ms": round((time.monotonic() - t0) * 1000), "final_url": r.url}
    except Exception as exc:  # noqa: BLE001 — every failure mode is classified, never raised
        return {"error_kind": _error_kind(exc), "error": str(exc)[:300],
                "latency_ms": round((time.monotonic() - t0) * 1000)}


def check_resource(res: dict) -> dict:
    url = effective_url(res)
    verify = res.get("check", {}).get("verify_tls", True)
    got = fetch(url, verify=verify)
    tls_warning = None
    if got.get("error_kind") == "tls_error" and verify:
        if not any(s in got["error"].lower() for s in TLS_FATAL):
            retry = fetch(url, verify=False)
            if "error_kind" not in retry:
                tls_warning = got["error"][:200]
                got = retry
    verdict = classify(got.get("code"), got.get("headers"), got.get("body", ""), got.get("error_kind"))
    return {
        "id": res["id"],
        "url": url,
        "checked_at": iso(utcnow()),
        "state": verdict["state"],
        "reason": verdict["reason"],
        "url_review": verdict["url_review"],
        "code": got.get("code"),
        "latency_ms": got.get("latency_ms"),
        "final_url": got.get("final_url"),
        "error": got.get("error"),
        "tls_warning": tls_warning,
    }


def run_checks(resources: list[dict], workers: int = 24, retry_delay: float = 30) -> dict[str, dict]:
    """Check all resources; failures are re-checked once after retry_delay seconds."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = {r["id"]: r for r in pool.map(check_resource, resources)}
    failed = [res for res in resources if results[res["id"]]["state"] == "down"]
    if failed and retry_delay >= 0:
        log(f"{len(failed)} failed; re-checking in {retry_delay:.0f}s to confirm")
        time.sleep(retry_delay)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for second in pool.map(check_resource, failed):
                second["recovered_on_retry"] = second["state"] != "down"
                results[second["id"]] = second
    return results


def record(data_dir: Path, results: dict[str, dict], now: datetime) -> dict:
    """Fold one run's results into daily counters, state.json and events.jsonl."""
    day_file = daily_path(data_dir, now.date())
    daily = load_json(day_file) or {"date": now.date().isoformat(), "interval_minutes": INTERVAL_MINUTES,
                                    "runs": 0, "first_run": iso(now), "last_run": None, "resources": {}}
    daily["runs"] += 1
    daily["last_run"] = iso(now)

    state = load_json(state_path(data_dir)) or {"updated": None, "runs_total": 0, "resources": {}}
    state["updated"] = iso(now)
    state["runs_total"] += 1
    cutoff = now - timedelta(hours=RECENT_RUNS_HOURS)
    recent = [ts for ts in state.get("recent_runs", []) if parse_iso(ts) > cutoff]
    state["recent_runs"] = (recent + [iso(now)])[-RECENT_RUNS_MAX:]
    events = []

    for rid, res in results.items():
        counts = daily["resources"].setdefault(rid, empty_counts())
        counts["checks"] += 1
        counts[res["state"]] += 1
        if res["state"] != "down" and res.get("latency_ms") is not None:
            counts["latency_sum_ms"] += res["latency_ms"]
            counts["latency_hist"][bucket_index(res["latency_ms"])] += 1

        prev = state["resources"].get(rid)
        detail = {"code": res.get("code"), "reason": res["reason"], "error": res.get("error")}
        if prev is None or prev["state"] != res["state"]:
            events.append({"ts": iso(now), "id": rid, "event": "state_change",
                           "from": prev["state"] if prev else None, "to": res["state"], **detail})
            since = iso(now)
        else:
            since = prev["since"]
            if res["state"] == "down" and prev.get("reason") != res["reason"]:
                events.append({"ts": iso(now), "id": rid, "event": "down_reason_changed",
                               "from": prev.get("reason"), "to": res["reason"], **detail})
        state["resources"][rid] = {
            "state": res["state"],
            "since": since,
            "first_seen": prev["first_seen"] if prev else iso(now),
            "consecutive_down": (prev.get("consecutive_down", 0) + 1 if prev else 1) if res["state"] == "down" else 0,
            "last_checked": res["checked_at"],
            "code": res.get("code"),
            "reason": res["reason"],
            "error": res.get("error"),
            "latency_ms": res.get("latency_ms"),
            "url": res["url"],
            "final_url": res.get("final_url"),
            "url_review": res["url_review"],
            "tls_warning": res.get("tls_warning"),
        }

    write_json(day_file, daily)
    write_json(state_path(data_dir), state)
    if events:
        with open(events_path(data_dir), "a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return {"events": len(events), "day_file": str(day_file)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--data", type=Path, help="data directory (tracker-data branch checkout)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--retry-delay", type=float, default=30)
    ap.add_argument("--limit", type=int, help="only check the first N resources (testing)")
    ap.add_argument("--only", help="comma-separated resource ids")
    ap.add_argument("--dry-run", action="store_true", help="print results as JSON; write nothing")
    args = ap.parse_args()

    if not args.dry_run and not args.data:
        ap.error("--data is required unless --dry-run")
    resources = load_resources(args.config)
    if args.only:
        wanted = set(args.only.split(","))
        resources = [r for r in resources if r["id"] in wanted]
    if args.limit:
        resources = resources[: args.limit]

    now = utcnow()
    log(f"checking {len(resources)} resources")
    results = run_checks(resources, workers=args.workers, retry_delay=args.retry_delay)

    by_state = {s: sum(1 for r in results.values() if r["state"] == s) for s in ("up", "challenged", "down")}
    if args.dry_run:
        print(json.dumps(list(results.values()), indent=1))
    else:
        info = record(args.data, results, now)
        log(f"recorded run at {iso(now)} -> {info['day_file']} ({info['events']} events)")
    print(f"up={by_state['up']} challenged={by_state['challenged']} down={by_state['down']}")
    for r in sorted(results.values(), key=lambda r: r["id"]):
        if r["state"] == "down":
            print(f"  DOWN {r['id']}: {r['reason']} {r['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
