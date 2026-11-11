"""Readers for yarn, pnpm, bun, Pipfile, pub, NuGet, Gradle and Maven files."""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ElementTree

from . import lockfiles, yamlish

_RELEASE = re.compile(r"^v?\d[0-9A-Za-z.+_-]*$")


def _split_descriptor(text: str) -> tuple[str, str]:
    """Split `name@rest` into (name, rest), scoped npm names included."""
    at = text.find("@", 1)
    if at <= 0:
        return text, ""
    return text[:at], text[at + 1 :]


def _npm_real(name: str, spec: str) -> tuple[str, str]:
    """Undo an `npm:` alias: (published name, range) for `name` asking `spec`."""
    if spec.startswith("npm:"):
        inner = spec[4:]
        if inner.startswith("@") or "@" in inner:
            real, rest = _split_descriptor(inner)
            if real:
                return real, rest
    return name, spec


def _rows(
    entries: dict[tuple[str, str], set[str]], direct: set[tuple[str, str]]
) -> list[lockfiles.Dependency]:
    return [
        lockfiles.Dependency(
            name=name,
            version=version,
            parents=[]
            if (name, version) in direct
            else sorted(entries[(name, version)]),
        )
        for name, version in sorted(entries)
    ]


def read_yarn(
    ecosystem: str, source: str, content: str, declared: dict[str, str] | None
) -> lockfiles.Project | None:
    """Parse `yarn.lock`, classic (v1) or Berry (v2 and later)."""
    if re.search(r"^__metadata:\s*$", content, re.MULTILINE):
        return _read_yarn_berry(ecosystem, source, content)
    return _read_yarn_classic(ecosystem, source, content, declared)


_YARN_NOT_REGISTRY = re.compile(
    r"^(file:|link:|portal:|workspace:|git|github:|gitlab:|bitbucket:|https?:|"
    r"[\w.-]+/[\w.-]+)"
)


def _yarn_classic_entries(content: str) -> list[tuple[list[str], dict]]:
    """Return each entry of a classic yarn.lock as (descriptors, fields)."""
    entries: list[tuple[list[str], dict]] = []
    fields: dict | None = None
    block: dict | None = None
    for raw in content.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            if not line.endswith(":"):
                raise ValueError(f"yarn.lock: not an entry header: {line[:60]!r}")
            descriptors = [_unquote(part) for part in _split_commas(line[:-1])]
            fields = {}
            block = None
            entries.append((descriptors, fields))
            continue
        if fields is None:
            raise ValueError("yarn.lock: field before the first entry")
        if indent == 2:
            if line.endswith(":"):
                block = {}
                fields[_unquote(line[:-1])] = block
                continue
            key, value = _key_value(line)
            fields[key] = value
            block = None
        elif block is not None:
            key, value = _key_value(line)
            block[key] = value
        else:
            raise ValueError(f"yarn.lock: unexpected indentation: {line[:60]!r}")
    return entries


def _split_commas(text: str) -> list[str]:
    parts, current, quote = [], [], ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
        elif char == '"':
            quote = char
        elif char == ",":
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _key_value(line: str) -> tuple[str, str]:
    """Split a classic yarn.lock field line into key and value."""
    if line.startswith('"'):
        end = line.index('"', 1)
        return line[1:end], _unquote(line[end + 1 :])
    key, _, value = line.partition(" ")
    return key, _unquote(value)


