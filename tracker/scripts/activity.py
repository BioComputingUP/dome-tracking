#!/usr/bin/env python3
"""
activity.py — record the code activity of each resource's GitHub repository.

For every resource with a `repo:` (owner/name), two GitHub REST calls:

  GET /repos/{repo}                          pushed_at (a push to ANY branch), default branch
  GET /repos/{repo}/commits/{default_branch} head commit: sha, date, author, message

Written into the data directory:

  activity.json            latest snapshot per resource, overwritten every run
  activity_history.jsonl   append-only; one line whenever pushed_at or the head commit changes

A failed API call never fails the run: the resource keeps its previous snapshot, gains an
`error`, and the script still exits 0 so the uptime results are committed regardless.

Usage:
    python activity.py --data DIR [--config tracker/resources.yml] [--dry-run]
    GITHUB_TOKEN is used when set (1,000 requests/h in Actions; 60/h unauthenticated).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (DEFAULT_CONFIG, USER_AGENT, activity_history_path, activity_path,  # noqa: E402
                    iso, load_json, load_resources, log, parse_iso, utcnow, write_json)

API = "https://api.github.com"
TIMEOUT = 20
MESSAGE_MAX = 120
FRESH_DAYS = 7      # pushed within this many days -> "active"
RECENT_DAYS = 30    # within this many -> "recent"; older -> "quiet"


def classify_freshness(pushed_at: str | None, now: datetime) -> str:
    if not pushed_at:
        return "unknown"
    days = (now - parse_iso(pushed_at)).total_seconds() / 86400
    if days < FRESH_DAYS:
        return "active"
    if days < RECENT_DAYS:
        return "recent"
    return "quiet"


def parse_repo_response(repo_doc: dict, commit_doc: dict) -> dict:
    """Pure: pick the fields we keep from the two API responses."""
    commit = commit_doc.get("commit") or {}
    author = commit.get("author") or {}
    message = (commit.get("message") or "").strip().splitlines()
    first_line = message[0] if message else ""
    if len(first_line) > MESSAGE_MAX:
        first_line = first_line[: MESSAGE_MAX - 1] + "…"
    return {
        "html_url": repo_doc.get("html_url"),
        "default_branch": repo_doc.get("default_branch"),
        "pushed_at": repo_doc.get("pushed_at"),
        "archived": bool(repo_doc.get("archived")),
        "last_commit": {
            "sha": commit_doc.get("sha"),
            "date": author.get("date"),
            "author": author.get("name") or (commit_doc.get("author") or {}).get("login"),
            "message": first_line,
            "url": commit_doc.get("html_url"),
        },
    }


def should_append_history(prev: dict | None, new: dict) -> bool:
    if new.get("error") and not new.get("pushed_at"):
        return False
    if not prev:
        return True
    return (prev.get("pushed_at"), (prev.get("last_commit") or {}).get("sha")) != \
           (new.get("pushed_at"), (new.get("last_commit") or {}).get("sha"))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _get(session: requests.Session, path: str) -> dict:
    r = session.get(f"{API}{path}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_repo(session: requests.Session, full_name: str) -> dict:
    repo_doc = _get(session, f"/repos/{full_name}")
    commit_doc = _get(session, f"/repos/{full_name}/commits/{repo_doc['default_branch']}")
    return parse_repo_response(repo_doc, commit_doc)


def collect(resources: list[dict], previous: dict, now: datetime,
            fetcher=None) -> dict[str, dict]:
    """One snapshot per resource; on error, carry the previous snapshot forward with `error` set."""
    if fetcher is None:
        session = _session()
        fetcher = lambda name: fetch_repo(session, name)  # noqa: E731
    out = {}
    for res in resources:
        full_name = res.get("repo")
        if not full_name:
            continue
        try:
            snap = {"repo": full_name, **fetcher(full_name), "error": None}
        except Exception as exc:  # noqa: BLE001 — never fail the run over one repo
            log(f"activity: {full_name}: {exc}")
            snap = {**(previous.get(res["id"]) or {"repo": full_name}), "error": str(exc)[:200]}
        snap["fetched_at"] = iso(now)
        snap["freshness"] = classify_freshness(snap.get("pushed_at"), now)
        out[res["id"]] = snap
    return out


def record(data_dir: Path, repos: dict[str, dict], previous: dict, now: datetime) -> int:
    write_json(activity_path(data_dir), {"generated": iso(now), "repos": repos})
    lines = []
    for rid, snap in repos.items():
        if should_append_history(previous.get(rid), snap):
            c = snap.get("last_commit") or {}
            lines.append({"ts": iso(now), "id": rid, "repo": snap["repo"],
                          "pushed_at": snap.get("pushed_at"), "sha": c.get("sha"),
                          "date": c.get("date"), "author": c.get("author"),
                          "message": c.get("message")})
    if lines:
        with open(activity_history_path(data_dir), "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return len(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--data", type=Path, help="data directory (tracker-data branch checkout)")
    ap.add_argument("--dry-run", action="store_true", help="print results as JSON; write nothing")
    args = ap.parse_args()
    if not args.dry_run and not args.data:
        ap.error("--data is required unless --dry-run")

    now = utcnow()
    previous = {}
    if args.data:
        previous = (load_json(activity_path(args.data)) or {}).get("repos", {})
    repos = collect(load_resources(args.config), previous, now)

    if args.dry_run:
        print(json.dumps(repos, indent=1))
    else:
        n = record(args.data, repos, previous, now)
        log(f"activity: {len(repos)} repos, {n} history lines appended")
    for rid, s in repos.items():
        print(f"  {rid}: {s.get('freshness')} pushed_at={s.get('pushed_at')}"
              f"{' ERROR ' + s['error'] if s.get('error') else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
