"""MCP server exposing the PackageTrack archive as tools."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any, Literal

import httpx

from packagetrackdev_mcp import config

REQUEST_TIMEOUT = 60.0

REASON_CHARS = 300

MAX_NOTE_CHARS = 3_000

Ecosystem = Literal["cargo", "composer", "go", "maven", "npm", "nuget", "pub", "pypi"]

SERVER_NAME = "packagetrackdev"

COMMAND = "packagetrackdev-mcp"

AGENT_ADD_COMMAND = "claude mcp add -s user packagetrackdev -- packagetrackdev-mcp"

STARTED_BY_HAND = f"""\
{COMMAND} is started by your coding agent, not by hand. It speaks
the Model Context Protocol on stdin and stdout and has nothing to show a
terminal. Register it once:

    {AGENT_ADD_COMMAND}

Cursor and Windsurf: add `{COMMAND}` under mcpServers; VS Code:
under servers. Then ask the agent what changed in a package."""

INSTRUCTIONS = """PackageTrack holds release histories and changelogs for \
packages on PyPI, npm, crates.io, Packagist, NuGet, pub.dev, the Go module \
index and Maven Central.

Before upgrading a dependency, call package_changes with the installed version \
as `since` (and the target as `until`, if there is one). It returns what the \
maintainers wrote about each release in between, oldest first. Read it before \
changing code: it says what was removed, renamed or deprecated when the \
maintainers wrote that down. A release with no notes is stated as such; that \
silence does not mean nothing changed. package_versions lists what exists, \
including withdrawn releases that must not be installed. project_report needs \
a PackageTrack API key and describes a project the user has pushed with \
`packagetrackdev push`.

These tools read the archive. The one thing they may ask the server to do is \
fetch a package it does not hold yet; nothing else is written. They never see \
the user's code."""

_UNHELD = {
    "not_found": "{registry} has no package named {name}.",
    "scanning": (
        "packagetrack.dev is fetching {name} from {registry} right now. "
        "Ask again in a minute."
    ),
    "requested": (
        "packagetrack.dev did not hold {name} yet; it has been asked to fetch "
        "it from {registry}. Ask again in a minute."
    ),
    "rate_limited": (
        "packagetrack.dev does not hold {name}, and this address has asked for "
        "too many new packages this hour. Try again later or open "
        "{server}/{ecosystem}/{name}."
    ),
    "unknown": (
        "packagetrack.dev does not hold {name} yet. Ask the user to open "
        "{server}/{ecosystem}/{name}, which requests it."
    ),
}

_REGISTRY = {
    "cargo": "crates.io",
    "composer": "Packagist",
    "go": "the Go module index",
    "maven": "Maven Central",
    "npm": "npm",
    "nuget": "NuGet",
    "pub": "pub.dev",
    "pypi": "PyPI",
}


class NoKey(Exception):
    """Raised when a project tool is called without an API key."""


def _version() -> str:
    """Return the installed version, or "0.0.0" when not installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("packagetrackdev-mcp")
    except PackageNotFoundError:
        return "0.0.0"


def response_reason(response: httpx.Response) -> str | None:
    """Extract a one-line reason from an error response, if any."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = response.text
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail)
    text = " ".join((detail or "").split())
    if len(text) > REASON_CHARS:
        text = text[:REASON_CHARS].rstrip() + "..."
    return text or None


def http_failure(exc: httpx.HTTPError, server: str) -> str:
    """Describe a failed request in one line."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return f"Could not reach {server}: {exc}"
    response = exc.response
    reason = response_reason(response)
    if response.status_code == 401:
        text = f"{server} rejected the API key (HTTP 401)"
        return f"{text}: {reason}" if reason else text
    return (
        f"{server} answered HTTP {response.status_code}: "
        f"{reason or response.reason_phrase or 'no reason given'}"
    )


def _day(value: str | None) -> str:
    """Return the date part of an ISO timestamp, or an empty string."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10]