def _read_yarn_classic(
    ecosystem: str, source: str, content: str, declared: dict[str, str] | None
) -> lockfiles.Project | None:
    entries = _yarn_classic_entries(content)
    by_descriptor: dict[str, tuple[str, str] | None] = {}
    rows: dict[tuple[str, str], set[str]] = {}
    edges: list[tuple[str, dict]] = []
    skipped = 0
    for descriptors, fields in entries:
        version = fields.get("version")
        name, spec = _split_descriptor(descriptors[0]) if descriptors else ("", "")
        real, real_spec = _npm_real(name, spec)
        registry = (
            isinstance(version, str)
            and _RELEASE.match(version)
            and not _YARN_NOT_REGISTRY.match(real_spec)
        )
        row = (real, version) if registry else None
        if row is None:
            skipped += 1
        else:
            rows.setdefault(row, set())
            edges.append((row, fields))
        for descriptor in descriptors:
            by_descriptor[descriptor] = row

    required: set[tuple[str, str]] = set()
    for row, fields in edges:
        for kind in ("dependencies", "optionalDependencies"):
            for dep, spec in lockfiles._mapping(fields.get(kind)).items():
                target = by_descriptor.get(f"{dep}@{spec}")
                if target is not None:
                    rows[target].add(row[0])
                    required.add(target)

    if declared is not None:
        direct = {
            target
            for name, spec in declared.items()
            if (target := by_descriptor.get(f"{name}@{spec}")) is not None
        }
    else:
        direct = set(rows) - required
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=_rows(rows, direct),
        skipped=skipped,
    )


def _read_yarn_berry(
    ecosystem: str, source: str, content: str
) -> lockfiles.Project | None:
    data = yamlish.loads(content)
    by_descriptor: dict[str, tuple[str, str] | None] = {}
    rows: dict[tuple[str, str], set[str]] = {}
    sources: list[tuple[tuple[str, str] | None, dict]] = []
    skipped = 0
    for key, fields in data.items():
        if key == "__metadata" or not isinstance(fields, dict):
            continue
        descriptors = [part.strip() for part in key.split(",") if part.strip()]
        resolution = fields.get("resolution")
        version = fields.get("version")
        if not isinstance(resolution, str):
            skipped += 1
            continue
        name, ref = _split_descriptor(resolution)
        if ref.startswith("workspace:"):
            row = None
            sources.append((None, fields))
        else:
            if ref.startswith("patch:"):
                inner = ref[len("patch:") :].split("#", 1)[0].replace("%3A", ":")
                name, ref = _split_descriptor(inner)
            if (
                ref.startswith("npm:")
                and isinstance(version, str)
                and _RELEASE.match(version)
            ):
                row = (name, version)
                rows.setdefault(row, set())
                sources.append((row, fields))
            else:
                row = None
                skipped += 1
        for descriptor in descriptors:
            by_descriptor[descriptor] = row

    def lands_on(dep: str, spec: str) -> tuple[str, str] | None:
        found = by_descriptor.get(f"{dep}@{spec}")
        if found is None and ":" not in spec:
            found = by_descriptor.get(f"{dep}@npm:{spec}")
        return found

    direct: set[tuple[str, str]] = set()
    for row, fields in sources:
        for kind in ("dependencies", "optionalDependencies"):
            for dep, spec in lockfiles._mapping(fields.get(kind)).items():
                if not isinstance(spec, str):
                    continue
                target = lands_on(dep, spec)
                if target is None:
                    continue
                if row is None:
                    direct.add(target)
                else:
                    rows[target].add(row[0])
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=_rows(rows, direct),
        skipped=skipped,
    )


def _strip_peers(text: str) -> str:
    """Drop pnpm's peer suffix: `4.3.4(supports-color@8.1.1)` is 4.3.4."""
    return text.split("(", 1)[0]


