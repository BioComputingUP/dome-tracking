from datetime import datetime, timedelta, timezone

import pytest

import check
from check import record
from common import activity_path, daily_path, events_path, load_json, state_path, write_json
from summarize import (build_badges, build_history, build_summary, expected_checks,
                       read_recent_events, render_branch_readme, render_status)

T0 = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
RESOURCES = [
    {"id": "alpha", "name": "Alpha DB", "url": "https://alpha.example.org", "repo": "org/alpha"},
    {"id": "beta", "name": "Beta DB", "url": "https://beta.example.org", "repo": "org/beta"},
]


@pytest.fixture(autouse=True)
def five_minute_interval(monkeypatch):
    """These tests synthesise a check every 5 minutes; record() stamps each daily file
    with the interval in force, and the summary measures coverage against that."""
    monkeypatch.setattr(check, "INTERVAL_MINUTES", 5)


def result(rid, state, code=200, latency=300, reason=None):
    return {"id": rid, "url": f"https://{rid}.example.org", "checked_at": "x", "state": state,
            "reason": reason or ("ok" if state == "up" else f"http_{code}"), "code": code,
            "latency_ms": latency, "final_url": None, "error": None, "url_review": code in (404, 410),
            "tls_warning": None}


def run(data, when, alpha="up", beta="up", beta_code=200):
    record(data, {"alpha": result("alpha", alpha), "beta": result("beta", beta, code=beta_code)}, when)


def test_record_counts_state_and_events(tmp_path):
    run(tmp_path, T0)
    run(tmp_path, T0 + timedelta(minutes=5), beta="down", beta_code=502)
    run(tmp_path, T0 + timedelta(minutes=10), beta="down", beta_code=502)
    run(tmp_path, T0 + timedelta(minutes=15), beta="down", beta_code=503)
    run(tmp_path, T0 + timedelta(minutes=20))

    daily = load_json(daily_path(tmp_path, T0.date()))
    assert daily["runs"] == 5
    assert daily["resources"]["beta"]["checks"] == 5
    assert daily["resources"]["beta"]["down"] == 3
    assert daily["resources"]["alpha"]["latency_hist"][1] == 5  # 300 ms -> <=500 bucket
    assert daily["resources"]["beta"]["latency_sum_ms"] == 600  # down checks excluded from latency

    state = load_json(state_path(tmp_path))["resources"]
    assert state["beta"]["state"] == "up"
    assert state["beta"]["since"] == "2026-09-10T00:20:00Z"
    assert state["beta"]["consecutive_down"] == 0
    assert state["beta"]["first_seen"] == "2026-09-10T00:00:00Z"

    events = [line for line in events_path(tmp_path).read_text().splitlines()]
    # 2 initial state_change + beta up->down + reason 502->503 + beta down->up; repeated identical downs not logged
    assert len(events) == 5
    assert '"down_reason_changed"' in events[3]


def test_expected_checks_respects_first_seen_and_now():
    day = T0.date()
    assert expected_checks(day, T0 + timedelta(days=2), None, 5) == 288
    assert expected_checks(day, T0 + timedelta(hours=1), None, 5) == 12
    assert expected_checks(day, T0 + timedelta(days=2), T0 + timedelta(hours=12), 5) == 144
    assert expected_checks(day, T0, None, 5) == 0
    assert expected_checks(day, T0 + timedelta(days=2), None, 60) == 24


