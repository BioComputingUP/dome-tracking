import json
from datetime import datetime, timedelta, timezone

from activity import (classify_freshness, collect, parse_repo_response, record,
                      should_append_history)
from common import activity_history_path, activity_path, load_json

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
RESOURCES = [
    {"id": "alpha", "name": "Alpha", "url": "https://alpha.example.org", "repo": "org/alpha"},
    {"id": "beta", "name": "Beta", "url": "https://beta.example.org", "repo": "org/beta"},
    {"id": "gamma", "name": "Gamma (no repo)", "url": "https://gamma.example.org"},
]

REPO_DOC = {"html_url": "https://github.com/org/alpha", "default_branch": "master",
            "pushed_at": "2026-09-24T08:00:00Z", "archived": False}
COMMIT_DOC = {"sha": "0123456789abcdef", "html_url": "https://github.com/org/alpha/commit/0123456",
              "author": {"login": "ana-gh"},
              "commit": {"message": "Add search\n\nLonger body that is dropped.",
                         "author": {"name": "Ana", "date": "2026-09-24T07:59:00Z"}}}


def snap(pushed_at, sha):
    return {"html_url": "x", "default_branch": "main", "pushed_at": pushed_at, "archived": False,
            "last_commit": {"sha": sha, "date": pushed_at, "author": "A", "message": "m", "url": "u"}}


def test_freshness_thresholds():
    assert classify_freshness(None, NOW) == "unknown"
    assert classify_freshness("2026-09-20T12:00:00Z", NOW) == "active"     # 5 days
    assert classify_freshness("2026-09-18T11:00:00Z", NOW) == "recent"     # just over 7 days
    assert classify_freshness("2026-08-26T13:00:00Z", NOW) == "recent"     # just under 30 days
    assert classify_freshness("2026-08-01T00:00:00Z", NOW) == "quiet"


def test_parse_keeps_first_line_and_falls_back_to_login():
    p = parse_repo_response(REPO_DOC, COMMIT_DOC)
    assert p["default_branch"] == "master" and p["pushed_at"] == "2026-09-24T08:00:00Z"
    assert p["last_commit"]["message"] == "Add search"
    assert p["last_commit"]["author"] == "Ana"
    no_name = json.loads(json.dumps(COMMIT_DOC))
    no_name["commit"]["author"]["name"] = None
    assert parse_repo_response(REPO_DOC, no_name)["last_commit"]["author"] == "ana-gh"
    long = json.loads(json.dumps(COMMIT_DOC))
    long["commit"]["message"] = "x" * 300
    assert len(parse_repo_response(REPO_DOC, long)["last_commit"]["message"]) == 120


def test_history_appends_only_on_change():
    a = snap("2026-09-24T08:00:00Z", "aaa")
    assert should_append_history(None, a)
    assert not should_append_history(a, dict(a))
    assert should_append_history(a, snap("2026-09-25T08:00:00Z", "aaa"))   # push to another branch
    assert should_append_history(a, snap("2026-09-24T08:00:00Z", "bbb"))
    assert not should_append_history(None, {"repo": "org/x", "error": "boom"})


def test_error_keeps_previous_snapshot_and_never_raises(tmp_path):
    previous = {"alpha": {"repo": "org/alpha", **snap("2026-09-01T00:00:00Z", "old")}}

    def fetcher(name):
        if name == "org/alpha":
            raise RuntimeError("502 Server Error")
        return snap("2026-09-25T10:00:00Z", "new")

    repos = collect(RESOURCES, previous, NOW, fetcher=fetcher)
    assert set(repos) == {"alpha", "beta"}                      # gamma has no repo
    assert repos["alpha"]["pushed_at"] == "2026-09-01T00:00:00Z"
    assert "502" in repos["alpha"]["error"] and repos["alpha"]["freshness"] == "recent"
    assert repos["beta"]["error"] is None and repos["beta"]["freshness"] == "active"

    record(tmp_path, repos, previous, NOW)
    lines = activity_history_path(tmp_path).read_text().splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["beta"]   # alpha unchanged -> no line
    assert load_json(activity_path(tmp_path))["repos"]["alpha"]["error"]


def test_404_says_the_token_cannot_read_the_repo():
    import requests

    def fetcher(name):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404 Client Error", response=resp)

    repos = collect(RESOURCES[:1], {}, NOW, fetcher=fetcher)
    assert "ACTIVITY_TOKEN" in repos["alpha"]["error"]
    assert repos["alpha"]["freshness"] == "unknown" and "pushed_at" not in repos["alpha"]


def test_second_run_without_changes_appends_nothing(tmp_path):
    fetcher = lambda name: snap("2026-09-25T10:00:00Z", name)  # noqa: E731
    first = collect(RESOURCES, {}, NOW, fetcher=fetcher)
    assert record(tmp_path, first, {}, NOW) == 2
    second = collect(RESOURCES, first, NOW + timedelta(hours=1), fetcher=fetcher)
    assert record(tmp_path, second, first, NOW + timedelta(hours=1)) == 0
    assert len(activity_history_path(tmp_path).read_text().splitlines()) == 2
