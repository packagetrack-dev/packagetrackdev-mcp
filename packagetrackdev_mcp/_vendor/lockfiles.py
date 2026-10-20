"""Lock file and manifest parsers."""

from __future__ import annotations

import codecs
import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import sbom

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Dependency:
    name: str
    version: str
    parents: list[str] = field(default_factory=list)
    indirect: bool = False

    @property
    def direct(self) -> bool:
        return not self.parents and not self.indirect


@dataclass(slots=True)
class Project:
    ecosystem: str
    source: str
    dependencies: list[Dependency]
    skipped: int = 0
    note: str | None = None

    @property
    def direct(self) -> list[Dependency]:
        return [d for d in self.dependencies if d.direct]


LOCKFILES = (
    ("npm", "package-lock.json"),
    ("npm", "npm-shrinkwrap.json"),
    ("pypi", "uv.lock"),
    ("pypi", "poetry.lock"),
    ("cargo", "Cargo.lock"),
    ("composer", "composer.lock"),
    ("go", "go.mod"),
)

MANIFESTS = (
    ("npm", "package.json"),
    ("pypi", "requirements.txt"),
)


def ecosystems() -> tuple[str, ...]:
    """Every ecosystem a lock file or manifest here can be read for."""
    return tuple(sorted({eco for eco, _ in LOCKFILES + MANIFESTS}))


def find(project: Path) -> list[tuple[str, Path]]:
    """Find the dependency file for each ecosystem, preferring lock files."""
    chosen: dict[str, Path] = {}
    for ecosystem, name in (*LOCKFILES, *MANIFESTS):
        path = project / name
        if ecosystem not in chosen and path.is_file():
            chosen[ecosystem] = path
    return [(ecosystem, path) for ecosystem, path in chosen.items()]


DECLARATIONS = frozenset(name for _, name in MANIFESTS)

SUPPORTED_FILES = tuple(name for _, name in (*LOCKFILES, *MANIFESTS))

_OPERATOR = r"(===|==|!=|<=?|>=?|~=)"
_CLAUSE = _OPERATOR + r"\s*[^\s;,=][^\s;,]*"
_REQUIREMENT_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*"
    r"(" + _CLAUSE + r"(\s*,\s*" + _CLAUSE + r")*)?"
    r"\s*(;.*)?$"
)


def detect(content: str, filename: str | None = None) -> tuple[str, str] | None:
    """Detect the file type of `content`, as (ecosystem, canonical name)."""
    content = content.removeprefix(_BOM)
    if filename:
        base = filename.replace("\\", "/").rsplit("/", 1)[-1]
        for ecosystem, name in (*LOCKFILES, *MANIFESTS):
            if base.lower() == name.lower():
                return ecosystem, name

    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if data is not None:
        if not isinstance(data, dict):
            return None
        which = sbom.looks_like(data)
        if which is not None:
            reading = sbom.read(content)
            groups = reading.by_ecosystem() if reading else {}
            if len(groups) != 1:
                return None
            return next(iter(groups)), which
        if isinstance(data.get("packages"), dict) or "lockfileVersion" in data:
            return "npm", "package-lock.json"
        if isinstance(data.get("packages"), list) or isinstance(
            data.get("packages-dev"), list
        ):
            return "composer", "composer.lock"
        if isinstance(data.get("dependencies"), dict) or isinstance(
            data.get("devDependencies"), dict
        ):
            return "npm", "package.json"
        return None

    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("package"), list):
        if "requires-python" in data:
            return "pypi", "uv.lock"
        metadata = data.get("metadata")
        if isinstance(metadata, dict) and "content-hash" in metadata:
            return "pypi", "poetry.lock"
        entries = [e for e in data["package"] if isinstance(e, dict)]
        if any(isinstance(e.get("source"), dict) for e in entries):
            return "pypi", "uv.lock"
        if any(isinstance(e.get("source"), str) or "checksum" in e for e in entries):
            return "cargo", "Cargo.lock"
        return None

    if _looks_like_gomod(content):
        return "go", "go.mod"

    lines = [
        stripped
        for raw in content.splitlines()
        if (stripped := _requirement_text(raw))
    ]
    candidates = [line for line in lines if not line.startswith("-")]
    if candidates and all(_REQUIREMENT_LINE.match(line) for line in candidates):
        return "pypi", "requirements.txt"
    return None