_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|=+\s*|-+\s*)?"  # markdown or underline heading marks
    r"[\[(]?v?(?P<version>\S+?)[\])]?"  # the version, maybe bracketed, maybe v-prefixed
    r"(?:\s*[-:(].*)?$"  # and a date or title after it
)


def _without_own_heading(text: str, version: str) -> str:
    """Drop a leading heading that only repeats the version."""
    head, _, rest = text.strip().partition("\n")
    match = _HEADING.match(head)
    if match and match.group("version").strip("*_`") == version and rest.strip():
        return rest.strip()
    return text.strip()


def _note_text(note: dict[str, Any], version: str) -> str:
    """Render one note, marking a server-side cut."""
    text = _without_own_heading(note["text"], version)
    if not note.get("cut"):
        return text
    shown = len(note["text"])
    more = max(0, int(note.get("chars") or shown) - shown)
    where = f" Full text: {note['url']}" if note.get("url") else ""
    return (
        text
        + f"\n[cut after {shown:,} characters, {more:,} more. Call package_changes "
        f'with until="{version}" and a larger max_chars for the whole note.{where}]'
    )


def _unheld(state: str, ecosystem: str, name: str, server: str) -> str:
    template = _UNHELD.get(state, _UNHELD["unknown"])
    return template.format(
        name=name,
        ecosystem=ecosystem,
        registry=_REGISTRY.get(ecosystem, ecosystem),
        server=server,
    )


def render_changes(data: dict[str, Any], ecosystem: str, name: str, server: str) -> str:
    if data.get("state") != "ok":
        return _unheld(data.get("state", "unknown"), ecosystem, name, server)

    package = data["package"]
    since = data["since"]
    until = data.get("until")
    versions = data["versions"]
    page = f"{server}/{ecosystem}/{package['name']}"
    lines = [f"# {package['name']} ({ecosystem})", f"Page: {page}"]

    if not since["known"]:
        lines.append(
            f"{since['version']} is not a version this archive holds for "
            f"{package['name']}."
        )
        if not versions:
            lines.append(
                "It could not be placed in the release history either; "
                "package_versions lists what exists."
            )
            return "\n".join(lines)
        lines.append("Showing the releases newer than it.")
    if until is not None and not until["known"]:
        lines.append(
            f"{until['version']} is not a version this archive holds; "
            "the range ends at the newest release that is not newer than it."
        )
    if since.get("yanked"):
        reason = since.get("yanked_reason") or "no reason given"
        lines.append(
            f"The installed version {since['version']} was withdrawn by its "
            f"maintainers ({reason.strip()}). package_versions shows which "
            "releases are not withdrawn."
        )

    target = until["version"] if until else package.get("latest_stable_version")
    head = f"Releases after {since['version']}"
    if target:
        head += f" up to {target}"
    if data["total"] == 0:
        lines.append(head + ": none. Nothing to change.")
        return "\n".join(lines)
    if data.get("truncated"):
        head += (
            f": {data['total']}, showing the first {len(versions)}, oldest first. "
            f'Call again with since="{data["next_since"]}" for the rest.'
        )
    else:
        head += f": {data['total']}, oldest first."
    lines.append(head)
    if package.get("latest_stable_version"):
        lines.append(f"Latest stable release: {package['latest_stable_version']}.")

    silent: list[str] = []

    def flush() -> None:
        if not silent:
            return
        lines.append("")
        if len(silent) == 1:
            lines.append(f"## {silent[0]}\nNo release notes found for this version.")
        else:
            lines.append(
                f"## {silent[0]} to {silent[-1]}\nNo release notes found for these "
                f"{len(silent)} versions."
            )
        silent.clear()

    for version in versions:
        notes = version.get("notes") or []
        if not notes and not version.get("yanked"):
            silent.append(version["version"])
            continue
        flush()
        title = f"## {version['version']}"
        day = _day(version.get("published_at"))
        if day:
            title += f" ({day})"
        if version.get("is_prerelease"):
            title += " prerelease"
        lines.append("")
        lines.append(title)
        if version.get("yanked"):
            reason = version.get("yanked_reason") or "no reason given"
            lines.append(
                f"WITHDRAWN by the maintainers ({reason.strip()}). Do not install."
            )
        if not notes:
            lines.append("No release notes found for this version.")
            continue
        primary = notes[0]
        lines.append(_note_text(primary, version["version"]))
        trailer = []
        if primary.get("url"):
            trailer.append(f"Source: {primary['url']}")
        if (primary.get("confirmed_by") or 1) > 1:
            trailer.append(f"confirmed by {primary['confirmed_by']} sources")
        if len(notes) > 1:
            trailer.append(f"{len(notes) - 1} more distinct note(s) at {page}")
        if trailer:
            lines.append("; ".join(trailer))
    flush()
    return "\n".join(lines)