def read_pnpm(ecosystem: str, source: str, content: str) -> lockfiles.Project | None:
    """Parse `pnpm-lock.yaml` (lockfile 5.x, 6.x and 9.x)."""
    data = yamlish.loads(content)
    raw_version = str(data.get("lockfileVersion", "")).strip("'\"")
    try:
        major = int(raw_version.split(".", 1)[0])
    except ValueError:
        return None
    old_keys = major < 6

    def parse_key(key: str) -> tuple[str, str] | None:
        text = key[1:] if key.startswith("/") else key
        if old_keys and key.startswith("/"):
            path = text.split("_", 1)[0] if "_" in text.rsplit("/", 1)[-1] else text
            name, _, version = path.rpartition("/")
        else:
            name, _, version = _strip_peers(text).rpartition("@")
        if not name or not _RELEASE.match(version):
            return None
        return name, version

    def lands_on(dep: str, value: object) -> tuple[str, str] | None:
        if isinstance(value, dict):
            value = value.get("version")
        if not isinstance(value, str) or value.startswith(
            ("link:", "file:", "workspace:")
        ):
            return None
        value = _strip_peers(value)
        if value.startswith("/"):
            return parse_key(value)
        if old_keys:
            value = value.split("_", 1)[0]
        if "@" in value[1:]:
            return parse_key(value)
        return (dep, value) if _RELEASE.match(value) else None

    rows: dict[tuple[str, str], set[str]] = {}
    skipped = 0
    for section in ("packages", "snapshots"):
        for key in lockfiles._mapping(data.get(section)):
            row = parse_key(key)
            if row is None:
                if section == "packages":
                    skipped += 1
                continue
            rows.setdefault(row, set())

    edges_from = "snapshots" if isinstance(data.get("snapshots"), dict) else "packages"
    for key, fields in lockfiles._mapping(data.get(edges_from)).items():
        row = parse_key(key)
        if row is None:
            continue
        fields = lockfiles._mapping(fields)
        for kind in ("dependencies", "optionalDependencies"):
            for dep, value in lockfiles._mapping(fields.get(kind)).items():
                target = lands_on(dep, value)
                if target in rows:
                    rows[target].add(row[0])

    importers = lockfiles._mapping(data.get("importers")) or {".": data}
    direct: set[tuple[str, str]] = set()
    for importer in importers.values():
        importer = lockfiles._mapping(importer)
        for kind in ("dependencies", "devDependencies", "optionalDependencies"):
            for dep, value in lockfiles._mapping(importer.get(kind)).items():
                target = lands_on(dep, value)
                if target in rows:
                    direct.add(target)
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=_rows(rows, direct),
        skipped=skipped,
    )


def jsonc_loads(content: str) -> object:
    """Parse JSON that may carry comments and trailing commas."""
    out: list[str] = []
    i = 0
    n = len(content)
    while i < n:
        char = content[i]
        if char == '"':
            end = i + 1
            while end < n and content[end] != '"':
                end += 2 if content[end] == "\\" else 1
            out.append(content[i : end + 1])
            i = end + 1
        elif content.startswith("//", i):
            newline = content.find("\n", i)
            i = n if newline < 0 else newline
        elif content.startswith("/*", i):
            close = content.find("*/", i + 2)
            if close < 0:
                raise ValueError("unterminated comment")
            i = close + 2
        elif char == ",":
            ahead = i + 1
            while True:
                while ahead < n and content[ahead] in " \t\r\n":
                    ahead += 1
                if content.startswith("//", ahead):
                    newline = content.find("\n", ahead)
                    ahead = n if newline < 0 else newline
                elif content.startswith("/*", ahead):
                    close = content.find("*/", ahead + 2)
                    ahead = n if close < 0 else close + 2
                else:
                    break
            if ahead < n and content[ahead] in "}]":
                i += 1
            else:
                out.append(char)
                i += 1
        else:
            out.append(char)
            i += 1
    return json.loads("".join(out))


def looks_like_bun(data: object) -> bool:
    """Return True for a parsed `bun.lock`."""
    return (
        isinstance(data, dict)
        and "lockfileVersion" in data
        and isinstance(data.get("workspaces"), dict)
        and isinstance(data.get("packages"), dict)
    )


def _bun_path(key: str) -> list[str]:
    """Split a bun.lock install path into package names: `a/@s/b` is a, @s/b."""
    parts = key.split("/")
    names: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i].startswith("@") and i + 1 < len(parts):
            names.append(f"{parts[i]}/{parts[i + 1]}")
            i += 2
        else:
            names.append(parts[i])
            i += 1
    return names


