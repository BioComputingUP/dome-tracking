#!/usr/bin/env python3
"""
notify.py — open a GitHub issue when a resource goes down, and close it when it is back up.

Runs after check.py has updated state.json. Every run reconciles the current state with the
open issues labelled `outage`, so it is idempotent and catches up after a failed run:

  down for >= min_consecutive_down runs, no open issue   -> open one (@mention + assign)
  up or challenged, open issue                           -> comment "back up", close it
  anything else                                          -> nothing

Each issue body carries a hidden marker naming the resource and when it went down.

Issues must be opened by github-actions[bot] (the workflow's GITHUB_TOKEN), never with your own
token: GitHub does not notify you about your own actions, so no email would arrive.

Usage:
    python notify.py --data DIR [--config tracker/resources.yml] [--repo owner/name]
                     [--simulate-down ID] [--dry-run]
    GITHUB_TOKEN and GITHUB_REPOSITORY are read from the environment (set in Actions).

--simulate-down ID treats one resource as down (title prefixed "[test]") without touching the
recorded data; the next normal run then closes the issue. Used to test the notifications.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (DEFAULT_CONFIG, USER_AGENT, iso, load_json, load_resources, log,  # noqa: E402
                    parse_iso, state_path, utcnow)
from summarize import DASHBOARD_URL, DATA_BRANCH_URL  # noqa: E402

API = "https://api.github.com"
TIMEOUT = 20
LABEL = "outage"
LABEL_COLOR = "d73a4a"
MARKER = re.compile(r"<!-- dome-tracker:id=(\S+) since=(\S*) -->")
DEFAULTS = {"mention": [], "assign": [], "min_consecutive_down": 2}


def load_notify_config(config: Path) -> dict:
    with open(config, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return {**DEFAULTS, **(doc.get("notify") or {})}


def marker(rid: str, since: str | None) -> str:
    return f"<!-- dome-tracker:id={rid} since={since or ''} -->"


def parse_marker(body: str | None) -> tuple[str, str | None] | None:
    m = MARKER.search(body or "")
    return (m.group(1), m.group(2) or None) if m else None


def duration(start: datetime, end: datetime) -> str:
    minutes = max(0, round((end - start).total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} days {minutes % 1440 // 60} h"


def cell(text) -> str:
    return str(text if text not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def issue_body(res: dict, st: dict, cfg: dict, simulated: bool) -> str:
    name, n = res.get("name", res["id"]), st.get("consecutive_down", 0)
    lines = []
    if simulated:
        lines += ["> **Test only.** This outage was simulated with `simulate_down`; the site was not "
                  "checked as down. The next normal run closes this issue.", ""]
    lines += [
        f"**{name}** is not responding: it has failed {n} checks in a row, each re-checked after 30 s.",
        "",
        "| | |", "|---|---|",
        f"| URL | {st.get('url') or res['url']} |",
        f"| Down since | {cell(st.get('since'))} (UTC) |",
        f"| Reason | `{cell(st.get('reason'))}` |",
        f"| HTTP code | {cell(st.get('code'))} |",
        f"| Error | {cell(st.get('error'))} |",
        "",
        f"This issue closes itself, with a comment, when {name} responds again. "
        f"[Dashboard]({DASHBOARD_URL}) · [STATUS.md]({DATA_BRANCH_URL}/STATUS.md)",
    ]
    if cfg["mention"]:
        lines += ["", "cc " + " ".join(f"@{u}" for u in cfg["mention"])]
    lines += ["", marker(res["id"], st.get("since"))]
    return "\n".join(lines)


def plan_actions(resources: list[dict], state: dict, open_issues: list[dict], cfg: dict,
                 now: datetime, simulate: str | None = None) -> list[dict]:
    """Pure: what to open and close. open_issues items are {number, id, since}."""
    by_id: dict[str, list[dict]] = {}
    for issue in open_issues:
        by_id.setdefault(issue["id"], []).append(issue)

    actions = []
    for res in resources:
        rid, name = res["id"], res.get("name", res["id"])
        st = state.get(rid)
        if simulate == rid:
            st = {**(st or {}), "state": "down", "since": iso(now), "reason": "simulated", "code": None,
                  "error": "simulated outage (workflow_dispatch input simulate_down)",
                  "consecutive_down": max(cfg["min_consecutive_down"], 1)}
        if not st or st.get("state") not in ("up", "challenged", "down"):
            continue
        issues = by_id.get(rid, [])

        if st["state"] == "down":
            if not issues and st.get("consecutive_down", 0) >= cfg["min_consecutive_down"]:
                actions.append({"action": "open", "id": rid,
                                "title": f"{'[test] ' if simulate == rid else ''}🔴 {name} is down",
                                "body": issue_body(res, st, cfg, simulate == rid),
                                "assignees": list(cfg["assign"])})
            continue

        up_since = parse_iso(st["since"]) if st.get("since") else now
        for issue in issues:
            down_since = parse_iso(issue["since"]) if issue.get("since") else None
            end = up_since if down_since and up_since >= down_since else now
            took = f", after about {duration(down_since, end)} down" if down_since else ""
            actions.append({"action": "close", "id": rid, "number": issue["number"],
                            "comment": f"🟢 **{name} is back up**: responding again at {iso(end)}{took} "
                                       f"(`{st.get('reason')}`). Closing."})
    return actions


class GitHub:
    def __init__(self, repo: str, token: str | None):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method: str, path: str, **kw) -> requests.Response:
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", timeout=TIMEOUT, **kw)
        if r.status_code >= 400 and not (method == "POST" and path == "/labels" and r.status_code == 422):
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r

    def open_issues(self) -> list[dict]:
        r = self._call("GET", "/issues", params={"state": "open", "labels": LABEL, "per_page": 100})
        out = []
        for issue in r.json():
            parsed = parse_marker(issue.get("body"))
            if parsed and "pull_request" not in issue:
                out.append({"number": issue["number"], "id": parsed[0], "since": parsed[1]})
        return out

    def ensure_label(self) -> None:
        self._call("POST", "/labels", json={"name": LABEL, "color": LABEL_COLOR,
                                           "description": "A monitored resource is down (opened by the tracker)"})

    def open_issue(self, title: str, body: str, assignees: list[str]) -> int:
        payload = {"title": title, "body": body, "labels": [LABEL], "assignees": assignees}
        try:
            return self._call("POST", "/issues", json=payload).json()["number"]
        except RuntimeError as exc:
            if not assignees or "HTTP 422" not in str(exc):
                raise
            log(f"notify: could not assign {assignees}; opening without assignees (the @mention still notifies)")
            return self._call("POST", "/issues", json={**payload, "assignees": []}).json()["number"]

    def close_issue(self, number: int, comment: str) -> None:
        self._call("POST", f"/issues/{number}/comments", json={"body": comment})
        self._call("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": "completed"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--data", type=Path, required=True, help="data directory (tracker-data branch checkout)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    ap.add_argument("--simulate-down", metavar="ID", help="treat this resource as down (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print the actions; change nothing")
    args = ap.parse_args()
    if not args.repo:
        ap.error("--repo is required outside GitHub Actions")

    resources = load_resources(args.config)
    if args.simulate_down and args.simulate_down not in {r["id"] for r in resources}:
        ap.error(f"--simulate-down: unknown resource id {args.simulate_down!r}")
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not args.dry_run:
        ap.error("GITHUB_TOKEN is required unless --dry-run")

    gh = GitHub(args.repo, token)
    state = (load_json(state_path(args.data)) or {}).get("resources", {})
    actions = plan_actions(resources, state, gh.open_issues(), load_notify_config(args.config),
                           utcnow(), args.simulate_down)

    if any(a["action"] == "open" for a in actions) and not args.dry_run:
        gh.ensure_label()
    for a in actions:
        if a["action"] == "open":
            number = None if args.dry_run else gh.open_issue(a["title"], a["body"], a["assignees"])
            print(f"opened #{number}: {a['title']}" if number else f"would open: {a['title']}")
        else:
            if not args.dry_run:
                gh.close_issue(a["number"], a["comment"])
            print(f"{'closed' if not args.dry_run else 'would close'} #{a['number']} ({a['id']}): back up")
    if not actions:
        print("no outage issues to open or close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
