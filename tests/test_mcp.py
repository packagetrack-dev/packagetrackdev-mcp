"""`packagetrackdev-mcp`: the archive as tools for a coding agent."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from packagetrackdev_mcp import config, messages
from packagetrackdev_mcp import server as mcp


def test_started_by_hand(monkeypatch, capsys):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(mcp.sys, "stdin", Tty())
    assert mcp.serve(config.Config(api_key=None, server="https://pt.test")) == 2
    err = capsys.readouterr().err
    assert "started by your coding agent" in err
    assert mcp.AGENT_ADD_COMMAND in err


def test_started_by_agent(monkeypatch):
    class Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr(mcp.sys, "stdin", Pipe())
    assert not mcp.started_by_hand()
    monkeypatch.setattr(mcp.sys, "stdin", None)
    assert not mcp.started_by_hand()


def test_config_precedence(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('api_key = "pkgt_file"\nserver = "https://file.test/"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv(config.ENV_KEY, raising=False)
    monkeypatch.delenv(config.ENV_SERVER, raising=False)
    assert config.load() == config.Config(
        api_key="pkgt_file", server="https://file.test"
    )
    monkeypatch.setenv(config.ENV_KEY, "pkgt_env")
    assert config.load().api_key == "pkgt_env"
    assert config.load(api_key="pkgt_flag").api_key == "pkgt_flag"


def test_config_missing_or_broken_file(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_KEY, raising=False)
    monkeypatch.delenv(config.ENV_SERVER, raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "missing.toml")
    assert config.load() == config.Config(api_key=None, server=config.DEFAULT_SERVER)
    broken = tmp_path / "broken.toml"
    broken.write_text("api_key = ")
    monkeypatch.setattr(config, "CONFIG_PATH", broken)
    assert config.load().api_key is None


def _server(handler, api_key=None):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    settings = config.Config(api_key=api_key, server="https://pt.test")
    return mcp.build_server(settings, client), seen


def _ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


async def _text(server, tool, **args) -> str:
    result = await server.call_tool(tool, args)
    return result.content[0].text


async def _refused(server, tool, **args) -> str:
    """Return what a refused call says; the client gets it with isError=true."""
    with pytest.raises(ToolError) as caught:
        await server.call_tool(tool, args)
    prefix = f"Error executing tool {tool}: "
    text = str(caught.value)
    assert text.startswith(prefix)
    return text.removeprefix(prefix)


CHANGES = {
    "state": "ok",
    "package": {"name": "httpx", "latest_stable_version": "0.28.1"},
    "since": {
        "version": "0.24.1",
        "known": True,
        "yanked": False,
        "yanked_reason": None,
    },
    "until": None,
    "versions": [
        {
            "version": "0.25.0",
            "published_at": "2023-09-11T00:00:00+00:00",
            "is_prerelease": False,
            "yanked": False,
            "yanked_reason": None,
            "notes": [
                {
                    "text": "## 0.25.0 (11th Sep, 2023)\n\n### Removed\n\n* Drop 3.7.",
                    "url": "https://example.test/CHANGELOG.md",
                    "confirmed_by": 2,
                    "primary": True,
                }
            ],
        },
        {
            "version": "0.25.1",
            "published_at": None,
            "is_prerelease": False,
            "yanked": False,
            "yanked_reason": None,
            "notes": [],
        },
        {
            "version": "0.25.2",
            "published_at": None,
            "is_prerelease": False,
            "yanked": False,
            "yanked_reason": None,
            "notes": [],
        },
        {
            "version": "0.26.0",
            "published_at": None,
            "is_prerelease": False,
            "yanked": True,
            "yanked_reason": "broken wheel",
            "notes": [],
        },
    ],
    "total": 4,
    "truncated": False,
    "next_since": None,
}


async def test_list_tools():
    server, _ = _server(lambda request: _ok({}))
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == {
        "package_changes",
        "package_versions",
        "search_packages",
        "list_projects",
        "project_report",
        "import_project",
        "update_status",
    }
    for name in ("list_projects", "project_report", "import_project"):
        assert "API key" in tools[name].description
    for name in ("package_changes", "package_versions", "search_packages"):
        assert "API key" not in tools[name].description
    schema = tools["package_changes"].input_schema
    assert schema["properties"]["ecosystem"]["enum"] == [
        "cargo",
        "composer",
        "go",
        "maven",
        "npm",
        "nuget",
        "pub",
        "pypi",
    ]


async def test_package_changes_request():
    server, seen = _server(lambda request: _ok(CHANGES))
    await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/api/public/changes/pypi/httpx"
    assert dict(request.url.params) == {"since": "0.24.1"}
    assert "authorization" not in request.headers


async def test_package_changes_rendering():
    server, _ = _server(lambda request: _ok(CHANGES))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "Releases after 0.24.1 up to 0.26.0: 4, oldest first." in text
    assert "Latest stable release: 0.28.1." in text
    assert text.count("0.25.0") == 1
    assert "### Removed" in text
    assert "Source: https://example.test/CHANGELOG.md" in text


async def test_package_changes_folds_silent_versions():
    server, _ = _server(lambda request: _ok(CHANGES))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "## 0.25.1 to 0.25.2\nNo release notes found for these 2 versions." in text


async def test_package_changes_withdrawn_release():
    server, _ = _server(lambda request: _ok(CHANGES))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "## 0.26.0\nWITHDRAWN by the maintainers (broken wheel)." in text


async def test_package_changes_withdrawn_since():
    payload = json.loads(json.dumps(CHANGES))
    payload["since"].update(yanked=True, yanked_reason="CVE-2024-0001")
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "The installed version 0.24.1 was withdrawn" in text
    assert "CVE-2024-0001" in text
    assert "package_versions shows which releases are not withdrawn" in text
    assert "not optional" not in text


async def test_package_changes_page_link():
    server, _ = _server(lambda request: _ok(CHANGES))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "Page: https://pt.test/pypi/httpx" in text


async def test_package_changes_secondary_notes():
    payload = json.loads(json.dumps(CHANGES))
    payload["versions"][0]["notes"].append(
        {"text": "different text", "url": None, "confirmed_by": 1, "primary": False}
    )
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "confirmed by 2 sources" in text
    assert "1 more distinct note(s) at https://pt.test/pypi/httpx" in text


async def test_package_changes_paging():
    payload = json.loads(json.dumps(CHANGES))
    payload.update(total=63, truncated=True, next_since="0.26.0")
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "63, showing the first 4" in text
    assert 'Call again with since="0.26.0" for the rest.' in text


async def test_package_changes_cut_note():
    payload = json.loads(json.dumps(CHANGES))
    note = payload["versions"][0]["notes"][0]
    note.update(text="x" * 3_000, cut=True, chars=3_500)
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "[cut after 3,000 characters, 500 more." in text
    assert 'until="0.25.0" and a larger max_chars' in text
    assert "Full text: https://example.test/CHANGELOG.md" in text


async def test_package_changes_max_chars():
    server, seen = _server(lambda request: _ok(CHANGES))
    await _text(
        server,
        "package_changes",
        ecosystem="pypi",
        name="httpx",
        since="0.24.1",
        max_chars=12_000,
    )
    assert seen[0].url.params["max_chars"] == "12000"


async def test_package_changes_a_downgrade_is_not_nothing_to_change():
    payload = {
        **CHANGES,
        "since": {**CHANGES["since"], "version": "0.28.1"},
        "until": {"version": "0.24.1", "known": True, "older_than_since": True},
        "versions": [],
        "total": 0,
    }
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server,
        "package_changes",
        ecosystem="pypi",
        name="httpx",
        since="0.28.1",
        until="0.24.1",
    )
    assert "0.24.1 is older than 0.28.1: that is a downgrade." in text
    assert 'since="0.24.1" and until="0.28.1"' in text
    assert "Nothing to change" not in text


async def test_package_changes_on_a_prerelease_says_newer_prereleases_exist():
    payload = {
        **CHANGES,
        "package": {"name": "pydantic", "latest_stable_version": "2.13.5"},
        "since": {**CHANGES["since"], "version": "2.14.0b1"},
        "versions": [],
        "total": 0,
        "prereleases_left_out": 1,
    }
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="pydantic", since="2.14.0b1"
    )
    assert "Releases after 2.14.0b1: no stable release. 1 prerelease(s)" in text
    assert "include_prereleases=true" in text
    assert "up to 2.13.5" not in text, "an older stable release is not a bound"
    assert "Nothing to change" not in text


async def test_package_changes_with_prereleases_names_the_real_end_of_the_range():
    payload = {
        **CHANGES,
        "package": {"name": "pydantic", "latest_stable_version": "2.13.5"},
        "since": {**CHANGES["since"], "version": "2.14.0b1"},
        "versions": [
            {
                "version": "2.14.0b2",
                "published_at": None,
                "is_prerelease": True,
                "yanked": False,
                "yanked_reason": None,
                "notes": [],
            }
        ],
        "total": 1,
        "prereleases_left_out": 0,
    }
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server,
        "package_changes",
        ecosystem="pypi",
        name="pydantic",
        since="2.14.0b1",
        include_prereleases=True,
    )
    assert "Releases after 2.14.0b1 up to 2.14.0b2: 1, oldest first." in text
    assert "up to 2.13.5" not in text


async def test_package_changes_says_prereleases_were_left_out_of_a_range():
    payload = {**CHANGES, "prereleases_left_out": 2}
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "2 prerelease(s) in this range were left out" in text


@pytest.mark.parametrize(("asked", "sent"), [(50_000, "20000"), (50, "200")])
async def test_package_changes_max_chars_outside_the_bounds_is_adjusted_and_said(
    asked, sent
):
    server, seen = _server(lambda request: _ok(CHANGES))
    text = await _text(
        server,
        "package_changes",
        ecosystem="pypi",
        name="httpx",
        since="0.24.1",
        max_chars=asked,
    )
    assert seen[0].url.params["max_chars"] == sent
    assert text.startswith(f"max_chars={asked:,} is outside what the server accepts")
    assert "422" not in text


async def test_package_changes_unknown_since_from_an_older_server():
    """A server older than `readable` cannot tell an unreadable `since` from
    one above every release; the sentence covers both."""
    payload = {
        "state": "ok",
        "package": {"name": "httpx", "latest_stable_version": "0.28.1"},
        "since": {"version": "garbage", "known": False, "yanked": False},
        "until": None,
        "versions": [],
        "total": 0,
        "truncated": False,
        "next_since": None,
    }
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="garbage"
    )
    assert "garbage is not a version this archive holds" in text
    assert "package_versions lists what exists" in text


def _changes(since: dict, until: dict | None = None, versions=None, total=0):
    return {
        "state": "ok",
        "package": {"name": "requests", "latest_stable_version": "2.34.2"},
        "since": since,
        "until": until,
        "versions": versions or [],
        "total": total,
        "truncated": False,
        "next_since": None,
    }


@pytest.mark.parametrize(
    ("args", "payload", "said"),
    [
        (
            {"since": "not-a-version"},
            _changes({"version": "not-a-version", "known": False, "readable": False}),
            "'not-a-version' is not a PyPI version, so it cannot bound the release "
            "history of requests. Pass the version installed now as since;",
        ),
        (
            {"since": "2.32.3", "until": "garbage"},
            _changes(
                {"version": "2.32.3", "known": True, "readable": True},
                {"version": "garbage", "known": False, "readable": False},
            ),
            "'garbage' is not a PyPI version, so it cannot bound the release "
            "history of requests. Pass the version to upgrade to, or leave it out "
            "for everything newer as until;",
        ),
    ],
    ids=["since", "until"],
)
async def test_a_bound_that_is_no_version_is_refused(args, payload, said):
    """The call's mistake, refused. An unreadable `until`
    used to end in "Nothing to change" over seven releases."""
    server, _ = _server(lambda request: _ok(payload))
    text = await _refused(
        server, "package_changes", ecosystem="pypi", name="requests", **args
    )
    assert text.startswith(said)
    assert "Nothing to change" not in text


async def test_a_since_above_every_release_held_is_an_answer_not_a_refusal():
    """The archive has not caught up with the registry: said, with the
    newest release it holds, and never "could not be placed" or "nothing to
    change"."""
    payload = _changes({"version": "99.0.0", "known": False, "readable": True})
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="requests", since="99.0.0"
    )
    assert (
        "No release this archive holds is newer than it (newest held: 2.34.2). "
        "The archive may not have caught up with PyPI yet; package_versions "
        "shows what it holds."
    ) in text
    assert "could not be placed" not in text
    assert "Nothing to change" not in text