def read_bun(ecosystem: str, source: str, content: str) -> lockfiles.Project | None:
    """Parse the text `bun.lock`."""
    data = jsonc_loads(content)
    if not looks_like_bun(data):
        return None
    packages = data["packages"]

    resolved: dict[str, tuple[str, str]] = {}
    meta: dict[str, dict] = {}
    skipped = 0
    for key, value in packages.items():
        if not isinstance(value, list) or not value or not isinstance(value[0], str):
            skipped += 1
            continue
        name, version = value[0].rsplit("@", 1) if "@" in value[0][1:] else ("", "")
        if version.startswith("workspace:"):
            continue
        if not name or not _RELEASE.match(version):
            skipped += 1
            continue
        resolved[key] = (name, version)
        meta[key] = next((v for v in value[1:] if isinstance(v, dict)), {})

    def lands_on(origin: list[str], dep: str) -> str | None:
        for cut in range(len(origin), -1, -1):
            candidate = "/".join([*origin[:cut], dep])
            if candidate in resolved:
                return candidate
        return None

    rows: dict[tuple[str, str], set[str]] = {row: set() for row in resolved.values()}
    for key, fields in meta.items():
        origin = _bun_path(key)
        for kind in ("dependencies", "optionalDependencies", "peerDependencies"):
            for dep in lockfiles._mapping(fields.get(kind)):
                target = lands_on(origin, dep)
                if target is not None and target != key:
                    rows[resolved[target]].add(resolved[key][0])

    direct: set[tuple[str, str]] = set()
    for path, workspace in data["workspaces"].items():
        workspace = lockfiles._mapping(workspace)
        own = workspace.get("name")
        origin = [own] if path and isinstance(own, str) else []
        for kind in ("dependencies", "devDependencies", "optionalDependencies"):
            for dep in lockfiles._mapping(workspace.get(kind)):
                target = lands_on(origin, dep)
                if target is not None:
                    direct.add(resolved[target])
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=_rows(rows, direct),
        skipped=skipped,
    )


