#!/usr/bin/env python3
"""
summarize.py — build every report the tracker publishes, from the data directory.

Written into the data directory (the `tracker-data` branch in CI):

  STATUS.md        human-readable status page, rendered by GitHub on the branch
  summary.json     the same data, machine-readable; also feeds the dashboard
  history.json     per-resource daily uptime for the last 90 days (dashboard strips)
  badges/*.json    shields.io endpoints for the badges on the README
  README.md        explains the branch to anyone who lands on it

Definitions (see tracker/README.md):
  uptime    = (up + challenged) / checks          over the checks actually recorded
  coverage  = checks / expected checks            expected = one per interval since the resource
                                                  was first seen, capped at 100%; missed cron
                                                  runs lower coverage, never counted as up
  windows   = today (UTC), 7d, 30d, 90d, 365d     calendar days ending today (UTC)

Monitoring is continuous: every resource is checked every interval, indefinitely. 99% is a
reference line only, used to sort the "below target" list. Repository activity comes from
activity.json, written by activity.py earlier in the same run.

Usage:
    python summarize.py --data DIR [--config tracker/resources.yml]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (DEFAULT_CONFIG, INTERVAL_MINUTES, N_BUCKETS, activity_path,  # noqa: E402
                    daily_path, daterange, effective_url, events_path, hist_percentile, iso,
                    load_json, load_resources, log, parse_iso, runs_within, state_path, utcnow,
                    write_json)

WINDOWS = [("today", 1), ("7d", 7), ("30d", 30), ("90d", 90), ("365d", 365)]
HISTORY_DAYS = 90
TARGET = 0.99                 # reference line; sorts the "below target" list only
STALE_AFTER_INTERVALS = 3     # no run for this many intervals -> monitoring reported as stale
RECENT_EVENTS = 25

REPO = "BioComputingUP/dome-tracking"
REPO_URL = f"https://github.com/{REPO}"
DATA_BRANCH = "tracker-data"
DATA_BRANCH_URL = f"{REPO_URL}/blob/{DATA_BRANCH}"
DASHBOARD_URL = "https://biocomputingup.github.io/dome-tracking/"
ACTIONS_URL = f"{REPO_URL}/actions/workflows/tracker-check.yml"


def load_days(data_dir: Path, start: date, end: date) -> dict[date, dict]:
    days = {}
    for d in daterange(start, end):
        doc = load_json(daily_path(data_dir, d))
        if doc:
            days[d] = doc
    return days


def expected_checks(day: date, now: datetime, first_seen: datetime | None,
                    interval: int = INTERVAL_MINUTES) -> int:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = min(start + timedelta(days=1), now)
    if first_seen:
        start = max(start, first_seen)
    if end <= start:
        return 0
    return math.ceil((end - start).total_seconds() / (interval * 60))


def aggregate(days: dict[date, dict], rid: str, start: date, end: date, now: datetime,
              first_seen: datetime | None, interval: int = INTERVAL_MINUTES) -> dict:
    agg = {"checks": 0, "up": 0, "challenged": 0, "down": 0, "latency_sum_ms": 0,
           "latency_hist": [0] * N_BUCKETS, "expected": 0}
    for d in daterange(start, end):
        # Measure each day against the interval in force that day, so a change of schedule
        # does not distort the coverage recorded for earlier days.
        agg["expected"] += expected_checks(d, now, first_seen,
                                           days.get(d, {}).get("interval_minutes", interval))
        counts = days.get(d, {}).get("resources", {}).get(rid)
        if not counts:
            continue
        for k in ("checks", "up", "challenged", "down", "latency_sum_ms"):
            agg[k] += counts.get(k, 0)
        for i, n in enumerate(counts.get("latency_hist", [])[:N_BUCKETS]):
            agg["latency_hist"][i] += n
    return agg


def rates(agg: dict) -> dict:
    checks = agg["checks"]
    uptime = (agg["up"] + agg["challenged"]) / checks if checks else None
    expected = max(agg["expected"], 1 if checks else 0)
    coverage = min(1.0, checks / expected) if expected else None
    return {"uptime": uptime, "coverage": coverage, "checks": checks, "down": agg["down"],
            "challenged": agg["challenged"]}


def monitoring_health(state: dict, now: datetime) -> dict:
    """Is the tracker actually running? Reported at the top of every surface."""
    last_run = state.get("updated")
    since_min = None
    if last_run:
        since_min = round((now - parse_iso(last_run)).total_seconds() / 60)
    stale = since_min is None or since_min > INTERVAL_MINUTES * STALE_AFTER_INTERVALS
    recent_runs = state.get("recent_runs", [])
    return {
        "last_run": last_run,
        "minutes_since_last_run": since_min,
        "runs_total": state.get("runs_total", 0),
        "runs_last_24h": runs_within(recent_runs, now, 24),
        "recent_runs": recent_runs,
        "interval_minutes": INTERVAL_MINUTES,
        "stale": stale,
        "status": "STALE" if stale else "ACTIVE",
        "workflow_url": ACTIONS_URL,
    }


def build_summary(resources: list[dict], data_dir: Path, now: datetime) -> dict:
    today = now.date()
    state = load_json(state_path(data_dir)) or {"resources": {}, "runs_total": 0}
    days = load_days(data_dir, today - timedelta(days=365), today)

    rows = []
    for res in resources:
        rid = res["id"]
        st = state["resources"].get(rid, {})
        first_seen = parse_iso(st["first_seen"]) if st.get("first_seen") else None
        windows = {}
        for name, n in WINDOWS:
            windows[name] = rates(aggregate(days, rid, today - timedelta(days=n - 1), today, now, first_seen))
        lat = aggregate(days, rid, today - timedelta(days=29), today, now, first_seen)
        ok = lat["up"] + lat["challenged"]
        rows.append({
            "id": rid, "name": res.get("name", rid), "url": effective_url(res),
            "repo": res.get("repo"),
            "state": st.get("state", "unknown"), "since": st.get("since"),
            "reason": st.get("reason"), "code": st.get("code"), "error": st.get("error"),
            "first_seen": st.get("first_seen"),
            "last_checked": st.get("last_checked"), "final_url": st.get("final_url"),
            "url_review": bool(st.get("url_review")), "tls_warning": st.get("tls_warning"),
            "windows": windows,
            "latency_30d": {"p50": hist_percentile(lat["latency_hist"], 0.5),
                            "p95": hist_percentile(lat["latency_hist"], 0.95),
                            "mean_ms": round(lat["latency_sum_ms"] / ok) if ok else None},
        })

    totals = {s: sum(1 for r in rows if r["state"] == s) for s in ("up", "challenged", "down", "unknown")}
    totals["url_review"] = sum(1 for r in rows if r["url_review"])
    totals["resources"] = len(rows)
    return {"generated": iso(now), "interval_minutes": INTERVAL_MINUTES,
            "monitoring": monitoring_health(state, now),
            "runs_total": state.get("runs_total", 0), "last_run": state.get("updated"),
            "resource_count": len(rows), "target": TARGET,
            "dashboard_url": DASHBOARD_URL, "totals": totals, "resources": rows,
            "activity": build_activity(resources, data_dir)}


def build_activity(resources: list[dict], data_dir: Path) -> dict:
    """Per resource id, the snapshot activity.py wrote this run (in resources.yml order)."""
    doc = load_json(activity_path(data_dir)) or {}
    repos = doc.get("repos", {})
    ordered = {r["id"]: {"name": r.get("name", r["id"]), **repos[r["id"]]}
               for r in resources if r["id"] in repos}
    return {"generated": doc.get("generated"), "repos": ordered}


def build_history(resources: list[dict], data_dir: Path, now: datetime) -> dict:
    """Per-resource daily uptime for the last HISTORY_DAYS, for the dashboard's bar strips.

    uptime[i] is a percentage 0-100 (rounded, one decimal) or null when no check was recorded
    that day. checks[i] is the number of checks behind it, so the dashboard can show
    "no data" and "one flaky check" differently.
    """
    today = now.date()
    start = today - timedelta(days=HISTORY_DAYS - 1)
    days = load_days(data_dir, start, today)
    out = {}
    for res in resources:
        rid = res["id"]
        up_pct, checks = [], []
        for d in daterange(start, today):
            c = days.get(d, {}).get("resources", {}).get(rid)
            n = (c or {}).get("checks", 0)
            checks.append(n)
            up_pct.append(round((c["up"] + c["challenged"]) / n * 100, 1) if n else None)
        out[rid] = {"uptime": up_pct, "checks": checks}
    return {"generated": iso(now), "days": HISTORY_DAYS,
            "start": start.isoformat(), "end": today.isoformat(), "resources": out}


def read_recent_events(data_dir: Path, limit: int = RECENT_EVENTS) -> list[dict]:
    path = events_path(data_dir)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit * 4:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events[-limit:][::-1]


# --- badges (shields.io endpoint schema) -------------------------------------

def build_badges(summary: dict) -> dict[str, dict]:
    t, m = summary["totals"], summary["monitoring"]
    n = summary["resource_count"]
    every = ("hourly" if m["interval_minutes"] == 60
             else f"every {m['interval_minutes']} min" if m["interval_minutes"] < 60
             else f"every {m['interval_minutes'] // 60}h")

    def badge(label: str, message: str, color: str) -> dict:
        return {"schemaVersion": 1, "label": label, "message": message, "color": color}

    return {
        "monitoring": badge("monitoring", "stale — check the workflow" if m["stale"] else f"active · {every}",
                            "red" if m["stale"] else "brightgreen"),
        "resources": badge("resources monitored", str(n), "blue"),
        "up": badge("responding", f"{t['up'] + t['challenged']}/{n}",
                    "brightgreen" if t["up"] + t["challenged"] >= n * 0.9 else "yellow"),
        "down": badge("down", str(t["down"]), "brightgreen" if not t["down"] else "red"),
        "url-review": badge("url review", str(t["url_review"]),
                            "brightgreen" if not t["url_review"] else "orange"),
        "last-check": badge("last check", (m["last_run"] or "never").replace("T", " ").replace("Z", " UTC"),
                            "brightgreen" if not m["stale"] else "red"),
        "runs-24h": badge("runs in last 24h", str(m["runs_last_24h"]),
                          "brightgreen" if m["runs_last_24h"] >= 20
                          else "yellow" if m["runs_last_24h"] >= 10 else "red"),
        "activity": activity_badge(summary["activity"]["repos"]),
    }


def activity_badge(repos: dict) -> dict:
    n = len(repos)
    pushed = sum(1 for s in repos.values() if s.get("freshness") in ("active", "recent"))
    return {"schemaVersion": 1, "label": "repos pushed ≤30d", "message": f"{pushed}/{n}",
            "color": "brightgreen" if n and pushed == n else "yellow" if pushed else "lightgrey"}


# --- STATUS.md ---------------------------------------------------------------

def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def cell(text) -> str:
    return str(text if text not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def link(row: dict) -> str:
    return f"[{cell(row['name'])}]({row['url']})"


def ago(minutes: int | None) -> str:
    if minutes is None:
        return "never"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 60 * 48:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


STATE_ICON = {"up": "🟢 up", "challenged": "🟡 challenged", "down": "🔴 down", "unknown": "⚪ unknown"}
FRESHNESS_ICON = {"active": "🟢 active (<7 d)", "recent": "🟡 recent (<30 d)",
                  "quiet": "🔴 quiet (≥30 d)", "unknown": "⚪ unknown"}
BADGE_BASE = (f"https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/"
              f"{REPO}/{DATA_BRANCH}/badges")


def minutes_since(ts: str | None, now: datetime) -> int | None:
    return round((now - parse_iso(ts)).total_seconds() / 60) if ts else None


def render_activity(summary: dict) -> list[str]:
    repos = summary["activity"]["repos"]
    now = parse_iso(summary["generated"])
    out = ["## Repository activity", "",
           "`Last push` is the latest push to **any** branch; `Head commit` is the tip of the "
           "default branch. The full timeline is in "
           f"[activity_history.jsonl]({DATA_BRANCH_URL}/activity_history.jsonl).", ""]
    if not repos:
        return out + ["No repository activity recorded yet.", ""]
    out += ["| Resource | Repository | Last push | Activity | Head commit |", "|---|---|---|---|---|"]
    for rid, s in repos.items():
        c = s.get("last_commit") or {}
        sha = (c.get("sha") or "")[:7]
        commit = (f"[`{sha}`]({c['url']}) {cell(c.get('message'))} — {cell(c.get('author'))}, "
                  f"{cell((c.get('date') or '')[:10])}" if sha and c.get("url") else "—")
        repo_link = f"[{s['repo']}]({s.get('html_url') or 'https://github.com/' + s['repo']})"
        if s.get("default_branch"):
            repo_link += f" `{s['default_branch']}`"
        err = " ⚠️ API error, showing last known" if s.get("error") else ""
        out.append(f"| {cell(s.get('name', rid))} | {repo_link} | "
                   f"{cell(s.get('pushed_at'))} ({ago(minutes_since(s.get('pushed_at'), now))}) | "
                   f"{FRESHNESS_ICON.get(s.get('freshness'), '⚪ unknown')}{err} | {commit} |")
    return out + [""]


def render_status(summary: dict, events: list[dict]) -> str:
    rows = summary["resources"]
    t, m = summary["totals"], summary["monitoring"]
    every = (f"{m['interval_minutes']} minutes" if m["interval_minutes"] < 60
             else f"hour" if m["interval_minutes"] == 60 else f"{m['interval_minutes'] // 60} hours")

    out = [
        "# DOME tracker status",
        "",
        f"[![monitoring]({BADGE_BASE}/monitoring.json)]({ACTIONS_URL}) "
        f"[![up]({BADGE_BASE}/up.json)](#all-resources) "
        f"[![down]({BADGE_BASE}/down.json)](#needs-attention) "
        f"[![runs 24h]({BADGE_BASE}/runs-24h.json)]({ACTIONS_URL}) "
        f"[![activity]({BADGE_BASE}/activity.json)](#repository-activity)",
        "",
        f"## {'🔴 Monitoring is STALE' if m['stale'] else '🟢 Monitoring is ACTIVE'}",
        "",
        f"| | |", "|---|---|",
        f"| **Resources monitored** | {summary['resource_count']} — every one, every {every}, continuously |",
        f"| **Last check** | {m['last_run'] or '—'} ({ago(m['minutes_since_last_run'])}) |",
        f"| **Runs in the last 24 h** | {m['runs_last_24h']} (target ≥ 24; GitHub drops some scheduled runs) |",
        f"| **Checks recorded** | {m['runs_total']} runs since monitoring began |",
        f"| **Runs on** | [GitHub Actions → tracker-check]({ACTIONS_URL}) |",
        f"| **Visual dashboard** | {DASHBOARD_URL} |",
        "",
    ]
    if m["stale"]:
        out += [f"> The last check was {ago(m['minutes_since_last_run'])}, more than "
                f"{STALE_AFTER_INTERVALS}× the {every} interval. The scheduled workflow has probably "
                f"stopped — GitHub disables schedules in repositories with 60 days of no activity. "
                f"Re-enable it at [Actions → tracker-check]({ACTIONS_URL}).", ""]

    out += [
        f"### Right now",
        "",
        f"🟢 up **{t['up']}** · 🟡 challenged **{t['challenged']}** · 🔴 down **{t['down']}** · "
        f"⚪ never checked **{t['unknown']}** · 🔗 check URL to review **{t['url_review']}**",
        "",
        "Uptime = (up + challenged) ÷ recorded checks. Coverage = recorded ÷ expected checks. "
        f"{pct(summary['target'])} is a reference line only; it sorts the list below. "
        f"Full definitions: [tracker/README.md]({REPO_URL}/blob/main/tracker/README.md).",
        "",
        "---",
        "",
    ]
    out += render_activity(summary)
    out += ["---", ""]

    down = sorted((r for r in rows if r["state"] == "down"), key=lambda r: r["since"] or "")
    out += ["## Needs attention", "", "### 🔴 Currently down", ""]
    if down:
        out += [f"**{len(down)}** of {summary['resource_count']} resources are not responding.", "",
                "| Resource | Since (UTC) | Reason | 30d uptime |", "|---|---|---|---|"]
        out += [f"| {link(r)} | {cell(r['since'])} | `{cell(r['reason'])}`"
                f"{' 🔗' if r['url_review'] else ''} | {pct(r['windows']['30d']['uptime'])} |"
                for r in down]
        out += ["", "🔗 = the check URL returns 404/410; see below."]
    else:
        out.append("None — every monitored resource is responding.")
    out.append("")

    review = [r for r in rows if r["url_review"]]
    out += ["### 🔗 Check URL needs review", ""]
    if review:
        out += [f"**{len(review)}** return 404/410, so the resource has moved or been retired. "
                "They are counted as "
                "down until the URL is corrected: set `check.url` for the resource in "
                f"[tracker/resources.yml]({REPO_URL}/blob/main/tracker/resources.yml), or "
                "`enabled: false` if it is genuinely gone.", "",
                "| Resource | Check URL | Code |", "|---|---|---|"]
        out += [f"| {cell(r['name'])} | {r['url']} | `{cell(r['reason'])}` |"
                for r in review]
    else:
        out.append("None.")
    out.append("")

    below = sorted((r for r in rows if r["windows"]["30d"]["uptime"] is not None
                    and r["windows"]["30d"]["uptime"] < summary["target"]),
                   key=lambda r: r["windows"]["30d"]["uptime"])
    out += [f"### 📉 Below {pct(summary['target'])} over the last 30 days", ""]
    if below:
        out += [f"**{len(below)}** resources, worst first.", "",
                "| Resource | 30d uptime | 30d coverage | Down checks |", "|---|---|---|---|"]
        out += [f"| {link(r)} | {pct(r['windows']['30d']['uptime'])} | "
                f"{pct(r['windows']['30d']['coverage'])} | {r['windows']['30d']['down']} |" for r in below]
    else:
        out.append("None.")
    out += ["", "---", ""]

    out += ["## All resources", "",
            f"All {summary['resource_count']} resources, sorted by name. "
            f"Sortable and searchable on the [dashboard]({DASHBOARD_URL}).", "",
            "| Resource | State | Today | 7d | 30d | 90d | 365d | Coverage 30d | p95 30d |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["name"].lower()):
        w = r["windows"]
        tls = " ⚠️ TLS" if r.get("tls_warning") else ""
        out.append(f"| {link(r)} | {STATE_ICON.get(r['state'], r['state'])}{tls} | "
                   f"{pct(w['today']['uptime'])} | {pct(w['7d']['uptime'])} | {pct(w['30d']['uptime'])} | "
                   f"{pct(w['90d']['uptime'])} | {pct(w['365d']['uptime'])} | {pct(w['30d']['coverage'])} | "
                   f"{cell(r['latency_30d']['p95'])} |")
    out += ["", "---", ""]

    by_id = {r["id"]: r for r in rows}
    out += ["## Recent state changes", "",
            "The newest entries from `events.jsonl`, the full audit trail of every state change.", ""]
    if events:
        out += ["| When (UTC) | Resource | Change | Detail |", "|---|---|---|---|"]
        for ev in events:
            name = by_id.get(ev["id"], {}).get("name", ev["id"])
            change = (f"{STATE_ICON.get(ev.get('from'), ev.get('from') or 'new')} → "
                      f"{STATE_ICON.get(ev.get('to'), ev.get('to'))}" if ev.get("event") == "state_change"
                      else f"reason changed: `{cell(ev.get('from'))}` → `{cell(ev.get('to'))}`")
            out.append(f"| {cell(ev['ts'])} | {cell(name)} | {change} | `{cell(ev.get('reason'))}` |")
    else:
        out.append("None recorded yet.")
    out += ["", "---", "",
            f"Generated by [tracker/scripts/summarize.py]({REPO_URL}/blob/main/tracker/scripts/summarize.py) "
            f"at **{summary['generated']}**, and rewritten after every check. "
            f"Machine-readable: [summary.json]({DATA_BRANCH_URL}/summary.json).", ""]
    return "\n".join(out)


# --- the data branch's own README --------------------------------------------

def render_branch_readme(summary: dict) -> str:
    m = summary["monitoring"]
    return f"""# {DATA_BRANCH} — where the DOME tracker results are deposited