async def test_an_empty_since_is_refused_before_any_request():
    server, seen = _server(lambda request: _ok({}))
    text = await _refused(
        server, "package_changes", ecosystem="pypi", name="requests", since="  "
    )
    assert text.startswith("since is empty.")
    assert "422" not in text
    assert seen == []


async def test_an_empty_until_is_read_as_left_out():
    payload = _changes(
        {"version": "2.32.3", "known": True, "readable": True},
        versions=[{"version": "2.32.4", "notes": []}],
        total=1,
    )
    server, seen = _server(lambda request: _ok(payload))
    await _text(
        server,
        "package_changes",
        ecosystem="pypi",
        name="requests",
        since="2.32.3",
        until="",
    )
    assert "until" not in seen[0].url.params


async def test_unheld_package_requested():
    def handle(request):
        if request.method == "POST":
            return _ok({"state": "scanning", "name": "left-pad", "purl_type": "npm"})
        return _ok({"state": "unknown", "name": "left-pad", "purl_type": "npm"})

    server, seen = _server(handle)
    text = await _text(
        server, "package_changes", ecosystem="npm", name="left-pad", since="1.0.0"
    )
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/public/changes/npm/left-pad"),
        ("POST", "/api/public/scan/npm/left-pad"),
    ]
    assert "did not hold left-pad yet; it has been asked to fetch it" in text


@pytest.mark.parametrize("state", ["scanning", "unknown"])
async def test_a_package_being_fetched_promises_no_time(state):
    """A lookup waits behind every scan queued before it, seconds or twenty
    minutes; "ask again in a minute" was a promise the queue did not keep."""

    def handle(request):
        if request.method == "POST":
            return _ok({"state": "scanning", "name": "left-pad", "purl_type": "npm"})
        return _ok({"state": state, "name": "left-pad", "purl_type": "npm"})

    server, _ = _server(handle)
    text = await _text(server, "package_versions", ecosystem="npm", name="left-pad")

    assert text.endswith("Ask again when that finishes.")
    assert "minute" not in text


@pytest.mark.parametrize(
    ("days", "said"),
    [(0, "pushed today"), (1, "pushed 1 day ago"), (5, "pushed 5 days ago")],
)
async def test_how_long_ago_a_project_was_pushed_reads_as_english(days, said):
    listing = {
        "projects": [
            {
                "name": "api",
                "purl_type": "pypi",
                "total": 3,
                "direct": 3,
                "measured": 3,
                "behind_direct": 0,
                "snapshot_age_days": days,
            }
        ]
    }
    server, _ = _server(lambda request: _ok(listing), api_key="pkgt_x")
    text = await _text(server, "list_projects")

    assert said in text
    assert "0 days" not in text and "1 days" not in text


async def test_not_found_package():
    unheld = {"state": "not_found", "name": "left-pad", "purl_type": "npm"}
    server, seen = _server(lambda request: _ok(unheld))
    text = await _text(server, "package_versions", ecosystem="npm", name="left-pad")
    assert text == "npm has no package named left-pad."
    assert [r.method for r in seen] == ["GET"], "nothing to ask for"


DEPRECATED = {
    "notice": (
        "request has been deprecated, see "
        "https://github.com/request/request/issues/3142"
    ),
    "replacement": None,
}


def _versions(deprecation) -> dict:
    return {
        "state": "ok",
        "package": {
            "name": "request",
            "summary": "Simplified HTTP request client.",
            "latest_stable_version": None,
            "deprecation": deprecation,
        },
        "versions": [
            {
                "version": "2.88.2",
                "published_at": None,
                "is_prerelease": False,
                "yanked": True,
            }
        ],
        "has_older": False,
    }


async def test_package_versions_says_the_registry_deprecated_the_package():
    server, _ = _server(lambda request: _ok(_versions(DEPRECATED)))
    text = await _text(server, "package_versions", ecosystem="npm", name="request")
    assert (
        "DEPRECATED on npm: request has been deprecated, see "
        "https://github.com/request/request/issues/3142" in text
    )
    assert "issues/3142\nIt still installs" in text
    assert text.index("DEPRECATED") < text.index("Releases, newest first:")
    assert "replacement" not in text, "the registry named none, so we name none"
    assert "—" not in text


async def test_a_notice_without_a_full_stop_keeps_its_own_line():
    payload = _versions({"notice": "Abandoned by its maintainers", "replacement": None})
    payload["package"]["name"] = "fzaninotto/faker"
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_versions", ecosystem="composer", name="fzaninotto/faker"
    )
    assert (
        "DEPRECATED on Packagist: Abandoned by its maintainers\nIt still installs"
        in text
    )
    assert "Abandoned by its maintainers." not in text


async def test_package_versions_names_the_registry_replacement():
    deprecation = {
        "notice": "Moved.",
        "replacement": {
            "purl_type": "npm",
            "name": "@scope/next",
            "path": "/npm/@scope/next",
        },
    }
    server, _ = _server(lambda request: _ok(_versions(deprecation)))
    text = await _text(server, "package_versions", ecosystem="npm", name="request")
    assert (
        "The registry names @scope/next (npm) as its replacement: "
        "https://pt.test/npm/@scope/next" in text
    )


async def test_package_changes_says_the_registry_deprecated_the_package():
    payload = {**CHANGES, "package": {**CHANGES["package"], "deprecation": DEPRECATED}}
    server, _ = _server(lambda request: _ok(payload))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "DEPRECATED on PyPI: request has been deprecated" in text
    assert text.index("DEPRECATED") < text.index("Releases after 0.24.1")


async def test_no_deprecation_line_without_a_notice():
    server, _ = _server(lambda request: _ok(CHANGES))
    text = await _text(
        server, "package_changes", ecosystem="pypi", name="httpx", since="0.24.1"
    )
    assert "DEPRECATED" not in text
    server, _ = _server(lambda request: _ok(_versions(None)))
    assert "DEPRECATED" not in await _text(
        server, "package_versions", ecosystem="npm", name="request"
    )


async def test_project_tools_without_key():
    server, seen = _server(lambda request: _ok({}))
    text = await _refused(server, "list_projects")
    assert "needs a PackageTrack API key" in text
    assert "https://pt.test/app/keys" in text
    assert "https://pt.test/register" in text
    assert "waitlist" not in text
    assert seen == [], "no key, so no request was worth making"


async def test_list_projects_rendering():
    listing = {
        "projects": [
            {
                "name": "api",
                "purl_type": "pypi",
                "total": 120,
                "direct": 30,
                "measured": 110,
                "unknown_package": 10,
                "behind_direct": 4,
                "yanked": 0,
                "unpinned": 0,
                "snapshot_age_days": 2,
                "paused": True,
            }
        ]
    }
    server, _ = _server(lambda request: _ok(listing), api_key="pkgt_x")
    text = await _text(server, "list_projects")
    assert "120 packages, 30 direct, 110 compared against the archive" in text
    assert "4 of yours behind, 10 not in the archive" in text
    assert "PAUSED: over the account's project limit" in text


async def test_project_report_empty():
    report = {"project": "api", "purl_type": "pypi", "withdrawn": [], "packages": []}
    server, _ = _server(lambda request: _ok(report), api_key="pkgt_x")
    text = await _text(server, "project_report", name="api")
    assert "among the dependencies the archive holds" in text
    assert "Every dependency is current" not in text


async def test_search_packages_total():
    rows = [{"name": f"p{i}", "purl_type": "npm"} for i in range(60)]
    server, _ = _server(lambda request: _ok({"rows": rows, "total": 339}))
    text = await _text(server, "search_packages", query="http")
    assert text.startswith("339 packages match 'http':")
    assert "...and 319 more on https://pt.test/search?q=http" in text