def looks_like_pipfile_lock(data: object) -> bool:
    """Return True for a parsed `Pipfile.lock`."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("_meta"), dict)
        and any(isinstance(data.get(k), dict) for k in ("default", "develop"))
    )


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pipfile_lock(
    ecosystem: str, source: str, content: str, declared: dict[str, str] | None
) -> lockfiles.Project | None:
    """Parse `Pipfile.lock`."""
    data = json.loads(content)
    if not looks_like_pipfile_lock(data):
        return None
    wanted = {_pep503(name) for name in declared} if declared is not None else None
    found: dict[str, str] = {}
    skipped = 0
    for section in ("default", "develop"):
        for name, entry in lockfiles._mapping(data.get(section)).items():
            version = lockfiles._mapping(entry).get("version")
            if not isinstance(version, str) or not version.startswith("=="):
                skipped += 1
                continue
            version = version.removeprefix("===").removeprefix("==").strip()
            if not _RELEASE.match(version):
                skipped += 1
                continue
            found.setdefault(name, version)
    if wanted is None:
        note = (
            "Pipfile.lock does not record which packages the Pipfile asked for, "
            "so every row here is listed without a parent. Read it beside its "
            "Pipfile to tell the two apart."
        )
    else:
        note = (
            "Pipfile.lock never records which package required which, so rows "
            "the Pipfile does not list are indirect and name no parent."
        )
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            lockfiles.Dependency(
                name=name,
                version=version,
                indirect=wanted is not None and _pep503(name) not in wanted,
            )
            for name, version in sorted(found.items())
        ],
        skipped=skipped,
        note=note,
    )


def pipfile_names(content: str) -> dict[str, str]:
    """Return the package names a `Pipfile` declares."""
    data = tomllib.loads(content)
    names: dict[str, str] = {}
    for section in ("packages", "dev-packages"):
        for name in lockfiles._mapping(data.get(section)):
            names[name] = ""
    return names


_PUB_HOSTS = ("https://pub.dev", "https://pub.dartlang.org")


def looks_like_pubspec_lock(data: object) -> bool:
    """Return True for a parsed `pubspec.lock`."""
    packages = data.get("packages") if isinstance(data, dict) else None
    return (
        isinstance(packages, dict)
        and bool(packages)
        and all(
            isinstance(entry, dict) and "dependency" in entry and "source" in entry
            for entry in packages.values()
        )
    )


def read_pubspec_lock(
    ecosystem: str, source: str, content: str
) -> lockfiles.Project | None:
    """Parse `pubspec.lock`."""
    data = yamlish.loads(content)
    if not looks_like_pubspec_lock(data):
        return None
    dependencies: list[lockfiles.Dependency] = []
    skipped = 0
    for name, entry in sorted(data["packages"].items()):
        kind = entry.get("source")
        if kind == "sdk":
            continue
        description = entry.get("description")
        url = description.get("url") if isinstance(description, dict) else None
        version = entry.get("version")
        if (
            kind != "hosted"
            or (isinstance(url, str) and url.rstrip("/") not in _PUB_HOSTS)
            or not isinstance(version, str)
            or not _RELEASE.match(version)
        ):
            skipped += 1
            continue
        relation = str(entry.get("dependency", ""))
        dependencies.append(
            lockfiles.Dependency(
                name=name,
                version=version,
                indirect=not relation.startswith("direct"),
            )
        )
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=dependencies,
        skipped=skipped,
        note=(
            "pubspec.lock marks which packages are transitive but never records "
            "which package required them, so transitive rows here name no parent."
        ),
    )


def looks_like_nuget_lock(data: object) -> bool:
    """Return True for a parsed NuGet `packages.lock.json`."""
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        return False
    frameworks = data.get("dependencies")
    if not isinstance(frameworks, dict) or not frameworks:
        return False
    if not all(isinstance(packages, dict) for packages in frameworks.values()):
        return False
    return any(
        isinstance(entry, dict) and ("resolved" in entry or "type" in entry)
        for packages in frameworks.values()
        for entry in packages.values()
    )


def read_nuget_lock(
    ecosystem: str, source: str, content: str
) -> lockfiles.Project | None:
    """Parse NuGet's `packages.lock.json`."""
    data = json.loads(content)
    if not looks_like_nuget_lock(data):
        return None
    rows: dict[tuple[str, str], set[str]] = {}
    spelling: dict[tuple[str, str], tuple[str, str]] = {}
    direct: set[tuple[str, str]] = set()
    skipped = 0
    for packages in data["dependencies"].values():
        here: dict[str, tuple[str, str]] = {}
        for name, entry in packages.items():
            entry = lockfiles._mapping(entry)
            if entry.get("type") == "Project":
                continue
            version = entry.get("resolved")
            if not isinstance(version, str) or not _RELEASE.match(version):
                skipped += 1
                continue
            key = (name.lower(), version)
            spelling.setdefault(key, (name, version))
            rows.setdefault(key, set())
            here[name.lower()] = key
            if entry.get("type") == "Direct":
                direct.add(key)
        for name, entry in packages.items():
            for dep in lockfiles._mapping(
                lockfiles._mapping(entry).get("dependencies")
            ):
                target = here.get(dep.lower())
                if target is not None:
                    rows[target].add(name)
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            lockfiles.Dependency(
                name=spelling[key][0],
                version=spelling[key][1],
                parents=[] if key in direct else sorted(rows[key]),
            )
            for key in sorted(rows)
        ],
        skipped=skipped,
    )


_GRADLE_LINE = re.compile(
    r"^(?P<group>[A-Za-z0-9][\w.-]*):(?P<artifact>[A-Za-z0-9][\w.-]*):"
    r"(?P<version>[^\s:=]+)(=[\w,.-]*)?$"
)


def looks_like_gradle_lockfile(content: str) -> bool:
    """Return True if `content` looks like a Gradle dependency lock file."""
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    coordinates = [line for line in lines if _GRADLE_LINE.match(line)]
    rest = [line for line in lines if not _GRADLE_LINE.match(line)]
    return bool(coordinates) and all(line.startswith("empty=") for line in rest)