FORMATS_BY_SUFFIX = {
    ".json": frozenset(
        {"package-lock.json", "package.json", "composer.lock", *sbom.FORMATS}
    ),
    ".lock": frozenset({"uv.lock", "poetry.lock", "Cargo.lock", "composer.lock"}),
    ".mod": frozenset({"go.mod"}),
    ".txt": frozenset({"requirements.txt"}),
}
REQUIREMENTS_WORDS = ("requirement", "constraint")

NOT_A_DEPENDENCY_NAME = "not_a_dependency_name"
NOT_THIS_SUFFIX = "not_this_suffix"
NOT_NAMED_REQUIREMENTS = "not_named_requirements"
UNPINNED_UNNAMED = "unpinned_unnamed"

_SPECIFIER = re.compile(_OPERATOR)


def may_open(filename: str) -> bool:
    """Whether a file of this name may be opened to look for dependencies."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
    known = {name.lower() for name in SUPPORTED_FILES}
    return base in known or base.endswith(tuple(FORMATS_BY_SUFFIX))


def refusal(
    detected: str, filename: str | None, content: str, folder: str = ""
) -> str | None:
    """Why `detected` may not be read from this file, or None when it may."""
    if filename is None:
        if detected == "requirements.txt" and not any(
            _SPECIFIER.search(line.split("#", 1)[0])
            for line in content.splitlines()
            if not line.lstrip().startswith("-")
        ):
            return UNPINNED_UNNAMED
        return None
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = base.lower()
    if lowered in {name.lower() for name in SUPPORTED_FILES}:
        return None
    if not may_open(base):
        return NOT_A_DEPENDENCY_NAME
    suffix = next(s for s in FORMATS_BY_SUFFIX if lowered.endswith(s))
    if detected not in FORMATS_BY_SUFFIX[suffix]:
        return NOT_THIS_SUFFIX
    if detected == "requirements.txt":
        named = f"{folder}/{lowered}".lower()
        if not any(word in named for word in REQUIREMENTS_WORDS):
            return NOT_NAMED_REQUIREMENTS
    return None


_BOM = chr(0xFEFF)  # a byte order mark once decoded


def decode(raw: bytes) -> str | None:
    """Decode a dependency file's bytes, or None when they are not text."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return None


def read(ecosystem: str, path: Path) -> Project | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    content = decode(raw)
    if content is None:
        log.warning("could not read %s: not UTF-8 or marked UTF-16 text", path)
        return None
    return parse(ecosystem, path.name, content)


def _read_sbom(ecosystem: str, filename: str, content: str) -> Project | None:
    """Read one ecosystem's components from an SBOM."""
    reading = sbom.read(content)
    if reading is None:
        return None
    mine = reading.by_ecosystem().get(ecosystem, [])
    dependencies = [
        Dependency(
            name=component.name,
            version=component.version,
            parents=list(component.parents),
            indirect=not component.direct and not component.parents,
        )
        for component in mine
    ]
    return Project(
        ecosystem=ecosystem,
        source=filename,
        dependencies=dependencies,
        skipped=reading.skipped,
        note=sbom.caveat(reading),
    )


_SEP = "[-_]"
_CREDENTIAL = re.compile(
    r"^(?=[^\d]*\d)(?:"
    rf"gh[pousr]{_SEP}[a-z0-9]{{36,}}"
    rf"|github{_SEP}pat{_SEP}[a-z0-9_-]{{60,}}"
    rf"|glpat{_SEP}[a-z0-9_-]{{20,}}"
    rf"|sk{_SEP}(?:live|test|proj|ant){_SEP}[a-z0-9_-]{{8,}}"
    rf"|[sr]k{_SEP}(?:live|test){_SEP}[a-z0-9]{{16,}}"
    rf"|xox[abposr]{_SEP}[a-z0-9-]{{10,}}"
    r"|akia[0-9a-z]{16}"
    r"|aiza[0-9a-z_-]{35}"
    rf"|npm{_SEP}[a-z0-9]{{36}}"
    rf"|pypi{_SEP}age[a-z0-9_-]{{50,}}"
    rf"|pkgt{_SEP}[a-z0-9_-]{{16,}}"
    r")$",
    re.IGNORECASE,
)

