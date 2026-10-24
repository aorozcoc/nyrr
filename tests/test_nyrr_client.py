"""HTTP client policy: retries, Retry-After, 401/403 stop, pacing, budget, authorization gate."""

import httpx
import pytest

from src.nyrr_client import (AccessDenied, CollectionNotAuthorized, NyrrClient, RateLimited, RequestBudgetExceeded,
                             SourceError)


def client(handler, clock, **kw):
    return NyrrClient(base_url="https://prod.nyrr.org", user_agent="ua-test", authorized=kw.pop("authorized", True),
                      transport=httpx.MockTransport(handler), sleep=clock.sleep, monotonic=clock.monotonic,
                      now_fn=clock, **kw)


def html(text="<html></html>"):
    return httpx.Response(200, headers={"content-type": "text/html"}, text=text)


def test_requires_authorization(clock):
    with pytest.raises(CollectionNotAuthorized):
        client(lambda r: html(), clock, authorized=False)


def test_user_agent_and_success(clock):
    seen = []
    c = client(lambda r: seen.append(r.headers["user-agent"]) or html("ok"), clock)
    body, url, fetched = c.get_html("/x")
    assert body == "ok" and url == "https://prod.nyrr.org/x" and fetched == clock()
    assert seen == ["ua-test"]


@pytest.mark.parametrize("code", [401, 403])
def test_access_denied_is_not_retried(code, clock):
    calls = []
    c = client(lambda r: calls.append(1) or httpx.Response(code), clock)
    with pytest.raises(AccessDenied):
        c.get_html("/x")
    assert len(calls) == 1


def test_5xx_bounded_retries_with_backoff(clock):
    calls = []
    c = client(lambda r: calls.append(1) or httpx.Response(503), clock)
    with pytest.raises(SourceError):
        c.get_html("/x")
    assert len(calls) == 3
    assert 2 in clock.sleeps and 4 in clock.sleeps


def test_5xx_then_success(clock):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(502) if len(calls) == 1 else html("ok")
    assert client(h, clock).get_html("/x")[0] == "ok"


def test_429_honours_retry_after(clock):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"}) if len(calls) == 1 else html("ok")
    assert client(h, clock).get_html("/x")[0] == "ok"
    assert 7 in clock.sleeps


def test_429_with_long_retry_after_gives_up(clock):
    c = client(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}), clock)
    with pytest.raises(RateLimited):
        c.get_html("/x")


def test_timeouts_are_source_errors_never_success(clock):
    def h(r):
        raise httpx.ReadTimeout("slow", request=r)
    with pytest.raises(SourceError):
        client(h, clock).get_html("/x")


def test_unexpected_status_and_content_type(clock):
    with pytest.raises(SourceError):
        client(lambda r: httpx.Response(404), clock).get_html("/x")
    with pytest.raises(SourceError):
        client(lambda r: httpx.Response(200, headers={"content-type": "application/json"}, text="{}"),
               clock).get_html("/x")


def test_offsite_urls_refused(clock):
    with pytest.raises(SourceError):
        client(lambda r: html(), clock).get_html("https://evil.example.com/x")


def test_pacing_and_request_budget(clock):
    c = client(lambda r: html(), clock, request_delay=2.0, max_requests=3)
    for _ in range(3):
        c.get_html("/x")
    assert clock.sleeps.count(2.0) == 2
    with pytest.raises(RequestBudgetExceeded):
        c.get_html("/x")


def test_review4_5xx_with_long_retry_after_stops_immediately(clock):
    calls = []
    c = client(lambda r: calls.append(1) or httpx.Response(503, headers={"Retry-After": "120"}), clock)
    with pytest.raises(RateLimited):
        c.get_html("/x")
    assert len(calls) == 1 and 2 not in clock.sleeps