async def test_rejected_key():
    server, seen = _server(
        lambda request: httpx.Response(
            401, json={"detail": "Missing or invalid API key."}
        ),
        api_key="pkgt_x",
    )
    text = await _refused(server, "project_report", name="api")
    assert text.startswith(
        "https://pt.test rejected the API key (HTTP 401): Missing or invalid API key. "
    )
    assert "https://pt.test/app/keys" in text
    assert "none is configured" not in text
    assert "pkgt_x" not in text
    assert seen[0].headers["authorization"] == "Bearer pkgt_x"


async def test_http_403():
    detail = "This key cannot reach that organization."
    server, _ = _server(
        lambda request: httpx.Response(403, json={"detail": detail}),
        api_key="pkgt_x",
    )
    text = await _refused(server, "list_projects")
    assert text == f"https://pt.test answered HTTP 403: {detail}"
    assert "Could not reach" not in text
    assert "mozilla" not in text
    assert "pkgt_x" not in text


async def test_project_report_unknown():
    server, _ = _server(lambda request: httpx.Response(404), api_key="pkgt_x")
    text = await _refused(server, "project_report", name="nope")
    assert text == "No project named 'nope'. list_projects shows the names."


async def test_http_status_errors():
    server, _ = _server(
        lambda request: httpx.Response(404, json={"detail": "Not here."}),
        api_key="pkgt_x",
    )
    assert (
        await _refused(server, "list_projects")
        == "https://pt.test answered HTTP 404: Not here."
    )
    server, _ = _server(lambda request: httpx.Response(404), api_key="pkgt_x")
    assert (
        await _refused(server, "list_projects")
        == "https://pt.test answered HTTP 404: Not Found"
    )
    server, _ = _server(
        lambda request: httpx.Response(429, text="slow down"), api_key="pkgt_x"
    )
    assert (
        await _refused(server, "list_projects")
        == "https://pt.test answered HTTP 429: slow down"
    )


async def test_http_error_page_truncated():
    page = "<html>\n  <body>\n" + "Bad Gateway. " * 100 + "</body>\n</html>"
    server, _ = _server(lambda request: httpx.Response(502, text=page))
    text = await _refused(server, "search_packages", query="httpx")
    assert text.startswith(
        "https://pt.test answered HTTP 502: <html> <body> Bad Gateway."
    )
    assert text.endswith("...")
    assert len(text) < 400


@pytest.mark.parametrize(
    "error",
    [
        lambda request: httpx.ConnectError("connection refused", request=request),
        lambda request: httpx.ReadTimeout("timed out", request=request),
    ],
)
async def test_unreachable_server(error):
    def down(request):
        raise error(request)

    server, _ = _server(down)
    text = await _refused(server, "search_packages", query="httpx")
    assert text.startswith("Could not reach https://pt.test: ")
    server, _ = _server(down, api_key="pkgt_x")
    text = await _refused(server, "list_projects")
    assert text.startswith("Could not reach https://pt.test: ")
    assert "pkgt_x" not in text


async def test_project_report_labels():
    report = {
        "project": "api",
        "purl_type": "pypi",
        "withdrawn": [
            {"name": "left", "version": "1.0", "reason": None, "parents": ["glob"]}
        ],
        "packages": [
            {
                "name": "httpx",
                "purl_type": "pypi",
                "installed": "0.24.1",
                "latest_stable": "0.28.1",
                "versions_behind": 4,
                "releases": [
                    {
                        "version": "0.28.0",
                        "published_at": "2024-11-28T00:00:00+00:00",
                        "lines": [
                            {"label": "breaking", "text": "proxies= is gone."},
                            {"label": None, "text": "Faster."},
                        ],
                        "source_url": "https://example.test/r",
                    }
                ],
            }
        ],
    }
    server, _ = _server(lambda request: _ok(report), api_key="pkgt_x")
    text = await _text(server, "project_report", name="api")
    assert "- left 1.0 (pulled in by glob): no reason given" in text
    assert "### httpx 0.24.1 -> 0.28.1  (4 behind)" in text
    assert "  - [BREAKING] proxies= is gone." in text
    assert "  - Faster." in text
    assert 'package_changes("pypi", "httpx", since="0.24.1")' in text


async def test_list_projects_counts_one_package_as_one_package():
    listing = {
        "projects": [
            {
                "name": "tiny",
                "purl_type": "npm",
                "total": 1,
                "direct": 1,
                "measured": 1,
                "behind_direct": 0,
            }
        ]
    }
    server, _ = _server(lambda request: _ok(listing), api_key="pkgt_x")
    text = await _text(server, "list_projects")
    assert "1 package, 1 direct" in text
    assert "1 packages" not in text


async def test_no_projects_is_an_answer_that_names_both_ways_in():
    """An agent has import_project; the CLI is not the only way to push."""
    server, _ = _server(lambda request: _ok({"projects": []}), api_key="pkgt_x")
    result = await server.call_tool("list_projects", {})
    text = result.content[0].text
    assert result.is_error is False
    assert text.startswith("No projects yet. import_project")
    assert "packagetrackdev push" in text


@pytest.mark.parametrize("state", ["scanning", "requested", "rate_limited", "unknown"])
async def test_unheld_sentences_name_the_server_that_answered(state):
    """The sentences said "packagetrack.dev" whoever answered them."""
    unheld = {"state": state, "name": "left-pad", "purl_type": "npm"}

    def handle(request):
        if request.method == "POST":
            return _ok({"state": state})
        return _ok(unheld)

    server, _ = _server(handle)
    call = _refused if state == "rate_limited" else _text
    text = await call(server, "package_versions", ecosystem="npm", name="left-pad")
    assert "pt.test" in text
    assert "packagetrack.dev" not in text


@pytest.mark.parametrize("tool", ["package_versions", "package_changes"])
async def test_a_scan_rate_limit_reads_like_any_other_429(tool):
    """The scan endpoint says rate_limited in a 200 body; the agent gets the
    sentence an HTTP 429 gets, as a refusal."""
    unheld = {"state": "unknown", "name": "left-pad", "purl_type": "npm"}

    def handle(request):
        if request.method == "POST":
            return _ok({"state": "rate_limited"})
        return _ok(unheld)

    server, seen = _server(handle)
    args = {"ecosystem": "npm", "name": "left-pad"}
    if tool == "package_changes":
        args["since"] = "1.0.0"
    text = await _refused(server, tool, **args)

    assert text == (
        "https://pt.test answered HTTP 429: Too many new packages from this "
        "address this hour. Try again later."
    )
    assert [request.method for request in seen] == ["GET", "POST"]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("address", "Too many new packages from this address this hour."),
        ("visitors", "PackageTrack's lookup queue is full for this hour."),
    ],
)
async def test_a_full_shared_queue_is_not_blamed_on_this_address(limit, expected):
    """The ceiling every visitor shares is not this address's doing; the
    server says which ceiling it was (`limit`)."""
    unheld = {"state": "unknown", "name": "left-pad", "purl_type": "npm"}

    def handle(request):
        if request.method == "POST":
            return _ok({"state": "rate_limited", "limit": limit})
        return _ok(unheld)

    server, _ = _server(handle)
    text = await _refused(server, "package_versions", ecosystem="npm", name="left-pad")

    assert text == f"https://pt.test answered HTTP 429: {expected} Try again later."


def _report_and_listing(report: dict, project: dict):
    """A handler answering the report endpoint and the listing differently."""

    def handle(request):
        if request.url.path.endswith("/report"):
            return _ok(report)
        return _ok({"projects": [project]})

    return handle


async def test_project_report_does_not_call_an_unmeasured_project_clean():
    """The reading that is never safe: silence taken for an all clear.

    A project where nothing could be compared answered "nothing behind",
    which is what the web page refuses to say for the same project.
    """
    report = {"project": "api", "purl_type": "pypi", "withdrawn": [], "packages": []}
    project = {
        "name": "api",
        "purl_type": "pypi",
        "total": 37,
        "direct": 14,
        "measured": 0,
        "unplaced_version": 37,
    }
    server, _ = _server(_report_and_listing(report, project), api_key="pkgt_x")
    text = await _text(server, "project_report", name="api")
    assert "0 of 37 packages were compared against the archive" in text
    assert "37 could not be" in text
    assert "37 pinned to a version the archive does not hold" in text
    assert "This is not an all clear for the whole project." in text


async def test_project_report_without_the_listing_says_what_it_always_said():
    """The footnote is not worth failing the report for."""
    report = {"project": "api", "purl_type": "pypi", "withdrawn": [], "packages": []}

    def handle(request):
        if request.url.path.endswith("/report"):
            return _ok(report)
        return httpx.Response(500)

    server, _ = _server(handle, api_key="pkgt_x")
    text = await _text(server, "project_report", name="api")
    assert "Nothing behind and nothing withdrawn" in text


async def test_list_projects_counts_indirect_and_uncompared_dependencies():
    """Four numbers the listing held and the line never said.

    An agent reading "4 of yours behind" took it for the whole of what is
    behind, and took the silence about the rest for nothing to report.
    """
    listing = {
        "projects": [
            {
                "name": "api",
                "purl_type": "pypi",
                "total": 80,
                "direct": 33,
                "measured": 74,
                "behind_direct": 10,
                "behind_indirect": 9,
                "scanning": 4,
                "unplaced_version": 2,
                "unknown_package": 0,
                "snapshot_age_days": 2,
            }
        ]
    }
    server, _ = _server(lambda request: _ok(listing), api_key="pkgt_x")
    text = await _text(server, "list_projects")
    assert "10 of yours behind, 9 behind through a parent" in text
    assert "4 being looked up now" in text
    assert "2 pinned to a version the archive does not hold" in text


async def test_project_report_says_what_is_behind_through_a_parent():
    report = {
        "project": "api",
        "purl_type": "pypi",
        "withdrawn": [],
        "packages": [
            {
                "name": "fastapi",
                "purl_type": "pypi",
                "installed": "0.127.1",
                "latest_stable": "0.141.1",
                "versions_behind": 52,
                "releases": [],
            }
        ],
    }
    project = {
        "name": "api",
        "purl_type": "pypi",
        "total": 37,
        "direct": 14,
        "measured": 37,
        "behind_direct": 1,
        "behind_indirect": 4,
    }
    server, _ = _server(_report_and_listing(report, project), api_key="pkgt_x")
    text = await _text(server, "project_report", name="api")
    assert "4 more are behind through a parent and not listed here" in text
    assert "not an all clear" not in text


