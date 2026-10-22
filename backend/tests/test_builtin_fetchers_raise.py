"""Built-in fetchers raise on a failed fetch instead of returning ``[]``.

NVD, CISA KEV and Wikipedia On This Day used to log a failed fetch and
return an empty list, so the scheduler recorded a success with nothing
new: the feed looked quiet while it was blocked or down. They now log
and re-raise, so ``_classify_error`` labels the feed (blocked, upstream
error, network, ...). GitHub releases reads several repos: one repo's
304 or failure still only leaves that repo out, and ``fetch`` raises
only when every repo failed.
"""

from __future__ import annotations

import httpx
import pytest

from app import scheduler
from app.sources import cisa_kev, github_releases, nvd, wikipedia_on_this_day
from app.sources.cisa_kev import CisaKev
from app.sources.github_releases import GithubReleases
from app.sources.nvd import NvdRecent
from app.sources.wikipedia_on_this_day import WikipediaOnThisDay

pytestmark = pytest.mark.no_db


@pytest.fixture
def respond(monkeypatch):
    """Route every request through ``handler``, which the test sets. The
    built-ins share the ``httpx`` module, so one patch covers them all."""
    real_client = httpx.AsyncClient
    state: dict = {}

    def client(**kwargs):
        # No DNS in tests: drop the SSRF hook (pinned elsewhere).
        kwargs.pop("event_hooks", None)
        return real_client(transport=httpx.MockTransport(state["handler"]), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    def _respond(handler) -> None:
        state["handler"] = handler

    return _respond


def _status(code: int):
    return lambda request: httpx.Response(code, content=b"nope")


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"<html>captcha</html>")


BUILTINS = [NvdRecent, CisaKev, WikipediaOnThisDay]
IDS = ["nvd", "cisa_kev", "wikipedia_on_this_day"]


def test_the_three_share_the_patched_httpx_module():
    # The fixture patches ``httpx.AsyncClient`` once; each module must
    # build its client from that same module.
    assert nvd.httpx is cisa_kev.httpx is wikipedia_on_this_day.httpx is httpx


@pytest.mark.parametrize("plugin", BUILTINS, ids=IDS)
@pytest.mark.parametrize(
    "code,kind",
    [(403, "blocked"), (402, "blocked"), (404, "not found"), (503, "upstream error"), (429, "http error")],
)
async def test_an_http_error_is_raised_and_labelled(respond, plugin, code, kind):
    respond(_status(code))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await plugin().fetch()
    assert scheduler._classify_error(excinfo.value) == kind


@pytest.mark.parametrize("plugin", BUILTINS, ids=IDS)
async def test_a_network_error_is_raised(respond, plugin):
    respond(_refused)
    with pytest.raises(httpx.ConnectError) as excinfo:
        await plugin().fetch()
    assert scheduler._classify_error(excinfo.value) == "network"


@pytest.mark.parametrize("plugin", BUILTINS, ids=IDS)
async def test_a_body_that_is_not_json_is_raised(respond, plugin):
    respond(_not_json)
    with pytest.raises(ValueError):
        await plugin().fetch()


# ---------------------------------------------------------------------------
# GitHub releases: raises only when every repo failed
# ---------------------------------------------------------------------------

REPOS = ["octo/one", "octo/two"]


def _release(repo: str) -> bytes:
    return (
        b'[{"name": "v1", "tag_name": "v1", "html_url": "https://github.com/'
        + repo.encode()
        + b'/releases/v1", "published_at": "2026-10-19T00:00:00Z"}]'
    )


@pytest.fixture
def github(monkeypatch, respond):
    monkeypatch.setattr(GithubReleases, "repos", list(REPOS))
    monkeypatch.setattr(github_releases, "_etag_cache", {})

    def _per_repo(by_repo: dict) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            repo = "/".join(request.url.path.split("/")[2:4])
            outcome = by_repo[repo]
            if callable(outcome):
                return outcome(request)
            return outcome

        respond(handler)

    return _per_repo


async def test_one_repo_failing_leaves_only_that_repo_out(github):
    github({"octo/one": httpx.Response(500), "octo/two": httpx.Response(200, content=_release("octo/two"))})
    items = await GithubReleases().fetch()
    assert [i["meta"]["repo"] for i in items] == ["octo/two"]


async def test_every_repo_not_modified_is_nothing_new(github):
    github({"octo/one": httpx.Response(304), "octo/two": httpx.Response(304)})
    assert await GithubReleases().fetch() == []


async def test_one_repo_not_modified_and_one_failing_is_nothing_new(github):
    # A 304 is not a failure, so not every repo failed.
    github({"octo/one": httpx.Response(304), "octo/two": _refused})
    assert await GithubReleases().fetch() == []


async def test_every_repo_failing_raises_the_first_repos_error(github):
    github({"octo/one": httpx.Response(403, content=b"rate limited"), "octo/two": _refused})
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await GithubReleases().fetch()
    assert excinfo.value.response.status_code == 403
    assert scheduler._classify_error(excinfo.value) == "blocked"


async def test_every_repo_unreachable_is_a_network_error(github):
    github({"octo/one": _refused, "octo/two": _refused})
    with pytest.raises(httpx.ConnectError) as excinfo:
        await GithubReleases().fetch()
    assert scheduler._classify_error(excinfo.value) == "network"


async def test_every_repo_answering_garbage_raises(github):
    github({"octo/one": _not_json, "octo/two": _not_json})
    with pytest.raises(ValueError):
        await GithubReleases().fetch()