**You are on the data branch.** It holds only the output of the DOME tracker. The code that
produces it is in [`tracker/` on `main`]({REPO_URL}/tree/main/tracker).

## 👉 [STATUS.md](STATUS.md) — uptime and repository activity for all {summary['resource_count']} resources

Or the visual dashboard: **{DASHBOARD_URL}**

Monitoring is **{m['status']}** · last check {m['last_run'] or '—'} · {m['runs_last_24h']} runs in the
last 24 h · {m['runs_total']} runs recorded.

## What is in here

| File | What it is |
|---|---|
| [`STATUS.md`](STATUS.md) | The status page. Rendered by GitHub, rewritten after every check. |
| [`index.html`](index.html) | The dashboard, served at {DASHBOARD_URL} |
| [`summary.json`](summary.json) | Everything in STATUS.md, machine-readable. |
| [`history.json`](history.json) | Per-resource daily uptime for the last 90 days. |
| [`state.json`](state.json) | Current state of each resource, plus the timestamps of recent runs. |
| [`events.jsonl`](events.jsonl) | Append-only audit trail of every uptime state change. |
| [`activity.json`](activity.json) | Latest push and head commit of each resource's GitHub repository. |
| [`activity_history.jsonl`](activity_history.jsonl) | Append-only: one line each time a repository's last push or head commit changes. |
| `daily/YYYY/YYYY-MM-DD.json` | Daily counters and latency histogram per resource. |
| `badges/*.json` | shields.io endpoints for the badges on the main README. |