def render_versions(
    data: dict[str, Any], ecosystem: str, name: str, server: str
) -> str:
    if data.get("state") != "ok":
        return _unheld(data.get("state", "unknown"), ecosystem, name, server)

    package = data["package"]
    lines = [f"# {package['name']} ({ecosystem})"]
    if package.get("summary"):
        lines.append(package["summary"])
    facts = []
    if package.get("latest_stable_version"):
        facts.append(f"latest stable {package['latest_stable_version']}")
    if package.get("version_count"):
        facts.append(f"{package['version_count']} releases")
    if package.get("last_release_at"):
        facts.append(f"last release {_day(package['last_release_at'])}")
    if facts:
        lines.append(", ".join(facts) + ".")
    if package.get("source_repo_url"):
        lines.append(f"Source repository: {package['source_repo_url']}")
    lines.append(f"Page: {server}/{ecosystem}/{package['name']}")

    versions = data.get("versions") or []
    lines.append("")
    lines.append("Releases, newest first:")
    for version in versions:
        row = f"- {version['version']}"
        day = _day(version.get("published_at"))
        if day:
            row += f"  {day}"
        if version.get("is_prerelease"):
            row += "  prerelease"
        if version.get("yanked"):
            row += "  WITHDRAWN"
        lines.append(row)
    if data.get("has_older"):
        lines.append(
            "Older releases are on the page above; package_changes walks any range."
        )
    return "\n".join(lines)


def render_search(data: dict[str, Any], query: str, server: str) -> str:
    rows = data.get("rows") or []
    if not rows:
        return f"Nothing in the archive matches {query!r}."
    total = int(data.get("total") or len(rows))
    lines = [f"{total} packages match {query!r}:"]
    for row in rows[:20]:
        line = f"- {row['name']} ({row['purl_type']})"
        if row.get("summary"):
            line += f": {row['summary']}"
        if row.get("last_release"):
            line += f"  [last release {_day(row['last_release'])}]"
        lines.append(line)
    if total > 20:
        lines.append(f"...and {total - 20} more on {server}/search?q={query}")
    return "\n".join(lines)


def render_projects(data: dict[str, Any], server: str) -> str:
    projects = data.get("projects") or []
    if not projects:
        return (
            "No projects yet. `packagetrackdev push --name <project>` in a folder with "
            "a lock file creates one."
        )
    lines = ["Projects pushed to PackageTrack:"]
    for project in projects:
        line = (
            f"- {project['name']} ({project['purl_type']}): {project['total']} "
            f"packages, {project['direct']} direct"
        )
        measured = project.get("measured")
        if measured is not None:
            line += f", {measured} compared against the archive"
        bits = []
        if project.get("behind_direct"):
            bits.append(f"{project['behind_direct']} of yours behind")
        if project.get("yanked"):
            bits.append(f"{project['yanked']} withdrawn")
        if project.get("unpinned"):
            bits.append(f"{project['unpinned']} unpinned")
        if project.get("unknown_package"):
            bits.append(f"{project['unknown_package']} not in the archive")
        if bits:
            line += "; " + ", ".join(bits)
        if project.get("snapshot_age_days") is not None:
            line += f"; pushed {project['snapshot_age_days']} days ago"
        if project.get("paused"):
            line += (
                "; PAUSED: over the account's project limit, so it receives "
                "no digest or alerts"
            )
        lines.append(line)
    return "\n".join(lines)


