"""Word the outcomes of import_project."""

from __future__ import annotations

from typing import Any

ECOSYSTEM_CONFLICT = "ecosystem_conflict"
PROJECT_LIMIT = "project_limit"
CREDENTIAL_NAME = "credential_name"

RESULT_PREVIEW_LIMIT = 4
PREVIEW_VALUE_CHARS = 80

NO_CHANGES = "No changes were made."

PROJECT_CREATED = (
    "Project created successfully. '{project}' was added with {dependencies}."
)
PROJECT_UPDATED = (
    "Project updated successfully. '{project}' now reflects the latest "
    "dependency set."
)
PROJECT_UNCHANGED = (
    "No dependency changes detected. '{project}' already contains this "
    "dependency set. No new history entry was created."
)
IMPORTED_HEADING = "Imported:"
RESULT_CHANGES_HEADING = "Changes:"
PROJECT_INFO_CHANGED = "Project information changed."
NO_PACKAGE_CHANGES = "No package version changes were detected."
ECOSYSTEM_MISMATCH = (
    "The project uses a different ecosystem. '{project}' currently contains "
    "{existing} dependencies and cannot be replaced with {incoming} "
    "dependencies. Use a different project name{suggestion}."
)
DEPENDENCY_LIMIT = (
    "The project exceeds the supported dependency limit. This import contains "
    "{count:,} dependencies; the current limit is {limit:,}. No project data "
    "was changed."
)
EMPTY_DEPENDENCIES = (
    "The dependency list is empty, so there is nothing to push. No project "
    "data was changed."
)
EMPTY_FILE = (
    "{file} lists no dependencies, so there is nothing to push. No project "
    "data was changed."
)
PROJECT_LIMIT_REACHED = (
    "The organization has reached its limit of {limit} projects. No new "
    "project was created. Remove a project it no longer needs to add this one."
)
PROJECT_LIMIT_UNNUMBERED = (
    "The organization has reached its project limit. No new project was "
    "created. Remove a project it no longer needs to add this one."
)
CREDENTIAL_NAMES = (
    "{count} {names} like an access token or API key. Leave {it} out of the "
    "dependency list, revoke {it} if real, and push again. Nothing was pushed."
)
OPERATION_FAILED = (
    "The operation could not be completed. No project data was changed."
)
OPERATION_REFUSED = "The operation could not be completed."
NOTHING_STORED = ("Nothing was pushed", "No project data was changed")
OPERATION_UNCONFIRMED = (
    "The operation could not be completed, and PackageTrack did not confirm "
    "whether the project changed. Call project_report to check its current "
    "state before trying again."
)


def dependencies(count: int) -> str:
    """Count dependencies in words: `1 dependency`, `2 dependencies`."""
    return f"{count:,} dependenc{'y' if count == 1 else 'ies'}"


def next_step(text: str) -> str:
    """Mark a line as the agent's instruction rather than the person's text."""
    return f"Next step for the agent: {text}"


def details(reason: str | None) -> str:
    """Append the server's own sentence after ours, where no code named the case."""
    return f"\nDetails: {reason}" if reason else ""


def failed(reason: str | None) -> str:
    """Return a refused push's answer, saying once that nothing changed."""
    says_it = reason is not None and any(s in reason for s in NOTHING_STORED)
    return (OPERATION_REFUSED if says_it else OPERATION_FAILED) + details(reason)


def _clean(value: Any) -> str:
    """Return one short printable line of somebody else's text."""
    text = "".join(c if c.isprintable() else " " for c in str(value or ""))
    text = " ".join(text.split())
    if len(text) > PREVIEW_VALUE_CHARS:
        text = text[: PREVIEW_VALUE_CHARS - 3] + "..."
    return text


def _balanced(changes: Any, limit: int) -> list[tuple[str, dict[str, Any]]]:
    """Return at most `limit` named changes as (kind, change), kinds in turn."""
    if not isinstance(changes, dict):
        return []
    queues = []
    for kind in ("added", "removed", "upgraded", "downgraded"):
        entries = changes.get(kind)
        if isinstance(entries, list):
            queues.append((kind, [e for e in entries if isinstance(e, dict)]))
    picked: list[tuple[str, dict[str, Any]]] = []
    depth = 0
    while len(picked) < limit and any(depth < len(q) for _, q in queues):
        for kind, entries in queues:
            if len(picked) >= limit:
                break
            if depth < len(entries) and _clean(entries[depth].get("name")):
                picked.append((kind, entries[depth]))
        depth += 1
    return picked


def _counts(conflict: dict[str, Any]) -> dict[str, int] | None:
    """Return the server's four counts, or None when it sent none."""
    diff = conflict.get("diff")
    if not isinstance(diff, dict):
        return None
    return {
        kind: int(diff.get(kind) or 0)
        for kind in ("added", "removed", "upgraded", "downgraded")
    }


def no_package_moved(conflict: dict[str, Any]) -> bool:
    """Whether the server says the sets differ although no package moved."""
    counts = _counts(conflict)
    return counts is not None and not any(counts.values())


def _count(value: Any) -> int | None:
    """Return a count the server sent, or None for anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def stored_count(data: dict[str, Any], count: int) -> int:
    """Return how many dependencies the project holds, the server's word first."""
    stored = _count(data.get("stored"))
    return count if stored is None else stored


def imported_digest(data: dict[str, Any], count: int) -> str:
    """Summarise what a new project was created with, counts only."""
    total = stored_count(data, count)
    line = dependencies(total)
    stored, direct = _count(data.get("stored")), _count(data.get("direct"))
    if stored is not None and direct is not None and direct <= stored:
        line += f" · {direct:,} direct · {stored - direct:,} indirect"
    return f"{IMPORTED_HEADING}\n{line}"