Raw per-check rows are not kept, so this branch stays small (well under 1 MB a year for 5 resources).

## Do not commit here by hand

Every commit comes from the [`tracker-check`]({ACTIONS_URL}) workflow, squashed to one commit per
UTC day and force-pushed. Hand edits will be overwritten. `main` is never touched by the tracker.

*Generated by `tracker/scripts/summarize.py` at {summary['generated']}.*
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--data", type=Path, required=True)
    args = ap.parse_args()

    resources = load_resources(args.config)
    now = utcnow()
    summary = build_summary(resources, args.data, now)

    write_json(args.data / "summary.json", summary)
    write_json(args.data / "history.json", build_history(resources, args.data, now))
    for name, doc in build_badges(summary).items():
        write_json(args.data / "badges" / f"{name}.json", doc, indent=None)
    (args.data / "STATUS.md").write_text(render_status(summary, read_recent_events(args.data)),
                                         encoding="utf-8")
    (args.data / "README.md").write_text(render_branch_readme(summary), encoding="utf-8")

    t, m = summary["totals"], summary["monitoring"]
    log(f"summary: {summary['resource_count']} resources, up={t['up']} challenged={t['challenged']} "
        f"down={t['down']} unknown={t['unknown']} url_review={t['url_review']} "
        f"monitoring={m['status']} runs_24h={m['runs_last_24h']} "
        f"repos={len(summary['activity']['repos'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