CREDENTIALS_LEFT_OUT = (
    "{count} {entries} looked like an access token or API key and {were} not "
    "sent. Remove {it} from the file, and revoke {it} if {it_is} real."
)


def looks_like_credential(name: str) -> bool:
    """Whether a dependency name is shaped like an access token or API key."""
    return bool(_CREDENTIAL.match(name.strip()))


def _without_credentials(project: Project) -> Project:
    """Leave out of `project` every entry shaped like a credential, and say so."""
    kept = [d for d in project.dependencies if not looks_like_credential(d.name)]
    left_out = len(project.dependencies) - len(kept)
    if not left_out:
        return project
    one = left_out == 1
    said = CREDENTIALS_LEFT_OUT.format(
        count=left_out,
        entries="entry" if one else "entries",
        were="was" if one else "were",
        it="it" if one else "them",
        it_is="it is" if one else "they are",
    )
    project.dependencies = kept
    project.skipped += left_out
    project.note = f"{project.note} {said}" if project.note else said
    return project


def parse(ecosystem: str, filename: str, content: str) -> Project | None:
    """Parse lock or manifest content by canonical file name."""
    project = _parse(ecosystem, filename, content)
    return _without_credentials(project) if project is not None else None


def _parse(ecosystem: str, filename: str, content: str) -> Project | None:
    """Parse by canonical file name, credentials and all."""
    content = content.removeprefix(_BOM)
    try:
        if filename in sbom.FORMATS:
            return _read_sbom(ecosystem, filename, content)
        if filename == "go.mod":
            return _read_gomod(ecosystem, filename, content)
        if filename == "package.json":
            return _read_package_json(ecosystem, filename, content)
        if filename == "requirements.txt":
            return _read_requirements(ecosystem, filename, content)
        if filename == "composer.lock":
            return _read_composer(ecosystem, filename, content)
        if filename == "Cargo.lock":
            return _read_cargo(ecosystem, filename, content)
        if filename.endswith(".json"):
            return _read_npm(ecosystem, filename, content)
        return _read_toml(ecosystem, filename, content)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse %s: %s", filename, exc)
        return None
    except (AttributeError, TypeError, KeyError) as exc:
        log.warning("could not parse %s: unexpected shape: %r", filename, exc)
        return None


def _mapping(value: object) -> dict:
    """Return `value` when it is a JSON object, an empty one otherwise."""
    return value if isinstance(value, dict) else {}