def read_gradle_lockfile(
    ecosystem: str, source: str, content: str
) -> lockfiles.Project | None:
    """Parse `gradle.lockfile`."""
    found: dict[tuple[str, str], None] = {}
    skipped = 0
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("empty="):
            continue
        match = _GRADLE_LINE.match(line)
        if not match or not _RELEASE.match(match["version"]):
            skipped += 1
            continue
        found[(f"{match['group']}:{match['artifact']}", match["version"])] = None
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            lockfiles.Dependency(name=n, version=v) for n, v in sorted(found)
        ],
        skipped=skipped,
        note=(
            "gradle.lockfile does not record which dependencies the build "
            "declares or which one required each, so every row here is listed "
            "without a parent."
        ),
    )


_PROPERTY = re.compile(r"\$\{([^}]+)\}")


def looks_like_pom(content: str) -> bool:
    """Return True if `content` looks like a Maven `pom.xml`."""
    return (
        "<project" in content
        and "<artifactId>" in content
        and ("maven.apache.org/POM" in content or "<modelVersion>" in content)
    )


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(
    element: ElementTree.Element | None, name: str
) -> ElementTree.Element | None:
    if element is None:
        return None
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _children(element: ElementTree.Element | None) -> list[ElementTree.Element]:
    return [] if element is None else list(element)


def _text(element: ElementTree.Element | None, name: str) -> str | None:
    child = _child(element, name)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def read_pom(ecosystem: str, source: str, content: str) -> lockfiles.Project | None:
    """Parse a Maven `pom.xml`."""
    if "<!DOCTYPE" in content or "<!ENTITY" in content:
        return None
    root = ElementTree.fromstring(content)
    if _local(root.tag) != "project":
        return None

    parent = _child(root, "parent")
    properties: dict[str, str] = {}
    for key, value in (
        ("project.version", _text(root, "version") or _text(parent, "version")),
        ("project.groupId", _text(root, "groupId") or _text(parent, "groupId")),
        ("project.artifactId", _text(root, "artifactId")),
        ("project.parent.version", _text(parent, "version")),
        ("project.parent.groupId", _text(parent, "groupId")),
    ):
        if value:
            properties[key] = value
            properties[key.replace("project.", "pom.", 1)] = value
    if "project.version" in properties:
        properties["version"] = properties["project.version"]
    for prop in _children(_child(root, "properties")):
        if prop.text and prop.text.strip():
            properties[_local(prop.tag)] = prop.text.strip()

    def resolve(value: str | None) -> str | None:
        if value is None:
            return None
        for _ in range(10):
            expanded = _PROPERTY.sub(
                lambda m: properties.get(m[1].strip(), m[0]), value
            )
            if expanded == value:
                break
            value = expanded
        return None if "${" in value else value

    managed: dict[str, str] = {}
    for dep in _children(_child(_child(root, "dependencyManagement"), "dependencies")):
        if _local(dep.tag) != "dependency":
            continue
        group = resolve(_text(dep, "groupId"))
        artifact = resolve(_text(dep, "artifactId"))
        version = resolve(_text(dep, "version"))
        if group and artifact and version:
            managed[f"{group}:{artifact}"] = version

    found: dict[str, str] = {}
    skipped = 0
    for dep in _children(_child(root, "dependencies")):
        if _local(dep.tag) != "dependency":
            continue
        group = resolve(_text(dep, "groupId"))
        artifact = resolve(_text(dep, "artifactId"))
        if not group or not artifact or _text(dep, "scope") == "system":
            skipped += 1
            continue
        name = f"{group}:{artifact}"
        stated = _text(dep, "version")
        version = resolve(stated) if stated is not None else managed.get(name)
        if not version or not _RELEASE.match(version):
            skipped += 1
            continue
        found[name] = version

    parts = ["pom.xml lists direct dependencies only; what they pull in is not read."]
    if skipped:
        count = lockfiles._plural(skipped, "dependency was", "dependencies were")
        parts.append(
            f"{count} left out because this file does not state the "
            "version: it comes from a parent POM or BOM, a property defined "
            "elsewhere, or a range."
        )
    return lockfiles.Project(
        ecosystem=ecosystem,
        source=source,
        dependencies=[
            lockfiles.Dependency(name=n, version=v) for n, v in sorted(found.items())
        ],
        skipped=skipped,
        note=" ".join(parts),
    )