async def test_the_uncompared_causes_add_up_to_the_number_in_front_of_them():
    """Every cause, or the sentence contradicts itself.

    A range is not compared either, and leaving it out made the count and its
    parenthesis disagree: "2 could not be (1 not in the archive)".
    """
    report = {
        "project": "api",
        "purl_type": "pypi",
        "withdrawn": [],
        "packages": [
            {
                "name": "ruff",
                "purl_type": "pypi",
                "installed": "0.16.3",
                "latest_stable": "0.16.8",
                "versions_behind": 5,
                "releases": [],
            }
        ],
    }
    project = {
        "name": "api",
        "purl_type": "pypi",
        "total": 6,
        "direct": 6,
        "measured": 4,
        "behind_direct": 4,
        "unknown_package": 1,
        "unpinned": 1,
    }
    server, _ = _server(_report_and_listing(report, project), api_key="pkgt_x")
    text = await _text(server, "project_report", name="api")
    assert (
        "4 of 6 packages were compared against the archive; 2 could not be "
        "(1 not in the archive, 1 a range rather than a version)" in text
    )


DIFF = {"added": 0, "removed": 36, "upgraded": 1, "downgraded": 0}
UPDATE_CHANGES = {
    "added": [],
    "removed": [{"name": "rich", "from": "13.0", "to": None}],
    "upgraded": [{"name": "django", "from": "4.2", "to": "5.0"}],
    "downgraded": [],
}


def _import_world(existing=None, api_key="pkgt_x", same=False):
    """The import door as the server answers it, over the names in `existing`.

    A new name is created (201). Over an existing name
    the set it already holds (`same`) is unchanged and any other set is
    updated with `DIFF`. `posted` holds every body the server stored.
    """
    names = {row["name"] for row in existing or []}
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/mcp/projects/import":
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        body = json.loads(request.content)
        posted.append(body)
        answer = {"project_id": 42, "project_name": body["name"]}
        if body["name"] not in names:
            return httpx.Response(201, json={**answer, "created": True})
        if same:
            return _ok({**answer, "created": False, "unchanged": True})
        return _ok(
            {
                **answer,
                "created": False,
                "updated": True,
                "diff": DIFF,
                "changes": UPDATE_CHANGES,
            }
        )

    server, seen = _server(handler, api_key=api_key)
    return server, seen, posted


CHECKOUT = [{"name": "checkout", "purl_type": "pypi", "total": 37}]
DEPS = [{"name": "django", "version": "5.0"}]
TEN = [{"name": f"pkg{i}", "version": "1.0"} for i in range(10)]


async def test_a_new_name_is_created():
    server, seen, posted = _import_world(existing=[])
    text = await _text(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )
    assert "Project created successfully. 'checkout'" in text
    assert len(seen) == 1


async def test_an_existing_name_is_updated_without_a_question():
    """The push is the update: one request, and the answer says what moved."""
    server, seen, posted = _import_world(existing=CHECKOUT)
    text = await _text(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )
    assert text.startswith("Project updated successfully. 'checkout'")
    assert "Changes:\n- rich 13.0 removed\n- django 4.2 → 5.0" in text
    assert (
        "- django 4.2 → 5.0\nAnd 35 more changes. Use project_report or see "
        "the dashboard."
    ) in text
    assert "https://pt.test" not in text, "the dashboard is named, not linked"
    assert [r.url.path for r in seen] == ["/api/v1/mcp/projects/import"]


async def test_the_set_already_held_is_unchanged():
    server, seen, _ = _import_world(existing=CHECKOUT, same=True)
    text = await _text(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )
    assert text.startswith("No dependency changes detected. 'checkout'")
    assert len(seen) == 1


async def test_the_push_sends_no_confirmation_and_never_asks():
    """No flag, no revision, no question: the body is the set and its name."""
    server, _, posted = _import_world(existing=CHECKOUT)
    await _text(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )
    assert set(posted[0]) == {"name", "purl_type", "source_file", "dependencies"}
    for gone in ("_ask_to_overwrite", "_ask_to_update", "_can_be_asked"):
        assert not hasattr(mcp, gone), gone


async def test_the_tool_takes_no_confirmation_parameters():
    server, _ = _server(lambda request: _ok({}))
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["import_project"].input_schema
    assert set(schema["properties"]) == {
        "name",
        "purl_type",
        "dependencies",
        "file_path",
        "source_file",
    }


def test_a_new_project_names_no_other_project():
    """A new name is a new project, said plainly: the opening line, then the
    short import digest. Another project holding the set is not mentioned,
    even by a server that sends it."""
    text = mcp.render_import(
        {
            "project_name": "app-agent",
            "created": True,
            "similar_to": [{"name": "app", "overlap": 1.0}],
        },
        3,
        "https://pt.test",
        sent=[
            {"name": "django", "version": "5.0"},
            {"name": "httpx", "version": "0.27.0"},
            {"name": "rich", "version": "13.0"},
        ],
    )
    blocks = text.split("\n\n")
    assert blocks[0] == (
        "Project created successfully. 'app-agent' was added with 3 dependencies."
    )
    assert blocks[1].startswith("Imported:")
    assert blocks[2].startswith("Examples:\n- django 5.0")
    assert "'app'" not in text and "Next step" not in text


def test_an_older_server_that_redirected_is_read_as_its_flags_say():
    """A server from 12 to 14 October answered a held set with
    `redirected_from` and the holding project's name; the flags alone say
    what happened to that project."""
    text = mcp.render_import(
        {
            "project_name": "app",
            "created": False,
            "unchanged": True,
            "redirected_from": "app-agent",
            "overlap": 1.0,
        },
        1,
        "https://pt.test",
    )
    assert text.splitlines()[0].startswith(
        "No dependency changes detected. 'app' already contains"
    )


async def test_the_description_says_what_a_push_over_a_name_does():
    server, _ = _server(lambda request: _ok({}))
    tools = {t.name: t for t in await server.list_tools()}
    description = tools["import_project"].description
    assert "updates that project's dependency list" in description
    assert "A new name is a new project, even when another project" in description
    assert "updated instead" not in description
    assert "a new name is refused and nothing is stored" in description
    for gone in ("update_existing", "expected_revision", "confirm_new_project"):
        assert gone not in description
        assert gone not in mcp.INSTRUCTIONS


def test_an_unchanged_import_does_not_say_replaced():
    text = mcp.render_import(
        {"project_name": "checkout", "created": False, "unchanged": True},
        1,
        "https://pt.test",
    )
    assert "replaced" not in text and "updated" not in text


async def test_an_unusable_name_is_refused_before_any_request():
    server, seen, _ = _import_world(existing=[])
    text = await _refused(
        server, "import_project", name="../etc", purl_type="pypi", dependencies=DEPS
    )
    assert text.endswith("Nothing was pushed.")
    assert seen == []


_PUSHED = {"created": True, "project_id": 42, "project_name": "checkout"}
_FAIL = {
    "kind": "no_unpinned",
    "action": "fail",
    "met": False,
    "message": "1 dependency declared as a range, so how far behind it is "
    "depends on when the build ran.",
}
_MET = {
    "kind": "no_withdrawn",
    "action": "warn",
    "met": True,
    "message": "No installed version has been withdrawn.",
}


def test_a_rule_not_met_is_said_as_a_stored_push():
    """The agent must not read it as a failed call and push again."""
    text = mcp.render_import(
        {**_PUSHED, "rules": [_FAIL, _MET], "unmeasured_note": None},
        2,
        "https://pt.test",
    )

    assert "Project created successfully. 'checkout' was added with 2 " in text
    assert (
        "Pushed. Organization rule not met (fail): 1 dependency declared as a "
        "range" in text
    )
    assert "nothing to retry" in text
    assert "withdrawn" not in text


def test_a_warn_rule_is_named_as_warn():
    warn = {**_FAIL, "action": "warn"}
    text = mcp.render_import({**_PUSHED, "rules": [warn]}, 2, "https://pt.test")

    assert "Pushed. Organization rule not met (warn):" in text


def test_the_unmeasured_note_follows_the_verdicts():
    note = "3 dependencies could not be compared, so this was judged on the rest."
    text = mcp.render_import(
        {**_PUSHED, "rules": [_FAIL], "unmeasured_note": note}, 2, "https://pt.test"
    )

    assert note in text


def test_rules_that_could_not_be_judged_are_said_as_stored():
    note = (
        "The dependencies were stored, but this organization's rules could not "
        "be evaluated for this push."
    )
    text = mcp.render_import(
        {**_PUSHED, "rules": [], "rules_note": note}, 2, "https://pt.test"
    )

    assert f"Pushed. {note} There is nothing to retry." in text


@pytest.mark.parametrize(
    "extra",
    [{}, {"rules": [], "unmeasured_note": None, "rules_note": None}, {"rules": [_MET]}],
    ids=["older-server", "no-rules", "all-met"],
)
def test_no_rule_line_when_nothing_was_broken(extra):
    text = mcp.render_import({**_PUSHED, **extra}, 2, "https://pt.test")

    assert "Pushed." not in text
    assert "rule" not in text.lower()


async def test_the_tool_passes_the_verdicts_through():
    """End to end through import_project, against the endpoint's answer."""

    def handler(request):
        if request.url.path == "/api/v1/projects":
            return _ok({"projects": []})
        if request.url.path == "/api/v1/mcp/projects/import":
            return _ok({**_PUSHED, "rules": [_FAIL]})
        raise AssertionError(f"unexpected request: {request.url}")

    server, _ = _server(handler, api_key="pkgt_x")
    text = await _text(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )

    assert "Pushed. Organization rule not met (fail):" in text


async def test_the_agents_own_parents_are_cut_at_20():
    """The endpoint refuses more than 20 with a bare 422."""
    server, _, posted = _import_world(existing=[])
    many = [f"p{i}" for i in range(25)]

    await _text(
        server,
        "import_project",
        name="checkout",
        purl_type="pypi",
        dependencies=[{"name": "django", "version": "5.0", "parents": many}],
    )

    sent = posted[0]["dependencies"][0]["parents"]
    assert sent == many[:20]