_NPM_DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def _read_npm(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `package-lock.json` (v2 and v3)."""
    data = json.loads(content)
    if not isinstance(data, dict):
        return None
    entries = data.get("packages")
    if not isinstance(entries, dict):
        return None

    real_name: dict[str, str] = {}
    for location, meta in entries.items():
        if not location or "node_modules/" not in location:
            continue
        if not isinstance(meta, dict):
            continue
        folder = location.split("node_modules/")[-1]
        published = meta.get("name")
        if isinstance(published, str) and published and published != folder:
            real_name[folder] = published

    def resolve(spelling: str) -> str:
        return real_name.get(spelling, spelling)

    def lands_on(origin: str, spelling: str) -> str | None:
        folder = origin
        while True:
            prefix = f"{folder}/" if folder else ""
            candidate = f"{prefix}node_modules/{spelling}"
            if candidate in entries:
                return candidate
            if not folder:
                return None
            folder = folder.rsplit("/", 1)[0] if "/" in folder else ""

    direct: set[str] = set()
    for location, meta in entries.items():
        if location and "node_modules/" in location:
            continue
        meta = _mapping(meta)
        for kind in _NPM_DEPENDENCY_FIELDS:
            for spelling in _mapping(meta.get(kind)):
                target = lands_on(location, spelling)
                if target is not None:
                    direct.add(target)

    rows: dict[str, tuple[str, str]] = {}
    row_of: dict[str, str] = {}
    skipped = 0

    for location, meta in entries.items():
        if not location or "node_modules/" not in location:
            continue
        version = meta.get("version") if isinstance(meta, dict) else None
        if not isinstance(version, str):
            skipped += 1
            continue
        name = resolve(location.split("node_modules/")[-1])
        key = f"{name}@{version}"
        rows.setdefault(key, (name, version))
        row_of[location] = key

    parents: dict[str, set[str]] = {}
    for location, key in row_of.items():
        meta = entries[location]
        for kind in ("dependencies", "peerDependencies", "optionalDependencies"):
            for spelling in _mapping(meta.get(kind)):
                target = lands_on(location, spelling)
                if target in row_of:
                    parents.setdefault(row_of[target], set()).add(rows[key][0])

    is_direct = {row_of[location] for location in direct if location in row_of}
    out: list[Dependency] = []
    for key, (name, version) in rows.items():
        owners = [] if key in is_direct else sorted(parents.get(key, ()))
        out.append(Dependency(name=name, version=version, parents=owners))

    return Project(
        ecosystem=ecosystem, source=source, dependencies=out, skipped=skipped
    )


def _read_cargo(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `Cargo.lock`."""
    data = tomllib.loads(content)
    packages = data.get("package")
    if not isinstance(packages, list):
        return None

    entries: list[tuple[str, str | None, bool, list]] = []
    by_name: dict[str, list[str]] = {}
    skipped = 0

    for entry in packages:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            skipped += 1
            continue
        name, version = entry["name"], entry.get("version")
        version = version if isinstance(version, str) else None
        is_local = not entry.get("source")
        deps = entry.get("dependencies")
        deps = deps if isinstance(deps, list) else []
        entries.append((name, version, is_local, deps))
        if not is_local and version is not None:
            by_name.setdefault(name, []).append(version)

    def targets(reference: object) -> list[tuple[str, str]]:
        if not isinstance(reference, str) or not reference:
            return []
        name, _, rest = reference.partition(" ")
        known = by_name.get(name, [])
        wanted = rest.split(" ", 1)[0]
        if wanted:
            return [(name, wanted)] if wanted in known else []
        return [(name, version) for version in known]

    direct: set[tuple[str, str]] = set()
    parents: dict[tuple[str, str], set[str]] = {}
    for name, _version, is_local, deps in entries:
        for reference in deps:
            for target in targets(reference):
                if is_local:
                    direct.add(target)
                else:
                    parents.setdefault(target, set()).add(name)

    rows = sorted(
        {
            (name, version)
            for name, version, is_local, _ in entries
            if not is_local and version is not None
        }
    )
    return Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            Dependency(
                name=name,
                version=version,
                parents=[]
                if (name, version) in direct
                else sorted(parents.get((name, version), ())),
            )
            for name, version in rows
        ],
        skipped=skipped,
    )


def _read_composer(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `composer.lock`."""
    data = json.loads(content)
    if not isinstance(data, dict):
        return None
    blocks = data.get("packages"), data.get("packages-dev")
    if not any(isinstance(b, list) for b in blocks):
        return None

    versions: dict[str, str] = {}
    requires: dict[str, set[str]] = {}
    for block in blocks:
        if not isinstance(block, list):
            continue
        for entry in block:
            if not isinstance(entry, dict):
                continue
            name, version = entry.get("name"), entry.get("version")
            if not _is_composer_package(name) or not isinstance(version, str):
                continue
            versions[name] = version
            for dep in _mapping(entry.get("require")):
                if _is_composer_package(dep):
                    requires.setdefault(dep, set()).add(name)

    return Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            Dependency(
                name=name,
                version=version,
                parents=sorted(requires.get(name, ())),
            )
            for name, version in sorted(versions.items())
        ],
    )


def _is_composer_package(name: object) -> bool:
    """Return True for a `vendor/package` name, False for platform requirements."""
    return isinstance(name, str) and "/" in name and not name.startswith("ext-")


def _read_toml(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `uv.lock` or `poetry.lock`."""
    data = tomllib.loads(content)
    packages = data.get("package")
    if not isinstance(packages, list):
        return None

    roots: list[set[tuple[str, str | None]]] = []
    entries: list[tuple[str, str, set[tuple[str, str | None]]]] = []
    by_name: dict[str, list[str]] = {}
    skipped = 0

    for entry in packages:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            skipped += 1
            continue
        name, version = entry["name"], entry.get("version")

        origin = entry.get("source") or {}
        is_root = isinstance(origin, dict) and (
            "virtual" in origin or "editable" in origin
        )
        needs = _toml_dependencies(entry)

        if is_root:
            roots.append(needs)
            continue

        if isinstance(version, str):
            entries.append((name, version, needs))
            if version not in by_name.setdefault(name, []):
                by_name[name].append(version)

    def targets(reference: tuple[str, str | None]) -> list[tuple[str, str]]:
        name, wanted = reference
        known = by_name.get(name, [])
        if wanted is not None:
            return [(name, wanted)] if wanted in known else []
        return [(name, version) for version in known]

    declared = {target for needs in roots for ref in needs for target in targets(ref)}
    parents: dict[tuple[str, str], set[str]] = {}
    for name, _version, needs in entries:
        for reference in needs:
            for target in targets(reference):
                parents.setdefault(target, set()).add(name)

    rows = sorted({(name, version) for name, version, _ in entries})
    return Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            Dependency(
                name=name,
                version=version,
                parents=[]
                if (name, version) in declared
                else sorted(parents.get((name, version), ())),
            )
            for name, version in rows
        ],
        skipped=skipped,
    )


