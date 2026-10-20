"""MCP server exposing the PackageTrack archive as tools."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NoReturn, get_args

if sys.version_info >= (3, 12):
    from typing import Required, TypedDict
else:
    from typing import Required

    from typing_extensions import TypedDict

import httpx
from pydantic import BaseModel

from packagetrackdev_mcp import config, messages
from packagetrackdev_mcp._vendor import lockfiles, sbom

REQUEST_TIMEOUT = 60.0

REASON_CHARS = 300

MAX_NOTE_CHARS = 3_000
MAX_CHARS_FLOOR = 200
MAX_CHARS_CEILING = 20_000

Ecosystem = Literal["cargo", "composer", "go", "maven", "npm", "nuget", "pub", "pypi"]


class Dependency(TypedDict, total=False):
    """One line of a pushed dependency set, as the import endpoint takes it."""

    name: Required[str]
    version: Required[str]
    parents: list[str]
    indirect: bool


MAX_DEPENDENCIES = 2_000

MAX_FILE_BYTES = 2 * 1024 * 1024


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
including withdrawn releases that must not be installed. When the registry \
marks the whole package deprecated, both tools say so first, with the \
replacement the registry names if there is one: it still installs, but its \
maintainers ask people to move off it. project_report needs \
a PackageTrack API key and describes a project the user has pushed with \
`packagetrackdev push` or import_project. import_project pushes a dependency \
set read out of a lock file: a new name is a new project, and a name that \
exists has its dependency list updated, so use the name the user means.

Every other tool reads. What they send is package names and version strings: \
no file paths, no file contents, no code. import_project can read a lock file \
on this machine; only the names and versions in it are sent."""

_UNHELD = {
    "not_found": "{registry} has no package named {name}.",
    "scanning": (
        "{host} is fetching {name} from {registry} right now. Ask again when "
        "that finishes."
    ),
    "requested": (
        "{host} did not hold {name} yet; it has been asked to fetch "
        "it from {registry}. Ask again when that finishes."
    ),
    "unknown": (
        "{host} does not hold {name} yet. Ask the user to open "
        "{server}/{ecosystem}/{name}, which requests it."
    ),
}

SCAN_RATE_LIMITED = (
    "{server} answered HTTP 429: Too many new packages from this address this "
    "hour. Try again later."
)
SCAN_QUEUE_FULL = (
    "{server} answered HTTP 429: PackageTrack's lookup queue is full for this "
    "hour. Try again later."
)

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


def _deprecation_lines(
    package: dict[str, Any], ecosystem: str, server: str
) -> list[str]:
    """Say the registry's package-level "stop using this", or nothing."""
    deprecation = package.get("deprecation")
    if not deprecation or not deprecation.get("notice"):
        return []
    registry = _REGISTRY.get(ecosystem, ecosystem)
    lines = [
        f"DEPRECATED on {registry}: {deprecation['notice'].strip()}",
        "It still installs; its maintainers ask people to move off it.",
    ]
    replacement = deprecation.get("replacement")
    if replacement and replacement.get("name"):
        lines.append(
            f"The registry names {replacement['name']} "
            f"({replacement.get('purl_type', ecosystem)}) as its replacement: "
            f"{server}{replacement.get('path', '')}"
        )
    return lines


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


def _plural(count: int, noun: str) -> str:
    """`1 package`, `2 packages`. Counts an agent reads should read as English."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def not_a_version(value: str, field: str, ecosystem: str, name: str) -> str:
    """The refusal for a bound the ecosystem's scheme cannot read."""
    registry = _REGISTRY.get(ecosystem, ecosystem)
    want = (
        "the version installed now"
        if field == "since"
        else "the version to upgrade to, or leave it out for everything newer"
    )
    return (
        f"{value!r} is not a {registry} version, so it cannot bound the "
        f"release history of {name}. Pass {want} as {field}; package_versions "
        "lists what exists."
    )


def _pushed_ago(days: int) -> str:
    """`today`, `1 day ago`, `3 days ago`: "0 days ago" read as a glitch."""
    if days <= 0:
        return "today"
    return f"{_plural(days, 'day')} ago"