MORE_POINTER = "Use project_report or see the dashboard."


def _more(remaining: int, one: str, many: str) -> str:
    """Say how many were not named, and where the rest is."""
    return f"And {remaining:,} more {one if remaining == 1 else many}. {MORE_POINTER}"


def imported_examples(
    sent: list[dict[str, Any]] | None,
    total: int,
    limit: int = RESULT_PREVIEW_LIMIT,
) -> str | None:
    """Name a few of the dependencies a new project was created with."""
    if not sent:
        return None
    seen: set[tuple[str, str]] = set()
    picked: list[tuple[bool, str, str]] = []
    for entry in sent:
        name, version = _clean(entry.get("name")), _clean(entry.get("version"))
        if not name or (name.lower(), version) in seen:
            continue
        seen.add((name.lower(), version))
        indirect = bool(entry.get("parents")) or bool(entry.get("indirect"))
        picked.append((indirect, name.lower(), f"- {name} {version}".rstrip()))
    shown = [line for *_, line in sorted(picked)[:limit]]
    if not shown:
        return None
    text = "Examples:\n" + "\n".join(shown)
    remaining = total - len(shown)
    if remaining > 0:
        text += "\n" + _more(remaining, "dependency", "dependencies")
    return text


def _result_line(kind: str, change: dict[str, Any]) -> str:
    """Return one change as a line of a stored push's answer."""
    name = _clean(change.get("name"))
    old, new = _clean(change.get("from")), _clean(change.get("to"))
    if kind == "added":
        return f"- {name} {new}".rstrip() + " added"
    if kind == "removed":
        return f"- {name} {old}".rstrip() + " removed"
    line = f"- {name} {old or '?'} → {new or '?'}"
    return f"{line} (downgraded)" if kind == "downgraded" else line


def update_digest(
    data: dict[str, Any] | None, limit: int = RESULT_PREVIEW_LIMIT
) -> str | None:
    """Summarise an applied update from the difference the server measured."""
    if not data:
        return None
    if no_package_moved(data):
        return f"{PROJECT_INFO_CHANGED}\n\n{NO_PACKAGE_CHANGES}"
    counts = _counts(data)
    picked = _balanced(data.get("changes"), limit)
    total = sum(counts.values()) if counts else len(picked)
    if not picked:
        return None
    text = RESULT_CHANGES_HEADING + "\n" + "\n".join(
        _result_line(kind, change) for kind, change in picked
    )
    remaining = total - len(picked)
    if remaining > 0:
        text += "\n" + _more(remaining, "change", "changes")
    return text


def outcome(data: dict[str, Any], count: int) -> str:
    """Return the sentence for a stored push, from the answer's flags alone."""
    project = data.get("project_name") or ""
    if data.get("created"):
        return PROJECT_CREATED.format(
            project=project, dependencies=dependencies(stored_count(data, count))
        )
    if data.get("unchanged"):
        return PROJECT_UNCHANGED.format(project=project)
    return PROJECT_UPDATED.format(project=project)


def refusal(body: Any) -> str | None:
    """Return the sentence for a refusal the server named with a code, or None."""
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if code == ECOSYSTEM_CONFLICT:
        project = body.get("project")
        existing = body.get("existing_ecosystem")
        incoming = body.get("incoming_ecosystem")
        if not (project and existing and incoming):
            return None
        suggested = body.get("suggested_name")
        sentence = ECOSYSTEM_MISMATCH.format(
            project=project,
            existing=existing,
            incoming=incoming,
            suggestion=f", such as '{suggested}'" if suggested else "",
        )
        return f"{sentence} {NO_CHANGES}"
    if code == PROJECT_LIMIT:
        limit = _count(body.get("limit"))
        if not limit:
            return PROJECT_LIMIT_UNNUMBERED
        return PROJECT_LIMIT_REACHED.format(limit=f"{limit:,}")
    if code == CREDENTIAL_NAME:
        count = _count(body.get("count"))
        if not count:
            return None
        one = count == 1
        return CREDENTIAL_NAMES.format(
            count=f"{count:,}",
            names="dependency name looks" if one else "dependency names look",
            it="it" if one else "them",
        )
    return None


REPORT_POINTER = 'Call project_report("{project}") for the full report.'

LOOKING_UP = "{count} being looked up; call project_report again when that finishes."
NOT_COMPARED = "{count} could not be compared; project_report says why."
RATE_LIMITED = (
    "Rate limit: PackageTrack's lookup queue is full for this hour, so "
    "{count} the archive does not hold {verb} not looked up. Push again later."
)


def report_pointer(project: str) -> str:
    """Point at project_report for everything else."""
    return REPORT_POINTER.format(project=project)


def lookup_lines(data: dict[str, Any]) -> list[str]:
    """Say what this push left unanswered, from the server's counts alone."""
    lines = []
    refused = _count(data.get("lookups_refused")) or 0
    if refused:
        lines.append(
            RATE_LIMITED.format(
                count=dependencies(refused), verb="was" if refused == 1 else "were"
            )
        )
    looking = _count(data.get("being_looked_up")) or 0
    missing = _count(data.get("not_compared")) or 0
    if looking:
        lines.append(LOOKING_UP.format(count=dependencies(looking)))
    if missing > looking:
        lines.append(NOT_COMPARED.format(count=dependencies(missing - looking)))
    return lines