def _toml_dependencies(entry: dict) -> set[tuple[str, str | None]]:
    """Return the (name, version or None) references of one lock entry."""
    found: set[tuple[str, str | None]] = set()

    def add(item: object) -> None:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            version = item.get("version")
            found.add((item["name"], version if isinstance(version, str) else None))

    for item in entry.get("dependencies") or []:
        if isinstance(item, str):
            found.add((item, None))
        else:
            add(item)

    metadata = entry.get("metadata") or {}
    for item in (
        (metadata.get("requires-dist") or []) if isinstance(metadata, dict) else []
    ):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            found.add((item["name"], None))

    poetry = entry.get("dependencies")
    if isinstance(poetry, dict):
        found |= {(k, None) for k in poetry if isinstance(k, str)}

    return found


_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?\s*"
    r"(?P<op>===|==)\s*(?P<version>[^\s;#,=*]+)\s*(;.*)?$"
)


def _requirement_text(raw: str) -> str:
    """Return a requirements line without its comment or trailing backslash."""
    return raw.split("#", 1)[0].strip().removesuffix("\\").rstrip()
_REQUIREMENT_NAME = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")
_DIRECT_REFERENCE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*@\s*\S+\s*(;.*)?$"
)


def _read_requirements(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `requirements.txt`."""
    dependencies: list[Dependency] = []
    skipped = 0
    for raw in content.splitlines():
        line = _requirement_text(raw)
        if not line:
            continue
        if line.startswith("-"):
            skipped += 1
            continue

        pinned = _REQUIREMENT.match(line)
        if pinned:
            dependencies.append(
                Dependency(name=pinned["name"], version=pinned["version"])
            )
            continue

        direct = _DIRECT_REFERENCE.match(line)
        if direct:
            dependencies.append(Dependency(name=direct["name"], version=""))
        elif _REQUIREMENT_LINE.match(line):
            loose = _REQUIREMENT_NAME.match(line)
            dependencies.append(Dependency(name=loose["name"], version=""))
        else:
            skipped += 1

    return Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=dependencies,
        skipped=skipped,
    )


_NPM_EXACT = re.compile(
    r"^=?v?(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+[0-9A-Za-z.-]+)?$"
)


def _npm_pinned(spec: object) -> str:
    """Return the version a `package.json` spec pins, or "" when it pins nothing."""
    if not isinstance(spec, str):
        return ""
    match = _NPM_EXACT.match(spec.strip())
    return match["version"] if match else ""


def _read_package_json(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `package.json`."""
    data = json.loads(content)
    if not isinstance(data, dict):
        return None
    declared = {
        **_mapping(data.get("dependencies")),
        **_mapping(data.get("devDependencies")),
    }
    return Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            Dependency(name=name, version=_npm_pinned(declared[name]))
            for name in sorted(declared)
            if isinstance(name, str)
        ],
    )


_GO_DIRECTIVES = frozenset(
    {
        "module",
        "go",
        "toolchain",
        "godebug",
        "require",
        "exclude",
        "replace",
        "retract",
        "tool",
    }
)

_GO_VERSION = re.compile(r"^v\d")

_GO_MODULE_LINE = re.compile(r"^module\s+\S+$")
_GO_OTHER_DIRECTIVE = re.compile(r"^(go|toolchain|require|replace|exclude)\b")

MAX_NAME_CHARS = 255
MAX_VERSION_CHARS = 256