def render_report(data: dict[str, Any], server: str) -> str:
    name = data["project"]
    lines = [f"# {name} ({data['purl_type']})"]
    withdrawn = data.get("withdrawn") or []
    packages = data.get("packages") or []
    if not withdrawn and not packages:
        lines.append(
            "Nothing behind and nothing withdrawn among the dependencies the "
            "archive holds. list_projects says how many were compared."
        )
        return "\n".join(lines)

    if withdrawn:
        lines.append("")
        lines.append("## Withdrawn versions in this build")
        for item in withdrawn:
            row = f"- {item['name']} {item['version']}"
            if item.get("parents"):
                row += f" (pulled in by {', '.join(item['parents'][:3])})"
            reason = item.get("reason") or "no reason given"
            row += f": {reason.strip()}"
            lines.append(row)

    if packages:
        lines.append("")
        lines.append("## Behind")
        for package in packages:
            lines.append("")
            lines.append(
                f"### {package['name']} {package['installed']} -> "
                f"{package['latest_stable']}  ({package['versions_behind']} behind)"
            )
            for release in package.get("releases") or []:
                title = f"- {release['version']}"
                day = _day(release.get("published_at"))
                if day:
                    title += f" ({day})"
                lines.append(title)
                for line in release.get("lines") or []:
                    label = f"[{line['label'].upper()}] " if line.get("label") else ""
                    lines.append(f"  - {label}{line['text']}")
                if release.get("source_url"):
                    lines.append(f"  Source: {release['source_url']}")
            lines.append(
                f'  Full notes: package_changes("{package["purl_type"]}", '
                f'"{package["name"]}", since="{package["installed"]}")'
            )
    lines.append("")
    lines.append(f"Report page: {server}/app/projects/{name}")
    return "\n".join(lines)


