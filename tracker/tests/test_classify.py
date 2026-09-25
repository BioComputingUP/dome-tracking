import pytest

from check import classify, detect_challenge
from common import effective_url


@pytest.mark.parametrize("fixture,code,headers,state,reason", [
    ("cloudflare_challenge.html", 403, {"Server": "cloudflare", "cf-mitigated": "challenge"}, "challenged", "challenge:cloudflare"),
    ("cloudflare_challenge.html", 503, {"Server": "cloudflare"}, "challenged", "challenge:cloudflare"),
    ("anubis_challenge.html", 200, {}, "challenged", "challenge:anubis"),
    ("cloudflare_502.html", 502, {"Server": "cloudflare"}, "down", "http_502"),
    ("not_found.html", 404, {}, "down", "http_404"),
    ("normal_page.html", 200, {}, "up", "ok"),
])
def test_classify_fixtures(fixture_text, fixture, code, headers, state, reason):
    got = classify(code, headers, fixture_text(fixture))
    assert got["state"] == state
    assert got["reason"] == reason


def test_404_and_410_flag_url_review():
    assert classify(404)["url_review"] is True
    assert classify(410)["url_review"] is True
    assert classify(500)["url_review"] is False


@pytest.mark.parametrize("code", [200, 204, 301, 302])
def test_success_and_redirect_codes_are_up(code):
    assert classify(code)["state"] == "up"


@pytest.mark.parametrize("code", [401, 403, 405, 429])
def test_restricted_4xx_counts_as_up(code):
    got = classify(code)
    assert got["state"] == "up"
    assert got["reason"] == f"http_{code}"


@pytest.mark.parametrize("code", [500, 502, 503, 504, 520, 522])
def test_5xx_is_down(code):
    assert classify(code)["state"] == "down"


@pytest.mark.parametrize("kind", ["connect_timeout", "read_timeout", "dns_error", "tls_error",
                                  "connection_refused", "redirect_loop"])
def test_transport_errors_are_down(kind):
    got = classify(None, error_kind=kind)
    assert got == {"state": "down", "reason": kind, "url_review": False}


def test_ddos_guard_header_only_with_blocking_status():
    assert detect_challenge(403, {"server": "ddos-guard"}, "") == "ddos-guard"
    assert detect_challenge(200, {"server": "ddos-guard"}, "<html>ok</html>") is None


def test_normal_page_mentioning_robot_is_not_a_challenge(fixture_text):
    assert detect_challenge(200, {}, fixture_text("normal_page.html")) is None


def test_effective_url_prefers_the_check_override():
    """`url` is owned by the spreadsheet import; a hand-fixed address goes in `check.url`."""
    listed = {"id": "x", "url": "https://old.example.org/"}
    assert effective_url(listed) == "https://old.example.org/"
    assert effective_url({**listed, "check": {"verify_tls": False}}) == "https://old.example.org/"
    assert effective_url({**listed, "check": {"url": "https://new.example.org/"}}) == "https://new.example.org/"