def test_summary_uptime_and_coverage(tmp_path):
    # Two days, one check every 5 minutes, except a 2-hour gap on day 2 (missed cron runs).
    # beta is down for 6 checks (30 min) on day 1.
    t = T0
    end = T0 + timedelta(days=2)
    gap = (T0 + timedelta(days=1, hours=6), T0 + timedelta(days=1, hours=8))
    i = 0
    while t < end:
        if not (gap[0] <= t < gap[1]):
            run(tmp_path, t, beta="down" if 100 <= i < 106 else "up", beta_code=502 if 100 <= i < 106 else 200)
        t += timedelta(minutes=5)
        i += 1

    now = end - timedelta(minutes=1)  # still on day 2
    summary = build_summary(RESOURCES, tmp_path, now)
    rows = {r["id"]: r for r in summary["resources"]}

    beta7 = rows["beta"]["windows"]["7d"]
    assert beta7["checks"] == 576 - 24
    assert beta7["down"] == 6
    assert abs(beta7["uptime"] - (546 / 552)) < 1e-9
    assert abs(beta7["coverage"] - (552 / 576)) < 1e-9
    assert rows["alpha"]["windows"]["7d"]["uptime"] == 1.0

    assert rows["alpha"]["latency_30d"]["p95"] == "≤500 ms"

    md = render_status(summary, [])
    assert "Beta DB" in md and "Alpha DB" in md
    assert "Monitoring is ACTIVE" in md  # summary's last run is `now`
    assert f"| **Resources monitored** | {summary['resource_count']}" in md


def test_unchecked_resource_is_unknown(tmp_path):
    summary = build_summary(RESOURCES, tmp_path, T0)
    assert summary["totals"]["unknown"] == 2
    assert all(r["windows"]["30d"]["uptime"] is None for r in summary["resources"])


def test_monitoring_goes_stale_after_three_missed_intervals(tmp_path):
    run(tmp_path, T0)

    fresh = build_summary(RESOURCES, tmp_path, T0 + timedelta(minutes=30))["monitoring"]
    assert fresh["status"] == "ACTIVE" and fresh["stale"] is False
    assert fresh["runs_total"] == 1

    # INTERVAL_MINUTES is 60 in summarize (check.py's is patched to 5 for these fixtures),
    # so staleness starts after 3 hours without a run.
    late = build_summary(RESOURCES, tmp_path, T0 + timedelta(hours=4))["monitoring"]
    assert late["status"] == "STALE" and late["minutes_since_last_run"] == 240

    md = render_status(build_summary(RESOURCES, tmp_path, T0 + timedelta(hours=4)), [])
    assert "Monitoring is STALE" in md
    assert "stopped" in md  # and says what to do about it


def test_no_data_at_all_is_stale(tmp_path):
    assert build_summary(RESOURCES, tmp_path, T0)["monitoring"]["status"] == "STALE"


def test_history_covers_the_window_and_marks_days_without_checks(tmp_path):
    for i in range(12):  # one hour of 5-minute checks, alpha down for 3 of them
        run(tmp_path, T0 + timedelta(minutes=5 * i), alpha="down" if i < 3 else "up")

    hist = build_history(RESOURCES, tmp_path, T0 + timedelta(days=2))
    alpha = hist["resources"]["alpha"]
    assert hist["days"] == len(alpha["uptime"]) == 90
    assert hist["end"] == "2026-09-12" and hist["start"] == "2026-06-15"
    assert alpha["uptime"][-3] == 75.0 and alpha["checks"][-3] == 12   # the day with data
    assert alpha["uptime"][-1] is None and alpha["checks"][-1] == 0    # days without any check
    assert hist["resources"]["beta"]["uptime"][-3] == 100.0


def test_badges_report_counts_and_staleness(tmp_path):
    run(tmp_path, T0, beta="down", beta_code=502)

    live = build_badges(build_summary(RESOURCES, tmp_path, T0 + timedelta(minutes=5)))
    assert live["monitoring"]["message"] == "active · hourly"
    assert live["monitoring"]["color"] == "brightgreen"
    assert live["down"]["message"] == "1" and live["down"]["color"] == "red"
    assert live["up"]["message"] == "1/2"   # labelled "responding": up + challenged
    assert live["resources"]["message"] == "2"
    assert all(b["schemaVersion"] == 1 for b in live.values())

    stale = build_badges(build_summary(RESOURCES, tmp_path, T0 + timedelta(days=1)))
    assert stale["monitoring"]["color"] == "red"
    assert "stale" in stale["monitoring"]["message"]


