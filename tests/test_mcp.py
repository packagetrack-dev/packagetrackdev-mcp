"""`packagetrackdev-mcp`: the archive as tools for a coding agent."""

from __future__ import annotations

import json

import httpx
import pytest
from packagetrackdev_mcp import config
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
    }
    for name in ("list_projects", "project_report"):
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
    assert "Releases after 0.24.1 up to 0.28.1: 4, oldest first." in text
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


async def test_package_changes_unknown_since():
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


async def test_not_found_package():
    unheld = {"state": "not_found", "name": "left-pad", "purl_type": "npm"}
    server, seen = _server(lambda request: _ok(unheld))
    text = await _text(server, "package_versions", ecosystem="npm", name="left-pad")
    assert text == "npm has no package named left-pad."
    assert [r.method for r in seen] == ["GET"], "nothing to ask for"


async def test_project_tools_without_key():
    server, seen = _server(lambda request: _ok({}))
    text = await _text(server, "list_projects")
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
    text = await _text(server, "project_report", name="api")
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
    text = await _text(server, "list_projects")
    assert text == f"https://pt.test answered HTTP 403: {detail}"
    assert "Could not reach" not in text
    assert "mozilla" not in text
    assert "pkgt_x" not in text


async def test_project_report_unknown():
    server, _ = _server(lambda request: httpx.Response(404), api_key="pkgt_x")
    text = await _text(server, "project_report", name="nope")
    assert text == "No project named 'nope'. list_projects shows the names."


async def test_http_status_errors():
    server, _ = _server(
        lambda request: httpx.Response(404, json={"detail": "Not here."}),
        api_key="pkgt_x",
    )
    assert (
        await _text(server, "list_projects")
        == "https://pt.test answered HTTP 404: Not here."
    )
    server, _ = _server(lambda request: httpx.Response(404), api_key="pkgt_x")
    assert (
        await _text(server, "list_projects")
        == "https://pt.test answered HTTP 404: Not Found"
    )
    server, _ = _server(
        lambda request: httpx.Response(429, text="slow down"), api_key="pkgt_x"
    )
    assert (
        await _text(server, "list_projects")
        == "https://pt.test answered HTTP 429: slow down"
    )


async def test_http_error_page_truncated():
    page = "<html>\n  <body>\n" + "Bad Gateway. " * 100 + "</body>\n</html>"
    server, _ = _server(lambda request: httpx.Response(502, text=page))
    text = await _text(server, "search_packages", query="httpx")
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
    text = await _text(server, "search_packages", query="httpx")
    assert text.startswith("Could not reach https://pt.test: ")
    server, _ = _server(down, api_key="pkgt_x")
    text = await _text(server, "list_projects")
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