def _looks_like_gomod(content: str) -> bool:
    """Return True if `content` looks like a `go.mod` file."""
    lines = [
        stripped
        for raw in content.splitlines()
        if (stripped := raw.split("//", 1)[0].strip())
    ]
    return any(_GO_MODULE_LINE.match(line) for line in lines) and any(
        _GO_OTHER_DIRECTIVE.match(line) for line in lines
    )


def _go_tokens(line: str) -> list[str]:
    """Tokenise one `go.mod` line."""
    spaced = line.replace("(", " ( ").replace(")", " ) ")
    return [token.strip('"`') for token in spaced.split()]


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _read_gomod(ecosystem: str, source: str, content: str) -> Project | None:
    """Parse `go.mod`."""
    module_path: str | None = None
    go_directive: str | None = None
    requires: dict[str, tuple[str, bool]] = {}
    replaced: set[str] = set()
    skipped = 0
    block: str | None = None

    for raw in content.splitlines():
        body, _, comment = raw.partition("//")
        if not body.strip():
            continue

        if block is not None:
            tokens = _go_tokens(body)
            if tokens[0] == ")":
                block = None
                continue
            keyword, args = block, tokens
        else:
            tokens = _go_tokens(body)
            keyword, args = tokens[0], tokens[1:]
            if keyword not in _GO_DIRECTIVES:
                skipped += 1
                log.warning(
                    "go.mod: unknown directive, line skipped: %r", raw.strip()[:120]
                )
                continue
            if args and args[0] == "(":
                block = keyword
                if len(args) > 1:
                    skipped += 1
                    log.warning(
                        "go.mod: content on the line opening a %s block, not read: %r",
                        keyword,
                        raw.strip()[:120],
                    )
                continue

        if keyword == "module":
            if args:
                module_path = args[0]
        elif keyword == "go":
            go_directive = args[0] if args else None
        elif keyword in {"toolchain", "godebug", "tool", "exclude", "retract"}:
            continue
        elif keyword == "require":
            name, version = (args + ["", ""])[:2]
            if not name or not _GO_VERSION.match(version):
                skipped += 1
                log.warning(
                    "go.mod: require line without a module and version, skipped: %r",
                    raw.strip()[:120],
                )
                continue
            if len(name) > MAX_NAME_CHARS or len(version) > MAX_VERSION_CHARS:
                skipped += 1
                log.warning(
                    "go.mod: %s@%s exceeds the %s/%s character limit, skipped",
                    name[:80],
                    version[:40],
                    MAX_NAME_CHARS,
                    MAX_VERSION_CHARS,
                )
                continue
            requires[name] = (version, "indirect" in comment.split())
        elif keyword == "replace":
            if args:
                replaced.add(args[0])
                log.info(
                    "go.mod: %s is replaced, so it is not reported: %r",
                    args[0],
                    raw.strip()[:120],
                )

    if module_path:
        requires.pop(module_path, None)

    dropped = sorted(replaced & set(requires))
    for name in dropped:
        del requires[name]
    if dropped:
        skipped += len(dropped)
        log.warning(
            "go.mod: %s replaced %s not reported: %s",
            len(dropped),
            "module" if len(dropped) == 1 else "modules",
            ", ".join(dropped),
        )

    return Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            Dependency(name=name, version=version, indirect=indirect)
            for name, (version, indirect) in sorted(requires.items())
        ],
        skipped=skipped,
        note=_gomod_note(go_directive, dropped),
    )


def _gomod_note(go_directive: str | None, dropped: list[str]) -> str:
    """Return the caveat for a `go.mod` read, if any."""
    parts = [
        "go.mod marks which modules are indirect but never records which "
        "module required them, so indirect rows here name no parent."
    ]
    if _go_before_117(go_directive):
        parts.append(
            f"This file targets Go {go_directive}, which lists only some "
            "indirect modules, so the indirect set here may be incomplete."
        )
    if dropped:
        parts.append(
            f"{_plural(len(dropped), 'module is', 'modules are')} replaced by a "
            "replace directive and left out: the build uses other code under "
            "that name, so the published version is not what runs."
        )
    return " ".join(parts)


def _go_before_117(go_directive: str | None) -> bool:
    """Return True for Go versions before 1.17."""
    if not go_directive:
        return False
    parts = go_directive.split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0) < (1, 17)
    except ValueError:
        return False