def test_url_review_is_called_out_separately_from_down(tmp_path):
    record(tmp_path, {"alpha": result("alpha", "down", code=404),
                      "beta": result("beta", "up")}, T0)
    summary = build_summary(RESOURCES, tmp_path, T0 + timedelta(minutes=5))
    assert summary["totals"]["down"] == 1 and summary["totals"]["url_review"] == 1

    md = render_status(summary, [])
    # the heading carries no count, so the README's anchor link stays valid
    assert "### 🔗 Check URL needs review\n" in md
    assert "**1** return 404/410" in md
    assert "`check.url`" in md  # tells the reader how to fix it


def test_status_page_lists_recent_events_newest_first(tmp_path):
    run(tmp_path, T0)
    run(tmp_path, T0 + timedelta(minutes=5), beta="down", beta_code=502)
    summary = build_summary(RESOURCES, tmp_path, T0 + timedelta(minutes=10))

    events = read_recent_events(tmp_path)
    assert events[0]["id"] == "beta" and events[0]["to"] == "down"
    md = render_status(summary, events)
    assert "## Recent state changes" in md
    assert md.index("Beta DB", md.index("## Recent state changes")) > 0


def test_branch_readme_points_at_the_status_surfaces(tmp_path):
    run(tmp_path, T0)
    readme = render_branch_readme(build_summary(RESOURCES, tmp_path, T0 + timedelta(minutes=5)))
    assert "STATUS.md" in readme and "biocomputingup.github.io/dome-tracking" in readme
    assert "Do not commit here by hand" in readme


def test_recent_runs_are_kept_for_48h_and_counted_over_24h(tmp_path):
    for h in range(0, 72, 2):   # a run every 2 hours for 3 days
        run(tmp_path, T0 + timedelta(hours=h))
    now = T0 + timedelta(hours=70, minutes=5)

    state = load_json(state_path(tmp_path))
    assert state["runs_total"] == 36
    assert len(state["recent_runs"]) == 24          # only the last 48 h are kept
    assert state["recent_runs"][-1] == "2026-09-12T22:00:00Z"

    m = build_summary(RESOURCES, tmp_path, now)["monitoring"]
    assert m["runs_last_24h"] == 12
    assert build_badges(build_summary(RESOURCES, tmp_path, now))["runs-24h"]["color"] == "yellow"
    assert "| **Runs in the last 24 h** | 12" in render_status(build_summary(RESOURCES, tmp_path, now), [])


def test_repo_activity_flows_into_summary_status_and_badge(tmp_path):
    run(tmp_path, T0)
    write_json(activity_path(tmp_path), {"generated": "2026-09-10T00:00:00Z", "repos": {
        "beta": {"repo": "org/beta", "html_url": "https://github.com/org/beta", "default_branch": "main",
                 "pushed_at": "2026-09-09T12:00:00Z", "freshness": "active", "error": None,
                 "last_commit": {"sha": "abcdef1234567", "date": "2026-09-09T11:00:00Z", "author": "Ana",
                                 "message": "Fix the thing", "url": "https://github.com/org/beta/commit/abcdef1"}},
        "alpha": {"repo": "org/alpha", "pushed_at": "2026-06-01T00:00:00Z", "freshness": "quiet",
                  "error": "HTTP 502", "last_commit": {}},
    }})
    summary = build_summary(RESOURCES, tmp_path, T0 + timedelta(minutes=5))

    assert list(summary["activity"]["repos"]) == ["alpha", "beta"]   # resources.yml order
    assert summary["activity"]["repos"]["beta"]["name"] == "Beta DB"
    badge = build_badges(summary)["activity"]
    assert badge["message"] == "1/2" and badge["color"] == "yellow"

    md = render_status(summary, [])
    section = md[md.index("## Repository activity"):md.index("## Needs attention")]
    assert "[`abcdef1`](https://github.com/org/beta/commit/abcdef1) Fix the thing — Ana" in section
    assert "🟢 active" in section and "🔴 quiet" in section and "API error" in section