async def test_the_description_says_a_rule_is_not_a_rejection():
    server, _ = _server(lambda request: _ok({}))
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert (
        "The push is stored; organization rules come back as warn/fail and are "
        "not a rejection." in tools["import_project"].description
    )


def _key_world(monkeypatch, tmp_path, file_key, good_key, api_key=None):
    """A config file holding `file_key`, and a server that takes only `good_key`."""
    path = tmp_path / "config.toml"
    path.write_text(f'api_key = "{file_key}"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv(config.ENV_KEY, raising=False)

    def handler(request):
        if request.headers["authorization"] != f"Bearer {good_key}":
            return httpx.Response(401, json={"detail": "Missing or invalid API key."})
        return _ok({"projects": []})

    server, seen = _server(handler, api_key=api_key or file_key)
    return server, seen, path


async def test_a_key_replaced_by_login_is_picked_up_without_a_restart(
    monkeypatch, tmp_path
):
    """HATALAR 38: one 401, the file re-read, the call made again."""
    server, seen, path = _key_world(monkeypatch, tmp_path, "pkgt_old", "pkgt_new")
    path.write_text('api_key = "pkgt_new"\n', encoding="utf-8")

    text = await _text(server, "list_projects")

    assert "rejected" not in text
    assert [r.headers["authorization"] for r in seen] == [
        "Bearer pkgt_old",
        "Bearer pkgt_new",
    ]


async def test_an_unchanged_key_says_it_may_be_old_and_how_to_renew_it(
    monkeypatch, tmp_path
):
    server, seen, _ = _key_world(monkeypatch, tmp_path, "pkgt_old", "pkgt_new")

    text = await _refused(server, "list_projects")

    assert len(seen) == 1, "nothing changed, so nothing is sent again"
    assert "The key may be old or revoked." in text
    assert "`packagetrackdev login --api-key pkgt_...`" in text
    assert "no restart" in text
    assert "https://pt.test/app/keys" in text


async def test_a_key_passed_on_purpose_is_never_swapped(monkeypatch, tmp_path):
    """A flag's key is not replaced by the file's, which may be another org's."""
    server, seen, _ = _key_world(
        monkeypatch, tmp_path, "pkgt_file", "pkgt_file", api_key="pkgt_flag"
    )

    text = await _refused(server, "list_projects")

    assert [r.headers["authorization"] for r in seen] == ["Bearer pkgt_flag"]
    assert "passed with --api-key" in text


async def test_a_key_from_the_environment_is_not_renewed_by_login(
    monkeypatch, tmp_path
):
    """The environment wins over the file, so `login` would change nothing."""
    path = tmp_path / "config.toml"
    path.write_text('api_key = "pkgt_file"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setenv(config.ENV_KEY, "pkgt_env")
    refused = {"detail": "Missing or invalid API key."}
    server, seen = _server(
        lambda request: httpx.Response(401, json=refused), api_key="pkgt_env"
    )

    text = await _refused(server, "list_projects")

    assert len(seen) == 1
    assert "It comes from PACKAGETRACK_API_KEY" in text


async def test_a_push_after_login_is_sent_once_more_with_the_new_key(
    monkeypatch, tmp_path
):
    path = tmp_path / "config.toml"
    path.write_text('api_key = "pkgt_old"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv(config.ENV_KEY, raising=False)
    posts = []

    def handler(request):
        if request.url.path == "/api/v1/projects":
            return _ok({"projects": []})
        posts.append(request.headers["authorization"])
        if request.headers["authorization"] != "Bearer pkgt_new":
            return httpx.Response(401, json={"detail": "Missing or invalid API key."})
        return _ok({"created": True, "project_id": 1, "project_name": "checkout"})

    server, _ = _server(handler, api_key="pkgt_old")
    path.write_text('api_key = "pkgt_new"\n', encoding="utf-8")

    text = await _text(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )

    assert "Project created successfully. 'checkout'" in text
    assert posts == ["Bearer pkgt_old", "Bearer pkgt_new"]


def test_key_source_asks_each_place_again(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('api_key = "pkgt_file"\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv(config.ENV_KEY, raising=False)
    assert config.key_source(None) is None
    assert config.key_source("pkgt_file") == "file"
    assert config.key_source("pkgt_other") == "flag"
    monkeypatch.setenv(config.ENV_KEY, "pkgt_env")
    assert config.key_source("pkgt_env") == "env"
    assert config.key_source("pkgt_file") == "flag"


ECOSYSTEM_CONFLICT_BODY = {
    "code": "ecosystem_conflict",
    "detail": "Nothing was pushed. Project 'shop' holds pypi dependencies.",
    "project": "shop",
    "existing_ecosystem": "pypi",
    "incoming_ecosystem": "npm",
    "suggested_name": "shop-npm",
}


def _ecosystem_world():
    """An import door that refuses every push as another ecosystem's set."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=ECOSYSTEM_CONFLICT_BODY)

    return _server(handler, api_key="pkgt_x")


async def test_another_ecosystem_is_said_in_words():
    server, seen = _ecosystem_world()
    text = await _refused(
        server,
        "import_project",
        name="shop",
        purl_type="npm",
        dependencies=[{"name": "left-pad", "version": "1.3.0"}],
    )

    assert text == (
        "The project uses a different ecosystem. 'shop' currently contains pypi "
        "dependencies and cannot be replaced with npm dependencies. Use a "
        "different project name, such as 'shop-npm'. No changes were made."
    )
    assert len(seen) == 1, "one push, refused; nothing retried"


async def test_a_conflict_without_the_fields_falls_back_to_the_servers_sentence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"code": "ecosystem_conflict", "detail": "Nothing was pushed."}
        )

    server, _ = _server(handler, api_key="pkgt_x")
    text = await _refused(
        server, "import_project", name="shop", purl_type="npm", dependencies=DEPS
    )
    assert text == (
        "The operation could not be completed.\nDetails: Nothing was pushed."
    ), "the server's sentence says nothing changed; ours does not say it twice"


async def test_the_description_says_a_name_holds_one_ecosystem():
    server, _ = _server(lambda request: _ok({}))
    tools = {t.name: t for t in await server.list_tools()}
    assert "A name holds one ecosystem" in tools["import_project"].description


async def test_a_failed_push_says_what_the_server_said():
    """No check runs before the push any more; a failure is the push's own."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "upstream is down"})

    server, seen = _server(handler, api_key="pkgt_x")
    text = await _refused(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )
    assert text == messages.OPERATION_UNCONFIRMED + "\nDetails: upstream is down"
    assert [r.url.path for r in seen] == ["/api/v1/mcp/projects/import"]


async def test_the_guard_needs_a_key_like_every_project_tool():
    server, seen, _ = _import_world(existing=[], api_key=None)
    text = await _refused(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=DEPS
    )
    assert "needs a PackageTrack API key" in text
    assert seen == [], "no key, so no request was worth making"


REQUIREMENTS = (
    "# the service's direct dependencies\n"
    "django==5.0.4\n"
    "requests[socks]>=2.31  # a range, stored without a version\n"
    "--index-url https://user:hunter2@private.example/simple\n"
)


def _requirements(tmp_path, name="requirements.txt", content=REQUIREMENTS):
    path = tmp_path / "private-dir" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def test_a_file_path_sends_the_parsed_names_and_versions(tmp_path):
    path = _requirements(tmp_path)
    server, _, posted = _import_world(existing=[])

    text = await _text(server, "import_project", name="checkout", file_path=str(path))

    body = posted[0]
    assert body == {
        "name": "checkout",
        "purl_type": "pypi",
        "source_file": "requirements.txt",
        "dependencies": [
            {"name": "django", "version": "5.0.4", "parents": [], "indirect": False},
            {"name": "requests", "version": "", "parents": [], "indirect": False},
        ],
    }
    raw = json.dumps(posted[0])
    assert "hunter2" not in raw and "index-url" not in raw
    assert "private-dir" not in raw
    assert "Project created successfully. 'checkout' was added with 2 " in text
    assert "Source file: requirements.txt." in text
    assert "1 line of the file was not a dependency" in text
    assert "declaration, not a lock file" in text


async def test_a_lock_file_keeps_its_parent_chains(tmp_path):
    path = _requirements(
        tmp_path,
        "uv.lock",
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "app"\nversion = "0.1.0"\n'
        'source = { editable = "." }\n'
        'dependencies = [{ name = "django" }]\n\n'
        '[[package]]\nname = "django"\nversion = "5.0.4"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'dependencies = [{ name = "sqlparse" }]\n\n'
        '[[package]]\nname = "sqlparse"\nversion = "0.5.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
    )
    server, _, posted = _import_world(existing=[])

    await _text(server, "import_project", name="checkout", file_path=str(path))

    body = posted[0]
    assert body["source_file"] == "uv.lock"
    by_name = {d["name"]: d for d in body["dependencies"]}
    assert by_name["django"]["version"] == "5.0.4"
    assert by_name["sqlparse"]["parents"] == ["django"]


async def test_a_pnpm_lock_is_read_here_with_its_parents(tmp_path):
    path = _requirements(
        tmp_path,
        "pnpm-lock.yaml",
        "lockfileVersion: '9.0'\n\n"
        "importers:\n\n  .:\n    dependencies:\n      debug:\n"
        "        specifier: ^4.3.4\n        version: 4.3.4\n\n"
        "packages:\n\n  debug@4.3.4:\n    resolution: {integrity: sha512-PRWF}\n\n"
        "  ms@2.1.2:\n    resolution: {integrity: sha512-sGkP}\n\n"
        "snapshots:\n\n  debug@4.3.4:\n    dependencies:\n      ms: 2.1.2\n\n"
        "  ms@2.1.2: {}\n",
    )
    server, _, posted = _import_world(existing=[])

    await _text(server, "import_project", name="web", file_path=str(path))

    body = posted[0]
    assert body["purl_type"] == "npm"
    assert body["source_file"] == "pnpm-lock.yaml"
    by_name = {d["name"]: d for d in body["dependencies"]}
    assert by_name["debug"]["parents"] == []
    assert by_name["ms"]["parents"] == ["debug"]


async def test_a_pubspec_lock_imports_as_pub(tmp_path):
    path = _requirements(
        tmp_path,
        "pubspec.lock",
        "packages:\n  http:\n    dependency: \"direct main\"\n"
        "    description:\n      name: http\n      url: \"https://pub.dev\"\n"
        "    source: hosted\n    version: \"1.2.1\"\n"
        "sdks:\n  dart: \">=3.3.0 <4.0.0\"\n",
    )
    server, _, posted = _import_world(existing=[])

    await _text(server, "import_project", name="app", file_path=str(path))

    assert posted[0]["purl_type"] == "pub"
    assert posted[0]["dependencies"][0]["name"] == "http"
    assert posted[0]["dependencies"][0]["version"] == "1.2.1"


async def test_a_binary_bun_lockb_is_never_opened_and_says_what_to_run(tmp_path):
    path = tmp_path / "bun.lockb"
    path.write_bytes(b"#!/usr/bin/env bun\nbun-lockfile-format-v0\n\x00")
    server, seen, _ = _import_world(existing=[])

    text = await _refused(server, "import_project", name="web", file_path=str(path))

    assert "bun install --save-text-lockfile" in text
    assert seen == []


async def test_a_file_that_is_not_a_dependency_file_is_never_opened(tmp_path):
    for name in ("id_ed25519", ".env", "credentials", "settings.py"):
        path = _requirements(tmp_path, name, "django==5.0\n")
        server, seen, _ = _import_world(existing=[])

        text = await _refused(
            server, "import_project", name="checkout", file_path=str(path)
        )

        assert "not a dependency file, so it was not opened" in text, name
        assert seen == [], name


async def test_an_unrecognised_file_is_refused_here_and_names_the_formats(tmp_path):
    path = _requirements(tmp_path, "notes.txt", "hello world\nthis is prose\n")
    server, seen, _ = _import_world(existing=[])

    text = await _refused(
        server, "import_project", name="checkout", file_path=str(path)
    )

    assert text == (
        "notes.txt was opened, but its content is not a dependency file this "
        f"tool can read. {mcp.SUPPORTED_FILES_SENTENCE} Nothing was pushed."
    )
    assert seen == []


def test_both_refusals_of_an_unsupported_file_name_the_same_formats(tmp_path):
    """By name the file is not opened, by content it was; the list of what is
    read is one sentence, so neither names fewer formats."""
    prose = tmp_path / "notes.txt"
    prose.write_text("hello world\nthis is prose\n", encoding="utf-8")

    by_name = mcp.read_lock_file(str(tmp_path / "settings.py"))
    by_content = mcp.read_lock_file(str(prose))

    assert "so it was not opened" in by_name
    assert "was opened, but its content" in by_content
    for text in (by_name, by_content):
        assert mcp.SUPPORTED_FILES_SENTENCE in text
    for named in ("uv.lock", "requirements-dev.txt", "a renamed go.mod", "SBOM"):
        assert named in mcp.SUPPORTED_FILES_SENTENCE, named


async def test_a_broken_lock_file_says_which_format_it_looked_like(tmp_path):
    path = _requirements(tmp_path, "package-lock.json", "{not json")
    server, seen, _ = _import_world(existing=[])

    text = await _refused(
        server, "import_project", name="checkout", file_path=str(path)
    )

    assert "package-lock.json is package-lock.json but could not be read" in text
    assert seen == []


async def test_a_missing_or_oversized_file_costs_no_request(tmp_path, monkeypatch):
    server, seen, _ = _import_world(existing=[])
    missing = await _refused(
        server, "import_project", name="checkout", file_path=str(tmp_path / "uv.lock")
    )
    monkeypatch.setattr(mcp, "MAX_FILE_BYTES", 5)
    big = await _refused(
        server,
        "import_project",
        name="checkout",
        file_path=str(_requirements(tmp_path)),
    )

    assert "No file at" in missing
    assert "reads at most 5" in big
    assert seen == []


async def test_a_file_over_the_ceiling_is_refused_before_the_listing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(mcp, "MAX_DEPENDENCIES", 1)
    server, seen, _ = _import_world(existing=[])

    text = await _refused(
        server,
        "import_project",
        name="checkout",
        file_path=str(_requirements(tmp_path)),
    )

    assert text.startswith(
        "The project exceeds the supported dependency limit. This import "
        "contains 2 dependencies; the current limit is 1. No project data was "
        "changed."
    )
    assert seen == []


async def test_a_purl_type_that_disagrees_with_the_file_is_refused(tmp_path):
    server, seen, _ = _import_world(existing=[])

    text = await _refused(
        server,
        "import_project",
        name="checkout",
        purl_type="npm",
        file_path=str(_requirements(tmp_path)),
    )

    assert "holds pypi dependencies, not npm" in text
    assert seen == []


async def test_every_tool_says_whether_it_writes():
    """Five tools only read; import_project replaces an existing project's
    list without asking, and the same set again stores nothing. Hints, not
    guarantees: a client may base its confirmation on them. openWorldHint is
    left to its default, true: the archive is a service on the internet."""
    server, _ = _server(lambda request: _ok({}))
    hints = {
        tool.name: (
            tool.annotations.model_dump(by_alias=True, exclude_none=True)
            if tool.annotations
            else None
        )
        for tool in await server.list_tools()
    }

    assert hints == {
        "package_changes": {"readOnlyHint": True},
        "package_versions": {"readOnlyHint": True},
        "search_packages": {"readOnlyHint": True},
        "list_projects": {"readOnlyHint": True},
        "project_report": {"readOnlyHint": True},
        "import_project": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
        },
        "update_status": {"readOnlyHint": True, "openWorldHint": False},
    }


async def test_a_client_tells_a_refusal_from_an_answer_by_is_error():
    """Through a real client session, not the server's own call_tool: a
    refusal arrives with isError=true and its sentence, an answer that the
    archive has no such package arrives with isError=false."""
    from mcp import Client

    server, seen = _server(lambda request: _ok({"state": "not_found"}))

    async with Client(server) as client:
        refused = await client.call_tool(
            "import_project",
            {"name": "checkout", "purl_type": "pypi", "dependencies": []},
        )
        unkeyed = await client.call_tool("list_projects", {})
        answered = await client.call_tool(
            "package_versions", {"ecosystem": "pypi", "name": "nosuchpkg"}
        )

    assert refused.is_error is True
    assert messages.EMPTY_DEPENDENCIES in refused.content[0].text
    assert unkeyed.is_error is True
    assert "needs a PackageTrack API key" in unkeyed.content[0].text
    assert answered.is_error is False
    assert answered.content[0].text == "PyPI has no package named nosuchpkg."
    assert [request.method for request in seen] == ["GET"]


async def test_exactly_one_source_is_required():
    server, seen, _ = _import_world(existing=[])

    both = await _refused(
        server,
        "import_project",
        name="checkout",
        purl_type="pypi",
        dependencies=DEPS,
        file_path="requirements.txt",
    )
    neither = await _refused(server, "import_project", name="checkout")
    no_type = await _refused(
        server, "import_project", name="checkout", dependencies=DEPS
    )

    assert "not both and not neither" in both
    assert "not both and not neither" in neither
    assert "purl_type is required" in no_type
    assert seen == []


async def test_an_empty_list_is_refused_and_sends_nothing():
    server, seen, _ = _import_world(existing=[])
    text = await _refused(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=[]
    )

    assert text == messages.EMPTY_DEPENDENCIES
    assert seen == []


async def test_an_empty_list_over_an_existing_name_changes_nothing():
    server, seen, posted = _import_world(existing=CHECKOUT)
    text = await _refused(
        server, "import_project", name="checkout", purl_type="pypi", dependencies=[]
    )

    assert text == messages.EMPTY_DEPENDENCIES
    assert seen == []
    assert posted == []


async def test_a_file_with_no_dependencies_is_refused_by_its_name(tmp_path):
    server, seen, _ = _import_world(existing=CHECKOUT)
    path = tmp_path / "web" / "package.json"
    path.parent.mkdir()
    path.write_text('{"name": "web", "version": "1.0.0"}', encoding="utf-8")

    text = await _refused(
        server, "import_project", name="checkout", file_path=str(path)
    )

    assert text == (
        "package.json lists no dependencies, so there is nothing to push. "
        "No project data was changed."
    )
    assert str(tmp_path) not in text
    assert seen == []


async def test_an_empty_file_says_what_it_could_not_read(tmp_path):
    server, seen, _ = _import_world(existing=[])
    path = _requirements(tmp_path, content="-r base.txt\n")

    text = await _refused(
        server, "import_project", name="checkout", file_path=str(path)
    )

    assert text.startswith("requirements.txt lists no dependencies")
    assert text.endswith("1 entry in it could not be read as a dependency.")
    assert seen == []


async def test_a_file_over_an_existing_name_is_the_same_update(tmp_path):
    server, seen, posted = _import_world(existing=CHECKOUT)
    text = await _text(
        server,
        "import_project",
        name="checkout",
        file_path=str(_requirements(tmp_path)),
    )

    assert text.startswith("Project updated successfully. 'checkout'")
    assert len(json.loads(seen[0].content)["dependencies"]) == 2
    assert len(posted) == 1


def test_the_created_answer_is_the_standard_sentence():
    text = mcp.render_import(
        {"project_name": "shop", "created": True}, 12, "https://pt.test"
    )
    assert text.splitlines()[0] == (
        "Project created successfully. 'shop' was added with 12 dependencies."
    )


def test_one_dependency_is_singular():
    text = mcp.render_import({"project_name": "shop", "created": True}, 1, "x")
    assert "was added with 1 dependency." in text


_CREATED = {"project_name": "test-1", "created": True, "stored": 155, "direct": 42}


def test_a_new_project_gets_the_import_digest_then_the_way_to_the_report():
    text = mcp.render_import(_CREATED, 155, "https://pt.test")

    assert text.startswith(
        "Project created successfully. 'test-1' was added with 155 dependencies."
        "\n\nImported:\n155 dependencies · 42 direct · 113 indirect\n\n"
    )
    assert "Report page:" not in text
    assert 'Call project_report("test-1")' in text


@pytest.mark.parametrize(
    "counts",
    [{}, {"stored": 155}, {"direct": 42}, {"stored": 155, "direct": None}],
    ids=["older-server", "no-direct", "no-stored", "direct-null"],
)
def test_without_both_counts_the_digest_is_the_total_alone(counts):
    text = mcp.render_import(
        {"project_name": "test-1", "created": True, **counts}, 155, "x"
    )

    assert "Imported:\n155 dependencies\n" in text
    assert "direct" not in text


@pytest.mark.parametrize(
    "counts",
    [
        {"stored": 3, "direct": 4},
        {"stored": 3, "direct": -1},
        {"stored": 3, "direct": True},
    ],
    ids=["more-direct-than-stored", "negative", "bool"],
)
def test_a_split_that_does_not_add_up_is_left_out(counts):
    text = mcp.render_import(
        {"project_name": "test-1", "created": True, **counts}, 3, "x"
    )

    assert "Imported:\n3 dependencies\n" in text
    assert "indirect" not in text


def test_the_stored_count_wins_over_what_was_sent():
    """The store merges a package listed twice; the digest says what it holds."""
    text = mcp.render_import({**_CREATED, "stored": 150, "direct": 40}, 155, "x")

    assert "was added with 150 dependencies." in text
    assert "Imported:\n150 dependencies · 40 direct · 110 indirect" in text


def test_a_new_project_is_not_described_as_a_change():
    text = mcp.render_import(_CREATED, 155, "https://pt.test").lower()

    for update_word in (
        "added:",
        "155 added",
        "upgraded",
        "downgraded",
        "removed",
        "changes",
        "other_changes",
        "examples",
    ):
        assert update_word not in text


@pytest.mark.parametrize(
    "answer, first_line",
    [
        (
            {"updated": True},
            "Project updated successfully. 'shop' now reflects the latest "
            "dependency set.",
        ),
        (
            {"unchanged": True},
            "No dependency changes detected. 'shop' already contains this "
            "dependency set. No new history entry was created.",
        ),
    ],
    ids=["updated", "unchanged"],
)
def test_an_update_or_an_unchanged_set_gets_no_import_digest(answer, first_line):
    text = mcp.render_import(
        {"project_name": "shop", "created": False, "stored": 10, "direct": 2, **answer},
        10,
        "https://pt.test",
    )

    lines = text.splitlines()
    assert lines[0] == first_line
    assert lines[1] != "", "the block layout is the create answer's alone"
    assert "Imported:" not in text
    assert 'Call project_report("shop")' in text


async def test_the_created_answer_never_lists_the_dependencies():
    """End to end through import_project: counts, never the names."""

    def handler(request):
        if request.url.path == "/api/v1/mcp/projects/import":
            return httpx.Response(
                201,
                json={
                    "created": True,
                    "project_id": 42,
                    "project_name": "app",
                    "stored": 10,
                    "direct": 10,
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    server, _ = _server(handler, api_key="pkgt_x")
    text = await _text(
        server, "import_project", name="app", purl_type="pypi", dependencies=TEN
    )

    assert "Imported:\n10 dependencies · 10 direct · 0 indirect" in text
    assert "Examples:\n- pkg0 1.0\n- pkg1 1.0\n- pkg2 1.0\n- pkg3 1.0" in text
    assert (
        "- pkg3 1.0\nAnd 6 more dependencies. Use project_report or see the dashboard."
    ) in text
    assert not any(f"pkg{i} " in text for i in range(4, 10))
    assert 'Call project_report("app")' in text


def test_the_created_examples_put_the_customers_own_lines_first():
    sent = [
        {"name": "zlib-ng", "version": "2.0", "parents": [], "indirect": False},
        {"name": "certifi", "version": "1.0", "parents": ["requests"]},
        {"name": "idna", "version": "3.7", "parents": [], "indirect": True},
        {"name": "Django", "version": "5.0", "parents": [], "indirect": False},
        {"name": "django", "version": "5.0", "parents": [], "indirect": False},
        {"name": "anyio", "version": "", "parents": [], "indirect": False},
    ]
    text = mcp.render_import(
        {"project_name": "shop", "created": True, "stored": 5, "direct": 3},
        6,
        "x",
        sent=sent,
    )

    assert text.startswith(
        "Project created successfully. 'shop' was added with 5 dependencies."
        "\n\nImported:\n5 dependencies · 3 direct · 2 indirect\n\n"
        "Examples:\n- anyio\n- Django 5.0\n- zlib-ng 2.0\n- certifi 1.0\n"
        "And 1 more dependency. Use project_report or see the dashboard.\n"
    )


def _approved(**counts):
    """The difference as the server sends it: counts, five names a kind."""
    changes = {}
    for kind in ("added", "removed", "upgraded", "downgraded"):
        n = counts.get(kind, 0)
        changes[kind] = [
            {
                "name": f"{kind[:2]}{i}",
                "from": None if kind == "added" else "1.0",
                "to": None if kind == "removed" else "2.0",
            }
            for i in range(min(n, 5))
        ]
    return {
        "diff": {k: counts.get(k, 0) for k in changes},
        "changes": changes,
    }


def _updated(approved):
    return mcp.render_import(
        {
            "project_name": "test-1",
            "created": False,
            "updated": True,
            **(approved or {}),
        },
        10,
        "https://pt.test",
    )


def test_an_upgrade_is_named_in_the_update_digest():
    approved = {
        "diff": {"added": 0, "removed": 0, "upgraded": 1, "downgraded": 0},
        "changes": {
            "added": [],
            "removed": [],
            "upgraded": [{"name": "requests", "from": "2.31", "to": "2.32"}],
            "downgraded": [],
        },
    }

    text = _updated(approved)

    assert text.startswith(
        "Project updated successfully. 'test-1' now reflects the latest "
        "dependency set.\n\nChanges:\n- requests 2.31 → 2.32\n\n"
    )
    assert "more changes" not in text
    assert "Report page:" not in text
    assert 'Call project_report("test-1")' in text


def test_every_kind_is_worded_and_at_most_four_are_shown():
    text = _updated(_approved(added=2, removed=2, upgraded=2, downgraded=2))

    block = text.split("Changes:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert block == [
        "- ad0 2.0 added",
        "- re0 1.0 removed",
        "- up0 1.0 → 2.0",
        "- do0 1.0 → 2.0 (downgraded)",
        "And 4 more changes. Use project_report or see the dashboard.",
    ]


def test_one_crowded_kind_does_not_crowd_out_the_rest():
    text = _updated(_approved(added=1, removed=36, upgraded=1))

    block = text.split("Changes:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert block == [
        "- ad0 2.0 added",
        "- re0 1.0 removed",
        "- up0 1.0 → 2.0",
        "- re1 1.0 removed",
        "And 34 more changes. Use project_report or see the dashboard.",
    ]


def test_no_package_moving_is_not_said_as_four_zeroes():
    approved = _approved()

    text = _updated(approved)

    assert (
        "Project updated successfully. 'test-1' now reflects the latest "
        "dependency set.\n\nProject information changed.\n\n"
        "No package version changes were detected.\n"
    ) in text
    assert "Added: 0" not in text and ": 0" not in text
    assert "Dependency changes detected" not in text
    assert "Changes:" not in text
    assert 'Call project_report("test-1")' in text


def test_an_update_with_nothing_known_about_it_gets_no_digest():
    """A server older than the difference sends nothing, or counts alone:
    nothing is named."""
    for approved in (None, {"diff": {"added": 0, "removed": 36, "upgraded": 1}}):
        text = _updated(approved)
        assert text.splitlines()[1] != ""
        assert "Changes:" not in text and "Project information" not in text


def test_an_unchanged_set_gets_no_digest_whatever_was_approved():
    text = mcp.render_import(
        {
            "project_name": "test-1",
            "created": False,
            "unchanged": True,
            **_approved(upgraded=3),
        },
        10,
        "x",
        sent=[{"name": "django", "version": "5.0"}],
    )

    assert text.splitlines()[1] != ""
    for block in ("Changes:", "Examples:", "Imported:", "Project information"):
        assert block not in text


@pytest.mark.parametrize(
    "answer", [{"updated": True}, {}], ids=["updated", "older-server"]
)
def test_the_updated_answer_is_the_standard_sentence(answer):
    text = mcp.render_import(
        {"project_name": "shop", "created": False, **answer}, 3, "https://pt.test"
    )
    assert text.splitlines()[0] == (
        "Project updated successfully. 'shop' now reflects the latest dependency set."
    )


def test_the_whole_diff_never_reaches_the_tool_answer():
    text = _updated(_approved(added=40, removed=30, upgraded=20, downgraded=10))
    assert text.count("\n- ") == messages.RESULT_PREVIEW_LIMIT
    assert "And 96 more changes." in text


def _refusing(status: int, body: dict):
    return _server(lambda request: httpx.Response(status, json=body), api_key="pkgt_x")


async def test_another_ecosystem_without_a_suggested_name_ends_plainly():
    server, _ = _refusing(409, {**ECOSYSTEM_CONFLICT_BODY, "suggested_name": None})
    text = await _refused(
        server, "import_project", name="shop", purl_type="npm", dependencies=DEPS
    )
    assert "Use a different project name. No changes were made." in text


async def test_the_project_limit_is_said_from_its_code():
    server, seen = _refusing(
        400,
        {
            "code": "project_limit",
            "detail": "An account tracks 20 projects.",
            "limit": 20,
        },
    )
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text == (
        "The organization has reached its limit of 20 projects. No new project "
        "was created. Remove a project it no longer needs to add this one."
    )
    assert "capacity" not in text
    assert len(seen) == 1, "one request, refused; nothing retried"


async def test_a_limit_without_its_number_makes_none_up():
    server, _ = _refusing(400, {"code": "project_limit", "detail": "x"})
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text == messages.PROJECT_LIMIT_UNNUMBERED


async def test_an_unknown_code_falls_back_to_the_servers_sentence():
    server, seen = _refusing(
        409, {"code": "something_new", "detail": "A newer server said this."}
    )
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text == (
        "The operation could not be completed. No project data was changed.\n"
        "Details: A newer server said this."
    )
    assert len(seen) == 1, "an unknown refusal is not retried"


async def test_a_credential_name_is_one_sentence_naming_no_file():
    server, seen = _refusing(
        400,
        {"code": "credential_name", "detail": "Nothing was pushed. x", "count": 2},
    )
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text == (
        "2 dependency names look like an access token or API key. Leave them "
        "out of the dependency list, revoke them if real, and push again. "
        "Nothing was pushed."
    )
    assert "Details" not in text and "file" not in text
    assert len(seen) == 1, "a refused push is not retried"


async def test_one_credential_name_is_said_in_the_singular():
    server, _ = _refusing(400, {"code": "credential_name", "detail": "x", "count": 1})
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text.startswith("1 dependency name looks like")
    assert "Leave it out" in text


async def test_a_credential_refusal_without_its_count_says_the_servers_sentence():
    server, _ = _refusing(
        400, {"code": "credential_name", "detail": "Nothing was pushed. Keys."}
    )
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text == (
        "The operation could not be completed.\nDetails: Nothing was pushed. Keys."
    )


async def test_a_lost_connection_does_not_claim_nothing_changed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    server, _ = _server(handler, api_key="pkgt_x")
    text = await _refused(
        server, "import_project", name="new", purl_type="pypi", dependencies=DEPS
    )
    assert text.startswith(messages.OPERATION_UNCONFIRMED)
    assert "No project data was changed" not in text


def test_no_sentence_for_the_person_names_internals_or_uses_a_long_dash():
    for name in dir(messages):
        value = getattr(messages, name)
        if not name.isupper() or not isinstance(value, str) or " " not in value:
            continue
        assert "—" not in value, name
        for internal in ("expected_revision", "update_existing", "409", "HTTP"):
            assert internal not in value, name


POINTER = 'Call project_report("test-1") for the full report.'


@pytest.mark.parametrize(
    "data",
    [
        _CREATED,
        {"project_name": "test-1", "created": False, "updated": True},
        {"project_name": "test-1", "created": False, "unchanged": True},
    ],
    ids=["created", "updated", "unchanged"],
)
def test_every_push_ends_with_one_pointer_to_project_report(data):
    text = mcp.render_import(data, 40, "https://pt.test")

    assert text.rstrip().endswith(POINTER)
    assert text.count("project_report(") == 1
    assert "https://pt.test" not in text, "no link to the dashboard"


def test_nothing_is_said_about_lookups_the_server_did_not_count():
    """The fixed "being fetched, ask again in a minute" is gone: with no
    counts, or counts of zero, the answer ends at the pointer."""
    for data in (
        _CREATED,
        {**_CREATED, "not_compared": 0, "being_looked_up": 0},
    ):
        text = mcp.render_import(data, 40, "https://pt.test")
        assert "looked up" not in text and "minute" not in text
        assert "Rate limit" not in text


def test_lookups_in_flight_are_counted_without_a_time():
    text = mcp.render_import(
        {**_CREATED, "not_compared": 3, "being_looked_up": 2},
        40,
        "https://pt.test",
    )
    lines = text.splitlines()
    assert lines[-3:] == [
        "2 dependencies being looked up; call project_report again when that finishes.",
        "1 dependency could not be compared; project_report says why.",
        POINTER,
    ]
    assert "minute" not in text


def test_lookups_the_rate_limit_refused_are_a_warning():
    text = mcp.render_import(
        {
            **_CREATED,
            "not_compared": 1,
            "being_looked_up": 0,
            "lookups_refused": 1,
        },
        40,
        "https://pt.test",
    )
    assert (
        "Rate limit: PackageTrack's lookup queue is full for this hour, so 1 "
        "dependency the archive does not hold was not looked up. Push again later."
    ) in text
    assert "being looked up" not in text
    assert text.rstrip().endswith(POINTER)


def test_a_count_that_is_not_one_is_not_said():
    text = mcp.render_import(
        {**_CREATED, "being_looked_up": "lots", "lookups_refused": True},
        40,
        "https://pt.test",
    )
    assert "looked up" not in text and "Rate limit" not in text


def test_a_review_an_older_server_sends_is_not_shown():
    review = {"total": 40, "measured": 40, "behind_direct": 34, "rows": []}

    text = mcp.render_import({**_CREATED, "review": review}, 40, "x")

    assert "Review:" not in text and "behind" not in text
    assert not hasattr(messages, "review")


GO_MOD = (
    "module example.com/go-mix\n\ngo 1.23\n\n"
    "require (\n\tgithub.com/google/uuid v1.6.0\n"
    "\tgolang.org/x/text v0.14.0 // indirect\n)\n"
)


async def test_a_renamed_go_mod_is_read_by_its_content(tmp_path):
    """`go1.mod` was refused unopened on 29 September although the parser
    recognises a go.mod by its `module` line whatever the file is called."""
    path = _requirements(tmp_path, "go1.mod", GO_MOD)
    server, _, posted = _import_world(existing=[])

    text = await _text(server, "import_project", name="go1", file_path=str(path))

    body = posted[0]
    assert (body["purl_type"], body["source_file"]) == ("go", "go.mod")
    assert [(d["name"], d["version"], d["indirect"]) for d in body["dependencies"]] == [
        ("github.com/google/uuid", "v1.6.0", False),
        ("golang.org/x/text", "v0.14.0", True),
    ]
    assert "Source file: go.mod." in text


async def test_a_mod_file_that_is_not_a_go_mod_is_refused_as_unreadable(tmp_path):
    path = _requirements(tmp_path, "notes.mod", "hello world\nthis is prose\n")
    server, seen, _ = _import_world(existing=[])

    text = await _refused(
        server, "import_project", name="checkout", file_path=str(path)
    )

    assert "not a dependency file this tool can read" in text
    assert seen == []


async def test_the_refusal_unopened_names_the_renamed_go_mod():
    text = mcp.read_lock_file("settings.py")

    assert "a renamed go.mod" in text


def test_an_sbom_of_two_ecosystems_is_named_without_an_article(tmp_path):
    """HATALAR 60: no "a"/"an" before an ecosystem or format name; "an SBOM"
    is fixed text, and the format goes in brackets."""
    path = tmp_path / "bom.json"
    components = [
        {
            "type": "library",
            "name": "django",
            "version": "5.0",
            "purl": "pkg:pypi/django@5.0",
        },
        {
            "type": "library",
            "name": "left-pad",
            "version": "1.3.0",
            "purl": "pkg:npm/left-pad@1.3.0",
        },
    ]
    path.write_text(
        json.dumps(
            {"bomFormat": "CycloneDX", "specVersion": "1.5", "components": components}
        ),
        encoding="utf-8",
    )

    text = mcp.read_lock_file(str(path))

    assert text.startswith("bom.json is an SBOM (CycloneDX) listing npm, pypi ")


def test_a_missing_relative_path_says_where_it_looked_and_asks_for_absolute(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    text = mcp.read_lock_file("go.mod")

    assert text.startswith(
        f"No file at go.mod: this server looked for {tmp_path.resolve() / 'go.mod'}"
        ". A relative path starts where it runs. Nothing was pushed.\n"
    )
    assert text.endswith(
        "Next step for the agent: call import_project again with the file's "
        "absolute path."
    )


def test_a_home_relative_path_is_said_as_it_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    text = mcp.read_lock_file("~/uv.lock")

    assert f"looked for {tmp_path.resolve() / 'uv.lock'}" in text
    assert "relative path" not in text, "~ is the home, not this directory"


def test_a_missing_absolute_path_is_said_as_given(tmp_path):
    path = tmp_path / "uv.lock"

    text = mcp.read_lock_file(str(path))

    assert text == f"No file at {path}. Nothing was pushed."
    assert "Next step" not in text


@pytest.mark.parametrize(
    "relative, content",
    [
        ("tokens.txt", "sk-live-4f9a8b7c6d5e\nghp_A1b2C3d4E5f6G7h8\n"),
        ("version.txt", "1.4.2\n"),
        ("users.txt", "alice\nbob\ncarol\n"),
        ("deps.txt", "django==5.0\n"),
    ],
    ids=["keys", "version", "words", "unnamed-list"],
)
async def test_a_txt_that_does_not_say_requirements_is_not_read(
    tmp_path, relative, content
):
    path = tmp_path / relative
    path.write_text(content, encoding="utf-8")
    server, seen, _ = _import_world(existing=[])

    text = await _refused(
        server, "import_project", name="checkout", file_path=str(path)
    )

    assert text.startswith(f"{relative} was opened, and its lines read as package ")
    assert "neither its name nor its folder says it is a requirements file" in text
    assert text.endswith("Nothing was pushed.")
    for line in content.split():
        assert line not in text, "nothing read from it is quoted back"
    assert seen == []


@pytest.mark.parametrize(
    "relative, content",
    [("weird.lock", "foo\n"), ("x.mod", "bar\n"), ("data.json", '{"a": 1}')],
    ids=["lock", "mod", "json"],
)
def test_content_outside_what_the_suffix_allows_is_refused(tmp_path, relative, content):
    path = tmp_path / relative
    path.write_text(content, encoding="utf-8")

    text = mcp.read_lock_file(str(path))

    assert text.startswith(f"{relative} was opened, but its content is not")


@pytest.mark.parametrize(
    "relative",
    [
        "requirements-dev.txt",
        "dev-requirements.txt",
        "constraints.txt",
        "requirements/base.txt",
        "requirements.txt",
    ],
)
def test_a_txt_named_for_requirements_is_still_read(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text("django==5.0\nrequests\n", encoding="utf-8")

    read = mcp.read_lock_file(str(path))

    assert not isinstance(read, str), read
    assert [d["name"] for d in read.dependencies] == ["django", "requests"]


def test_a_utf16_requirements_file_is_read_not_refused(tmp_path):
    path = tmp_path / "requirements.txt"
    lines = ["django==5.0", "requests==2.31.0", ""]
    crlf = chr(13) + chr(10)
    path.write_bytes(crlf.join(lines).encode("utf-16"))

    read = mcp.read_lock_file(str(path))

    assert not isinstance(read, str), read
    assert [d["name"] for d in read.dependencies] == ["django", "requests"]