class Archive:
    """HTTP client for the PackageTrack API."""

    def __init__(
        self, settings: config.Config, client: httpx.AsyncClient | None = None
    ):
        self.server = settings.server
        self.api_key = settings.api_key
        self._client = client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": f"packagetrackdev-mcp/{_version()}"},
            follow_redirects=True,
        )

    async def public(self, path: str, **params: Any) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.server}{path}",
            params={k: v for k, v in params.items() if v is not None},
        )
        response.raise_for_status()
        return response.json()

    async def request_scan(self, ecosystem: str, name: str) -> str:
        """Request a scan of a package; return the resulting state."""
        response = await self._client.post(
            f"{self.server}/api/public/scan/{ecosystem}/{name}"
        )
        response.raise_for_status()
        state = response.json().get("state", "unknown")
        return "requested" if state == "scanning" else state

    async def private(self, path: str) -> dict[str, Any]:
        if not self.api_key:
            raise NoKey
        response = await self._client.get(
            f"{self.server}{path}",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        response.raise_for_status()
        return response.json()

    def _key_howto(self) -> str:
        return (
            f"Keys are created at {self.server}/app/keys; an account is "
            f"created at {self.server}/register. "
            "Then start the server with PACKAGETRACK_API_KEY set, or run "
            "`packagetrackdev login --api-key pkgt_...` (the PackageTrack CLI) "
            "so the key is stored on this machine. The key does not belong in "
            "the agent's config file."
        )

    def no_key(self) -> str:
        return (
            "This tool needs a PackageTrack API key and none is configured. "
            + self._key_howto()
        )

    def failed(self, exc: httpx.HTTPError) -> str:
        """Describe a failed request in one line."""
        text = http_failure(exc, self.server)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
            text = text.rstrip(".") + ". " + self._key_howto()
        return text


async def _held_or_requested(
    archive: Archive, data: dict[str, Any], ecosystem: str, name: str
) -> dict[str, Any]:
    """Request a package the archive does not hold yet."""
    if data.get("state") != "unknown":
        return data
    try:
        state = await archive.request_scan(ecosystem, name)
    except httpx.HTTPError:
        return data
    return {**data, "state": state}


def build_server(
    settings: config.Config | None = None, client: httpx.AsyncClient | None = None
):
    """Build the MCP server with its tools registered."""
    from mcp.server.mcpserver import MCPServer

    archive = Archive(settings or config.load(), client)
    server = MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        website_url="https://packagetrack.dev",
        version=_version(),
    )

    @server.tool(
        name="package_changes",
        description=(
            "What the maintainers wrote about each release between two versions "
            "of a package, oldest first; a release with no notes is said to have "
            "none, and a withdrawn release is flagged. Call this before upgrading "
            "a dependency, with the installed version as `since` and the target "
            "as `until` (omit `until` for everything up to the newest release). "
            "Long ranges are paged and long notes are cut; the answer says how "
            "to get the rest, and `max_chars` raises the cut for one note."
        ),
    )
    async def package_changes(
        ecosystem: Ecosystem,
        name: str,
        since: str,
        until: str | None = None,
        include_prereleases: bool = False,
        max_chars: int | None = None,
    ) -> str:
        try:
            data = await archive.public(
                f"/api/public/changes/{ecosystem}/{name}",
                since=since,
                until=until,
                prerelease="true" if include_prereleases else None,
                max_chars=max_chars,
            )
            data = await _held_or_requested(archive, data, ecosystem, name)
        except httpx.HTTPError as exc:
            return archive.failed(exc)
        return render_changes(data, ecosystem, name, archive.server)

    @server.tool(
        name="package_versions",
        description=(
            "What a package is and which versions of it exist: summary, source "
            "repository, latest stable release, and the newest releases with "
            "their dates, prerelease and withdrawn flags. Use it to pick an "
            "upgrade target or to check that a version has not been withdrawn."
        ),
    )
    async def package_versions(ecosystem: Ecosystem, name: str) -> str:
        try:
            data = await archive.public(f"/api/public/package/{ecosystem}/{name}")
            data = await _held_or_requested(archive, data, ecosystem, name)
        except httpx.HTTPError as exc:
            return archive.failed(exc)
        return render_versions(data, ecosystem, name, archive.server)

    @server.tool(
        name="search_packages",
        description=(
            "Find packages in the archive by name. Returns the matches across "
            "every registry with a one-line summary each."
        ),
    )
    async def search_packages(query: str) -> str:
        try:
            data = await archive.public("/api/public/search", q=query)
        except httpx.HTTPError as exc:
            return archive.failed(exc)
        return render_search(data, query, archive.server)

    @server.tool(
        name="list_projects",
        description=(
            "The user's projects on PackageTrack (pushed with `packagetrackdev push`): "
            "how many dependencies each has, how many the archive could compare, "
            "and how many are behind or withdrawn. Needs a PackageTrack API key."
        ),
    )
    async def list_projects() -> str:
        try:
            data = await archive.private("/api/v1/projects")
        except NoKey:
            return archive.no_key()
        except httpx.HTTPError as exc:
            return archive.failed(exc)
        return render_projects(data, archive.server)

    @server.tool(
        name="project_report",
        description=(
            "What PackageTrack tells the user about one of their projects: "
            "withdrawn versions in the build, each dependency that is behind "
            "with the notable lines from the newest releases it is missing, and "
            "where the full notes are. Needs a PackageTrack API key; "
            "list_projects gives the names."
        ),
    )
    async def project_report(name: str) -> str:
        try:
            data = await archive.private(f"/api/v1/projects/{name}/report")
        except NoKey:
            return archive.no_key()
        except httpx.HTTPError as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 404
            ):
                return f"No project named {name!r}. list_projects shows the names."
            return archive.failed(exc)
        return render_report(data, archive.server)

    return server


def started_by_hand() -> bool:
    """Return True if stdin is a terminal."""
    stream = sys.stdin
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError):
        return False


def serve(settings: config.Config) -> int:
    """Run the server over stdio."""
    if started_by_hand():
        print(STARTED_BY_HAND, file=sys.stderr)
        return 2
    build_server(settings).run("stdio")
    return 0