def _host(server: str) -> str:
    """The server's host on its own, for a sentence that names it."""
    try:
        url = httpx.URL(server)
    except (httpx.InvalidURL, ValueError):
        return server
    if not url.host:
        return server
    return f"{url.host}:{url.port}" if url.port else url.host


def _unheld(state: str, ecosystem: str, name: str, server: str) -> str:
    template = _UNHELD.get(state, _UNHELD["unknown"])
    return template.format(
        name=name,
        ecosystem=ecosystem,
        registry=_REGISTRY.get(ecosystem, ecosystem),
        server=server,
        host=_host(server),
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
    lines.extend(_deprecation_lines(package, ecosystem, server))

    if not since["known"]:
        lines.append(
            f"{since['version']} is not a version this archive holds for "
            f"{package['name']}."
        )
        if not versions and since.get("readable") is True:
            newest = package.get("latest_stable_version")
            held = f" (newest held: {newest})" if newest else ""
            lines.append(
                f"No release this archive holds is newer than it{held}. The "
                f"archive may not have caught up with "
                f"{_REGISTRY.get(ecosystem, ecosystem)} yet; package_versions "
                "shows what it holds."
            )
            left_out = data.get("prereleases_left_out") or 0
            if left_out:
                lines.append(
                    f"{left_out} prerelease(s) after it were left out; call "
                    "again with include_prereleases=true to read them."
                )
            return "\n".join(lines)
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

    if until is not None and until.get("older_than_since"):
        lines.append(
            f"{until['version']} is older than {since['version']}: that is a "
            "downgrade. What it would undo is package_changes with "
            f'since="{until["version"]}" and until="{since["version"]}".'
        )
        return "\n".join(lines)

    left_out = data.get("prereleases_left_out") or 0
    if until is not None:
        target = until["version"]
    elif versions and not data.get("truncated"):
        target = versions[-1]["version"]
    else:
        target = None
    head = f"Releases after {since['version']}"
    if target:
        head += f" up to {target}"
    if data["total"] == 0:
        if left_out:
            lines.append(
                f"{head}: no stable release. {left_out} prerelease(s) after it "
                "were left out; call again with include_prereleases=true to "
                "read them."
            )
        else:
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
    if left_out:
        lines.append(
            f"{left_out} prerelease(s) in this range were left out; "
            "include_prereleases=true lists them."
        )
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
    lines.extend(_deprecation_lines(package, ecosystem, server))
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


def _rule_lines(data: dict[str, Any]) -> list[str]:
    """Return what the organization's rules made of a stored push."""
    note = data.get("rules_note")
    if note:
        return [f"Pushed. {note} There is nothing to retry."]
    unmet = [v for v in data.get("rules") or [] if not v.get("met")]
    if not unmet:
        return []
    lines = [
        "Pushed. Organization rule not met "
        f"({'fail' if v.get('action') == 'fail' else 'warn'}): {v.get('message')}"
        for v in unmet
    ]
    if data.get("unmeasured_note"):
        lines.append(data["unmeasured_note"])
    lines.append(
        "The push is stored; a rule not met does not undo it, so there is "
        "nothing to retry."
    )
    return lines


def render_import(
    data: dict[str, Any],
    count: int,
    server: str,
    read: LockFile | None = None,
    *,
    sent: list[dict[str, Any]] | None = None,
) -> str:
    """Say what the push did, and where to look."""
    name = data.get("project_name") or ""
    opening = messages.outcome(data, count)
    blocks: list[str | None] = []
    if data.get("created"):
        blocks = [
            messages.imported_digest(data, count),
            messages.imported_examples(sent, messages.stored_count(data, count)),
        ]
    elif not data.get("unchanged"):
        blocks = [messages.update_digest(data)]
    shown = [block for block in blocks if block]
    if shown:
        opening = "\n\n".join([opening, *shown]) + "\n"
    caveats = []
    if read is not None:
        caveats.append(f"Source file: {read.source_file}.")
    if read is not None and read.skipped:
        caveats.append(
            f"{read.skipped} line{'' if read.skipped == 1 else 's'} of the file "
            f"{'was' if read.skipped == 1 else 'were'} not a dependency and "
            "skipped (options such as --index-url, or unreadable entries)."
        )
    if read is not None and read.note:
        caveats.append(read.note)
    pointer = messages.report_pointer(name)
    return "\n".join(
        [opening, *caveats, *_rule_lines(data), *messages.lookup_lines(data), pointer]
    )


def render_projects(data: dict[str, Any], server: str) -> str:
    projects = data.get("projects") or []
    if not projects:
        return (
            "No projects yet. import_project with the lock file's path creates "
            "one, as does `packagetrackdev push --name <project>` in a folder "
            "with a lock file."
        )
    lines = ["Projects pushed to PackageTrack:"]
    for project in projects:
        line = (
            f"- {project['name']} ({project['purl_type']}): "
            f"{_plural(project['total'], 'package')}, {project['direct']} direct"
        )
        measured = project.get("measured")
        if measured is not None:
            line += f", {measured} compared against the archive"
        bits = []
        if project.get("behind_direct"):
            bits.append(f"{project['behind_direct']} of yours behind")
        if project.get("behind_indirect"):
            bits.append(f"{project['behind_indirect']} behind through a parent")
        if project.get("yanked"):
            bits.append(f"{project['yanked']} withdrawn")
        if project.get("unpinned"):
            bits.append(f"{project['unpinned']} unpinned")
        if project.get("unknown_package"):
            bits.append(f"{project['unknown_package']} not in the archive")
        if project.get("scanning"):
            bits.append(f"{project['scanning']} being looked up now")
        if project.get("unplaced_version"):
            bits.append(
                f"{project['unplaced_version']} pinned to a version the "
                "archive does not hold"
            )
        if bits:
            line += "; " + ", ".join(bits)
        if project.get("snapshot_age_days") is not None:
            line += f"; pushed {_pushed_ago(project['snapshot_age_days'])}"
        if project.get("paused"):
            line += (
                "; PAUSED: over the account's project limit, so it receives "
                "no digest or alerts"
            )
        lines.append(line)
    return "\n".join(lines)


def _uncompared(summary: dict[str, Any] | None) -> str | None:
    """How much of the project the archive could not compare, as a sentence.

    The report itself carries no counts, so a caller that has the project's
    row from the listing passes it in. Silence here used to read as an all
    clear: a project where nothing at all could be compared was answered with
    "nothing behind", which is the one reading that is never safe.
    """
    if not summary:
        return None
    total = summary.get("total")
    measured = summary.get("measured")
    if total is None or measured is None or measured >= total:
        return None
    missing = total - measured
    causes = []
    if summary.get("scanning"):
        causes.append(f"{summary['scanning']} still being looked up")
    if summary.get("unplaced_version"):
        causes.append(
            f"{summary['unplaced_version']} pinned to a version the archive "
            "does not hold"
        )
    if summary.get("unknown_package"):
        causes.append(f"{summary['unknown_package']} not in the archive")
    if summary.get("unpinned"):
        causes.append(f"{summary['unpinned']} a range rather than a version")
    tail = f" ({', '.join(causes)})" if causes else ""
    return (
        f"{measured} of {_plural(total, 'package')} were compared against the "
        f"archive; {missing} could not be{tail}. This is not an all clear for "
        "the whole project."
    )


def render_report(
    data: dict[str, Any], server: str, summary: dict[str, Any] | None = None
) -> str:
    name = data["project"]
    lines = [f"# {name} ({data['purl_type']})"]
    withdrawn = data.get("withdrawn") or []
    packages = data.get("packages") or []
    uncompared = _uncompared(summary)
    if not withdrawn and not packages:
        if uncompared:
            lines.append(
                "Nothing behind and nothing withdrawn among the dependencies "
                "the archive compared."
            )
            lines.append(uncompared)
        else:
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

    indirect = (summary or {}).get("behind_indirect") or 0
    if indirect:
        lines.append("")
        lines.append(
            f"{indirect} more {'is' if indirect == 1 else 'are'} behind through "
            "a parent and not listed here; the report page has them."
        )
    if uncompared:
        lines.append("")
        lines.append(uncompared)
    lines.append("")
    lines.append(f"Report page: {server}/app/projects/{name}")
    return "\n".join(lines)


class LockFile(BaseModel):
    """A dependency file read and parsed here, ready to push."""

    purl_type: str
    source_file: str
    dependencies: list[dict[str, Any]]
    skipped: int = 0
    note: str | None = None


SUPPORTED_FILES_SENTENCE = (
    "Supported: "
    + ", ".join(lockfiles.SUPPORTED_FILES)
    + ", variants such as requirements-dev.txt or a renamed go.mod, and SBOM "
    ".json files."
)


def _content_against_name(path: Path, detected: str, content: str) -> str | None:
    """Refuse content its file's name does not allow, or return None."""
    reason = lockfiles.refusal(detected, path.name, content, path.parent.name)
    if reason is None:
        return None
    if reason == lockfiles.NOT_NAMED_REQUIREMENTS:
        return (
            f"{path.name} was opened, and its lines read as package names, but "
            "neither its name nor its folder says it is a requirements file, "
            "so nothing was read from it. A list of words or keys reads the "
            "same way. If it is a requirements file, pass a copy named "
            "requirements.txt, or send the dependencies yourself with "
            "purl_type. Nothing was pushed."
        )
    return (
        f"{path.name} was opened, but its content is not a dependency file this "
        f"tool can read. {SUPPORTED_FILES_SENTENCE} Nothing was pushed."
    )


def _looked_at(file_path: str, path: Path) -> str:
    """Return the path as given, with the full path it resolved to if different."""
    full = str(path.resolve())
    return file_path if full == file_path else f"{file_path} ({full})"


def _missing_file(file_path: str, path: Path) -> str:
    """Say that no file is there, where it was looked for, and what to send."""
    if Path(file_path).is_absolute():
        return f"No file at {file_path}. Nothing was pushed."
    where = "" if path.is_absolute() else " A relative path starts where it runs."
    return (
        f"No file at {file_path}: this server looked for {path.resolve()}.{where} "
        "Nothing was pushed.\n"
        + messages.next_step(
            "call import_project again with the file's absolute path."
        )
    )


def read_lock_file(file_path: str) -> LockFile | str:
    """Read and parse a dependency file: the result, or why not."""
    path = Path(file_path).expanduser()
    base = path.name
    if not lockfiles.may_open(base):
        return (
            f"{base!r} is not a dependency file, so it was not opened. "
            f"{SUPPORTED_FILES_SENTENCE} Nothing was pushed."
        )
    try:
        if not path.is_file():
            return _missing_file(file_path, path)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return (
                f"{base} is {size:,} bytes; this tool reads at most "
                f"{MAX_FILE_BYTES:,}. `packagetrackdev push` reads it instead. "
                "Nothing was pushed."
            )
        raw = path.read_bytes()
    except OSError as exc:
        return (
            f"Could not read {_looked_at(file_path, path)}: "
            f"{exc.strerror or exc}. Nothing was pushed."
        )
    content = lockfiles.decode(raw)
    if content is None:
        return (
            f"{base} is neither UTF-8 nor UTF-16 text, so not a lock file. "
            "Nothing was pushed."
        )

    found = lockfiles.detect(content, base)
    if found is None:
        reading = sbom.read(content)
        groups = sorted(reading.by_ecosystem()) if reading else []
        if len(groups) > 1:
            return (
                f"{base} is an SBOM ({reading.format}) listing {', '.join(groups)} "
                "packages. A project is one ecosystem; `packagetrackdev push` "
                "splits it. Nothing was pushed."
            )
        return (
            f"{base} was opened, but its content is not a dependency file this "
            f"tool can read. {SUPPORTED_FILES_SENTENCE} Nothing was pushed."
        )
    ecosystem, detected = found
    refused = _content_against_name(path, detected, content)
    if refused:
        return refused
    if ecosystem not in get_args(Ecosystem):
        return f"Files for {ecosystem!r} are not supported yet. Nothing was pushed."

    parsed = lockfiles.parse(ecosystem, detected, content)
    if parsed is None:
        looks = "is" if base.lower() == detected.lower() else "looks like"
        return (
            f"{base} {looks} {detected} but could not be read as one. "
            "Nothing was pushed."
        )
    if not parsed.dependencies:
        text = messages.EMPTY_FILE.format(file=base)
        if parsed.skipped == 1:
            text += " 1 entry in it could not be read as a dependency."
        elif parsed.skipped:
            text += (
                f" {parsed.skipped:,} entries in it could not be read as "
                "dependencies."
            )
        return text

    note = parsed.note
    if note is None and detected in lockfiles.DECLARATIONS:
        note = (
            f"{detected} is a declaration, not a lock file: only direct "
            "dependencies were imported, with versions only where they are pinned."
        )
    return LockFile(
        purl_type=ecosystem,
        source_file=detected,
        dependencies=[
            {
                "name": d.name,
                "version": d.version,
                "parents": list(d.parents[:20]),
                "indirect": d.indirect,
            }
            for d in parsed.dependencies
        ],
        skipped=parsed.skipped,
        note=note,
    )


MAX_NAME_LENGTH = 64


def name_problem(name: str) -> str | None:
    """Return why this project name is unusable, or None."""
    if not name:
        return "A project needs a name."
    if len(name) > MAX_NAME_LENGTH:
        return f"Project names are at most {MAX_NAME_LENGTH} characters."
    if not all(c.isalnum() or c in "-_." for c in name):
        return "Project names may use letters, digits, dot, dash and underscore."
    if name.startswith(".") or name in {".", ".."}:
        return "That is not a usable project name."
    return None


def push_refusal(exc: httpx.HTTPError) -> str | None:
    """Return the sentence for a refusal the server named with a code, or None."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code >= 500:
        return None
    try:
        body = exc.response.json()
    except ValueError:
        return None
    return messages.refusal(body)


class Archive:
    """HTTP client for the PackageTrack API."""

    def __init__(
        self, settings: config.Config, client: httpx.AsyncClient | None = None
    ):
        self.server = settings.server
        self.api_key = settings.api_key
        self._key_source = config.key_source(settings.api_key)
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
        body = response.json()
        state = body.get("state", "unknown")
        if state == "rate_limited" and body.get("limit") == "visitors":
            return "queue_full"
        return "requested" if state == "scanning" else state

    async def private(self, path: str) -> dict[str, Any]:
        return await self._with_key(
            lambda key: self._client.get(
                f"{self.server}{path}", headers={"Authorization": f"Bearer {key}"}
            )
        )

    async def private_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a body with the key. The only method here that writes."""
        return await self._with_key(
            lambda key: self._client.post(
                f"{self.server}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
        )

    async def _with_key(self, send) -> dict[str, Any]:
        """Send with the key; after a 401, re-read a changed key and resend once."""
        if not self.api_key:
            raise NoKey
        response = await send(self.api_key)
        if response.status_code == 401 and self._reread_key():
            response = await send(self.api_key)
        response.raise_for_status()
        return response.json()

    def _reread_key(self) -> bool:
        """Take the file's key if it changed since this one; True if it did."""
        if self._key_source != "file":
            return False
        fresh = config.load().api_key
        if not fresh or fresh == self.api_key:
            return False
        self.api_key = fresh
        return True

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
            text = text.rstrip(".") + ". The key may be old or revoked. "
            if self._key_source == "env":
                text += (
                    "It comes from PACKAGETRACK_API_KEY: set a current key there "
                    "and restart this server. "
                )
            elif self._key_source == "flag":
                text += (
                    "It was passed with --api-key: change it in the agent's "
                    "configuration and restart this server. "
                )
            else:
                text += (
                    "Run `packagetrackdev login --api-key pkgt_...` with a current "
                    "key; this server reads it on the next call, no restart. "
                )
            text += f"Keys are created at {self.server}/app/keys."
        return text

    def push_failed(self, exc: httpx.HTTPError) -> str:
        """Describe a failed push that no refusal code named."""
        if not isinstance(exc, httpx.HTTPStatusError):
            return messages.OPERATION_UNCONFIRMED + messages.details(
                http_failure(exc, self.server)
            )
        status = exc.response.status_code
        if status == 401:
            return f"{messages.OPERATION_FAILED}\n{self.failed(exc)}"
        reason = response_reason(exc.response)
        if status >= 500:
            return messages.OPERATION_UNCONFIRMED + messages.details(reason)
        return messages.failed(reason)


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
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    def refuse(text: str) -> NoReturn:
        raise ToolError(text)

    def held(data: dict[str, Any]) -> dict[str, Any]:
        if data.get("state") == "rate_limited":
            refuse(SCAN_RATE_LIMITED.format(server=archive.server))
        if data.get("state") == "queue_full":
            refuse(SCAN_QUEUE_FULL.format(server=archive.server))
        return data

    reads = ToolAnnotations(read_only_hint=True)
    pushes = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True
    )

    archive = Archive(settings or config.load(), client)
    server = MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        website_url="https://packagetrack.dev",
        version=_version(),
    )

    @server.tool(
        name="package_changes",
        annotations=reads,
        description=(
            "What the maintainers wrote about each release between two versions "
            "of a package, oldest first; a release with no notes is said to have "
            "none, and a withdrawn release is flagged. Call this before upgrading "
            "a dependency, with the installed version as `since` and the target "
            "as `until` (omit `until` for everything up to the newest release). "
            "Long ranges are paged and long notes are cut; the answer says how "
            "to get the rest, and `max_chars` (200 to 20,000) raises the cut "
            "for one note."
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
        notice = None
        if max_chars is not None and not (
            MAX_CHARS_FLOOR <= max_chars <= MAX_CHARS_CEILING
        ):
            adjusted = min(max(max_chars, MAX_CHARS_FLOOR), MAX_CHARS_CEILING)
            notice = (
                f"max_chars={max_chars:,} is outside what the server accepts "
                f"({MAX_CHARS_FLOOR:,} to {MAX_CHARS_CEILING:,}); notes were "
                f"cut at {adjusted:,}."
            )
            max_chars = adjusted
        if not since.strip():
            refuse(
                "since is empty. Pass the version installed now; "
                "package_versions lists what exists."
            )
        if until is not None and not until.strip():
            until = None
        try:
            data = await archive.public(
                f"/api/public/changes/{ecosystem}/{name}",
                since=since,
                until=until,
                prerelease="true" if include_prereleases else None,
                max_chars=max_chars,
            )
            data = held(await _held_or_requested(archive, data, ecosystem, name))
        except httpx.HTTPError as exc:
            refuse(archive.failed(exc))
        if data.get("state") == "ok":
            for field in ("since", "until"):
                bound = data.get(field) or {}
                if bound.get("readable") is False:
                    refuse(not_a_version(bound["version"], field, ecosystem, name))
        text = render_changes(data, ecosystem, name, archive.server)
        return f"{notice}\n{text}" if notice else text

    @server.tool(
        name="package_versions",
        annotations=reads,
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
            data = held(await _held_or_requested(archive, data, ecosystem, name))
        except httpx.HTTPError as exc:
            refuse(archive.failed(exc))
        return render_versions(data, ecosystem, name, archive.server)

    @server.tool(
        name="search_packages",
        annotations=reads,
        description=(
            "Find packages in the archive by name. Returns the matches across "
            "every registry with a one-line summary each."
        ),
    )
    async def search_packages(query: str) -> str:
        try:
            data = await archive.public("/api/public/search", q=query)
        except httpx.HTTPError as exc:
            refuse(archive.failed(exc))
        return render_search(data, query, archive.server)

    @server.tool(
        name="list_projects",
        annotations=reads,
        description=(
            "The user's projects on PackageTrack (pushed with import_project or "
            "`packagetrackdev push`): "
            "how many dependencies each has, how many the archive could compare, "
            "and how many are behind or withdrawn. Needs a PackageTrack API key."
        ),
    )
    async def list_projects() -> str:
        try:
            data = await archive.private("/api/v1/projects")
        except NoKey:
            refuse(archive.no_key())
        except httpx.HTTPError as exc:
            refuse(archive.failed(exc))
        return render_projects(data, archive.server)

    @server.tool(
        name="project_report",
        annotations=reads,
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
            refuse(archive.no_key())
        except httpx.HTTPError as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 404
            ):
                refuse(f"No project named {name!r}. list_projects shows the names.")
            refuse(archive.failed(exc))
        summary = None
        try:
            listing = await archive.private("/api/v1/projects")
            summary = next(
                (
                    row
                    for row in listing.get("projects") or []
                    if row.get("name") == name
                ),
                None,
            )
        except (NoKey, httpx.HTTPError):
            summary = None
        return render_report(data, archive.server, summary)

    @server.tool(
        name="import_project",
        annotations=pushes,
        description=(
            "Push a project's dependency set to PackageTrack, creating the "
            "project if that name is new. Prefer file_path: the absolute path "
            "of the "
            "user's lock file (uv.lock, poetry.lock, package-lock.json, "
            "Cargo.lock, composer.lock, go.mod) or, without one, "
            "requirements.txt or package.json. It is parsed on this machine "
            "and the ecosystem comes from the file, so you do not interpret "
            "the format. Otherwise send dependencies (names and versions you "
            "read yourself) with purl_type. Exactly one of the two. "
            "An empty set is refused and nothing is pushed. "
            "Pushing the set a project already holds changes nothing. Pushing "
            "another set over a name that exists updates that project's "
            "dependency list, so use the name the user means; list_projects "
            "gives the names that exist. A new name is a new project, even "
            "when another project holds the same set. A name holds one "
            "ecosystem: another "
            "ecosystem's set over it is refused; push that under another "
            "name. When the organization already has as many projects as it "
            "may, a new name is refused and nothing is stored. "
            "The push is stored; organization rules come back as warn/fail "
            "and are not a rejection. The answer is short and says what this "
            "push did; call project_report after it for how the project "
            "stands. Needs a PackageTrack API key. Only "
            "names and versions are sent, never file paths, file contents or "
            "code."
        ),
    )
    async def import_project(
        name: str,
        purl_type: Ecosystem | None = None,
        dependencies: list[Dependency] | None = None,
        file_path: str | None = None,
        source_file: str | None = None,
    ) -> str:
        if (dependencies is None) == (file_path is None):
            refuse(
                "Send either file_path (the lock file to read) or "
                "dependencies (with purl_type), not both and not neither. "
                "Nothing was pushed."
            )
        read = None
        if file_path is not None:
            read = read_lock_file(file_path)
            if isinstance(read, str):
                refuse(read)
            if purl_type is not None and purl_type != read.purl_type:
                refuse(
                    f"{read.source_file} holds {read.purl_type} dependencies, "
                    f"not {purl_type}. Nothing was pushed."
                )
            purl_type = read.purl_type
            dependencies = read.dependencies
            source_file = read.source_file
        elif purl_type is None:
            refuse(
                "purl_type is required with dependencies: a list of names says "
                "nothing about which registry they are from. Nothing was pushed."
            )
        if not dependencies:
            refuse(messages.EMPTY_DEPENDENCIES)
        if len(dependencies) > MAX_DEPENDENCIES:
            refuse(
                messages.DEPENDENCY_LIMIT.format(
                    count=len(dependencies), limit=MAX_DEPENDENCIES
                )
                + " `packagetrackdev push` handles a project this size."
            )
        problem = name_problem(name)
        if problem:
            refuse(f"{problem} Nothing was pushed.")
        entries = [
            {
                "name": entry["name"],
                "version": entry["version"],
                "parents": list(entry.get("parents") or [])[:20],
                "indirect": bool(entry.get("indirect", False)),
            }
            for entry in dependencies
        ]
        payload = {
            "name": name,
            "purl_type": purl_type,
            "source_file": source_file,
            "dependencies": entries,
        }
        try:
            data = await archive.private_post("/api/v1/mcp/projects/import", payload)
        except NoKey:
            refuse(archive.no_key())
        except httpx.HTTPError as exc:
            refuse(push_refusal(exc) or archive.push_failed(exc))
        return render_import(
            data, len(dependencies), archive.server, read, sent=entries
        )

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
