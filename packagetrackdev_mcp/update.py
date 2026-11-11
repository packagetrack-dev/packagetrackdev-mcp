"""Update the server to the newest release when it starts."""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packagetrackdev_mcp import config

DIST = "packagetrackdev-mcp"
PACKAGE = "packagetrackdev_mcp"

ORIGIN = "https://packagetrack.dev"
VERSIONS_URL = f"{ORIGIN}/cli/versions.json"

ENV_OFF = "PACKAGETRACK_NO_AUTO_UPDATE"
ENV_UPDATED = "PACKAGETRACK_MCP_UPDATED"

CHECK_SECONDS = 2.5
INSTALL_SECONDS = 8.0
MAX_WHEEL_BYTES = 10 * 1024 * 1024
KEEP = 2
BACKOFF_SECONDS = 3600

STORE = config.CONFIG_DIR / "mcp"

READY = "READY"
BAD = "BAD"
LAST_CHECK = "last-check"

RELEASE = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


NEEDS_REINSTALL = "needs-reinstall"

_served: str | None = None
_said: set[str] = set()


def strict_release(value: object) -> str | None:
    """Return `value` when it is a plain X.Y.Z version, else None."""
    if isinstance(value, str) and RELEASE.fullmatch(value):
        return value
    return None


class UpdateError(Exception):
    """Raised with a one-line reason not to update."""


class Unreachable(UpdateError):
    """Raised when the site could not be reached at all."""


class NeedsInstaller(UpdateError):
    """Raised when this installation cannot run a release."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    url: str
    sha256: str


def _note(message: str) -> None:
    print(f"{DIST}: {message}", file=sys.stderr, flush=True)


def installed_version() -> str:
    """Return this copy's version, or "0.0.0" when not installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(DIST)
    except PackageNotFoundError:
        return "0.0.0"


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _editable() -> bool:
    """Return True for an install that points at a source checkout."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        text = distribution(DIST).read_text("direct_url.json")
    except PackageNotFoundError:
        return False
    if not text:
        return False
    try:
        return bool(json.loads(text).get("dir_info", {}).get("editable"))
    except (ValueError, AttributeError):
        return False


def _stdin_is_a_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _off() -> bool:
    return os.environ.get(ENV_OFF, "").strip().lower() in {"1", "true", "yes"}


def inactive_reason(settings: config.Config, version: str) -> str | None:
    """Return why no update is used at all, downloaded or unpacked, or None."""
    if _off():
        return "turned off"
    if not RELEASE.fullmatch(version) or version == "0.0.0":
        return "development version"
    if _editable():
        return "editable install"
    if settings.server != config.DEFAULT_SERVER:
        return "not the default server"
    return None


def skip_reason(settings: config.Config, version: str | None = None) -> str | None:
    """Return why this start does not look for an update, or None."""
    version = installed_version() if version is None else version
    if os.environ.get(ENV_UPDATED):
        return "handed over"
    reason = inactive_reason(settings, version)
    if reason is not None:
        return reason
    if _stdin_is_a_terminal():
        return "started by hand"
    return None


def describe_version(settings: config.Config) -> str:
    """Return the installed version, with the unpacked one that runs instead."""
    running = installed_version()
    if inactive_reason(settings, running) is not None:
        return running
    try:
        newer = [v for v in _unpacked() if _key(v) > _key(running)]
    except OSError:
        return running
    return f"{running} (active: {newer[-1]})" if newer else running


_POSIX = os.name != "nt"
_skipped: set[str] = set()


def _private_dir(path: Path) -> None:
    """Create `path` and any missing parent, each readable by this user only."""
    missing = []
    while not path.exists():
        missing.append(path)
        if path.parent == path:
            break
        path = path.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass


def _uid() -> int:
    return os.getuid()


def _owner_and_mode(path: Path) -> tuple[int, int]:
    info = os.stat(path, follow_symlinks=False)
    return info.st_uid, info.st_mode


def untrusted(path: Path) -> str | None:
    """Return why a release folder must not be run, or None."""
    if not _POSIX:
        return None
    try:
        owner, mode = _owner_and_mode(path)
    except OSError:
        return "it cannot be read"
    if not stat.S_ISDIR(mode):
        return "it is not a folder"
    if owner != _uid():
        return "another user owns it"
    if mode & 0o022:
        return "other users can write to it"
    return None


def _ready(version: str) -> Path | None:
    path = STORE / version
    if not (path / READY).is_file() or (path / BAD).exists():
        return None
    why = untrusted(path)
    if why is not None:
        if str(path) not in _skipped:
            _skipped.add(str(path))
            _note(
                f"skipped {path}: {why}; delete that folder if you did not put it there"
            )
        return None
    return path


def _unpacked() -> list[str]:
    """Return every ready release in the store, oldest first."""
    if not STORE.is_dir():
        return []
    found = [
        entry.name
        for entry in STORE.iterdir()
        if RELEASE.fullmatch(entry.name) and _ready(entry.name)
    ]
    return sorted(found, key=_key)


def _is_bad(version: str) -> bool:
    return (STORE / version / BAD).exists()


def _mark_bad(path: Path, reason: str) -> None:
    try:
        (path / BAD).write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass


def _tidy(running: str) -> None:
    """Remove releases no start will use again."""
    if not STORE.is_dir():
        return
    needs = _needs_installer()
    if needs is not None and _key(needs) <= max(
        [_key(running), *(_key(v) for v in _unpacked())]
    ):
        try:
            (STORE / NEEDS_REINSTALL).unlink(missing_ok=True)
        except OSError:
            pass
    keep = {v for v in _unpacked() if _key(v) > _key(running)}
    keep = set(sorted(keep, key=_key)[-KEEP:]) | {running}
    now = time.time()
    for entry in STORE.iterdir():
        name = entry.name
        if name.startswith("."):
            try:
                stale = now - entry.stat().st_mtime > 3600
            except OSError:
                continue
            if stale:
                shutil.rmtree(entry, ignore_errors=True)
            continue
        if not RELEASE.fullmatch(name) or name in keep:
            continue
        if (entry / BAD).exists() and _key(name) > _key(running):
            continue
        shutil.rmtree(entry, ignore_errors=True)


def backoff_until() -> float | None:
    """Return when the next check is due after an unreachable site, or None."""
    try:
        stamp = float((STORE / LAST_CHECK).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    now = time.time()
    if stamp > now + 60:
        return None
    until = stamp + BACKOFF_SECONDS
    return until if until > now else None


def _remember_unreachable() -> None:
    try:
        _private_dir(STORE)
        (STORE / LAST_CHECK).write_text(f"{int(time.time())}\n", encoding="utf-8")
    except OSError:
        pass


def _forget_unreachable() -> None:
    try:
        (STORE / LAST_CHECK).unlink(missing_ok=True)
    except OSError:
        pass


_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_CLAUSE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*$")


def _numbers(version: str) -> tuple[int, ...]:
    """Return the release numbers at the front of a version."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return (left > right) - (left < right)


def satisfies(version: str, specifier: str) -> bool:
    """Return whether `version` meets a specifier such as `>=2,<3`."""
    have = _numbers(version)
    for clause in filter(None, (c.strip() for c in specifier.split(","))):
        match = _CLAUSE.match(clause)
        if not match:
            raise UpdateError(f"cannot check the requirement {clause!r}")
        op, wanted = match.group(1), _numbers(match.group(2))
        order = _compare(have, wanted)
        ok = {
            ">=": order >= 0,
            "<=": order <= 0,
            ">": order > 0,
            "<": order < 0,
            "==": order == 0,
            "!=": order != 0,
        }[op]
        if not ok:
            return False
    return True


def parse_requirement(line: str) -> tuple[str, str] | None:
    """Return the name and specifier of a Requires-Dist line, None for an extra."""
    requirement, _, marker = line.partition(";")
    if marker.strip():
        if "extra" in marker:
            return None
        raise UpdateError(f"cannot check the marker in {line.strip()!r}")
    match = _REQUIREMENT.match(requirement)
    if not match:
        raise UpdateError(f"cannot read the requirement {line.strip()!r}")
    specifier = match.group(3).strip().strip("()")
    return match.group(1), specifier


def check_requirements(metadata: str) -> None:
    """Raise unless this environment can run the release `metadata` describes."""
    from importlib.metadata import PackageNotFoundError, version

    python = ".".join(str(part) for part in sys.version_info[:3])
    for line in metadata.splitlines():
        if not line.strip():
            break  # the headers end at the first blank line
        field, _, value = line.partition(":")
        if field == "Requires-Python":
            if not satisfies(python, value.strip()):
                raise NeedsInstaller(f"needs Python {value.strip()}")
        elif field == "Requires-Dist":
            parsed = parse_requirement(value)
            if parsed is None:
                continue
            name, specifier = parsed
            try:
                have = version(name)
            except PackageNotFoundError:
                raise NeedsInstaller(f"needs {name}, which is not installed") from None
            if not satisfies(have, specifier):
                raise NeedsInstaller(f"needs {name}{specifier}, this has {have}")


def _client():
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(CHECK_SECONDS),
        follow_redirects=False,
        headers={"User-Agent": f"{DIST}/{installed_version()}"},
    )


def wheel_name(version: str) -> str:
    return f"{PACKAGE}-{version}-py3-none-any.whl"


def parse_versions(document: object) -> Release:
    """Return this server's entry of versions.json."""
    entry = document.get(DIST) if isinstance(document, dict) else None
    if not isinstance(entry, dict):
        raise UpdateError("versions.json names no release of this server")
    version, url, digest = entry.get("version"), entry.get("url"), entry.get("sha256")
    if not isinstance(version, str) or not RELEASE.fullmatch(version):
        raise UpdateError("versions.json has no usable version")
    if url != f"{ORIGIN}/cli/{wheel_name(version)}":
        raise UpdateError("versions.json points somewhere other than packagetrack.dev")
    if not isinstance(digest, str) or not SHA256.match(digest):
        raise UpdateError("versions.json has no usable checksum")
    return Release(version, url, digest)


def fetch_latest(client) -> Release:
    try:
        response = client.get(VERSIONS_URL)
    except Exception as exc:  # noqa: BLE001 - every failure is "no update"
        raise Unreachable(f"could not reach {ORIGIN} ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise UpdateError(f"versions.json answered {response.status_code}")
    try:
        return parse_versions(response.json())
    except ValueError:
        raise UpdateError("versions.json is not JSON") from None


def download(client, release: Release) -> bytes:
    """Download the wheel and check it against the published SHA256."""
    import hashlib

    digest = hashlib.sha256()
    chunks = []
    size = 0
    try:
        with client.stream("GET", release.url) as response:
            if response.status_code != 200:
                raise UpdateError(f"the wheel answered {response.status_code}")
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_WHEEL_BYTES:
                    raise UpdateError("the wheel is larger than any release")
                digest.update(chunk)
                chunks.append(chunk)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpdateError(f"the download failed ({type(exc).__name__})") from None
    if digest.hexdigest() != release.sha256:
        raise UpdateError("the download does not match its checksum")
    return b"".join(chunks)


def _members(archive: zipfile.ZipFile, version: str) -> list[zipfile.ZipInfo]:
    """Return the files to unpack: the package and its dist-info, nothing else."""
    allowed = (f"{PACKAGE}/", f"{PACKAGE}-{version}.dist-info/")
    members = []
    for info in archive.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        parts = PurePosixPath(name).parts
        if info.orig_filename != name:
            raise UpdateError(
                f"the wheel holds an unexpected file {info.orig_filename!r}"
            )
        if (
            "\\" in name
            or ":" in name
            or name.startswith("/")
            or ".." in parts
            or not name.startswith(allowed)
        ):
            raise UpdateError(f"the wheel holds an unexpected file {name!r}")
        members.append(info)
    names = {info.filename for info in members}
    if f"{PACKAGE}/__main__.py" not in names or f"{allowed[1]}METADATA" not in names:
        raise UpdateError("the wheel is not a release of this server")
    return members


def unpack(data: bytes, release: Release, cancelled: threading.Event) -> Path:
    """Unpack into the store, atomically, and return the release's directory."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("the download is not a wheel") from None
    with archive:
        members = _members(archive, release.version)
        metadata = archive.read(f"{PACKAGE}-{release.version}.dist-info/METADATA")
        metadata = metadata.decode("utf-8", errors="replace")
        if f"\nVersion: {release.version}\n" not in f"\n{metadata}":
            raise UpdateError("the wheel says it is a different version")
        check_requirements(metadata)
        _private_dir(STORE)
        staging = STORE / f".{release.version}-{os.getpid()}-{threading.get_ident()}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            for info in members:
                target = staging.joinpath(*PurePosixPath(info.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
            (staging / READY).write_text(release.sha256 + "\n", encoding="utf-8")
            if cancelled.is_set():
                raise UpdateError("took too long")
            final = STORE / release.version
            try:
                os.rename(staging, final)
            except OSError:
                if _ready(release.version) is None:
                    raise UpdateError("could not move the release into place") from None
            return final
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _ours(module_name: str) -> bool:
    return module_name == PACKAGE or module_name.startswith(f"{PACKAGE}.")


def hand_over(path: Path, version: str) -> Callable[[], int] | None:
    """Import the release at `path` in place of this one and return its main."""
    saved_path = list(sys.path)
    saved_modules = {
        name: module for name, module in sys.modules.items() if _ours(name)
    }
    for name in saved_modules:
        del sys.modules[name]
    sys.path.insert(0, str(path))
    importlib.invalidate_caches()
    os.environ[ENV_UPDATED] = version
    try:
        module = importlib.import_module(f"{PACKAGE}.__main__")
        origin = Path(module.__file__ or "").resolve()
        if path.resolve() not in origin.parents:
            raise ImportError(f"imported {origin} instead")
        return module.main
    except Exception as exc:  # noqa: BLE001 - a broken release must not stop us
        _mark_bad(path, f"{type(exc).__name__}: {exc}")
        for name in [name for name in sys.modules if _ours(name)]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
        importlib.invalidate_caches()
        os.environ.pop(ENV_UPDATED, None)
        running = installed_version()
        _note(f"{version} did not start ({type(exc).__name__}); starting {running}")
        return None


class _Attempt:
    def __init__(self) -> None:
        self.checked = threading.Event()
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.release: Release | None = None
        self.path: Path | None = None
        self.error: str | None = None
        self.answered = False
        self.unreachable = False


def _look(attempt: _Attempt, newest_here: str) -> None:
    """Look for a newer release and unpack it."""
    try:
        with _client() as client:
            release = fetch_latest(client)
            attempt.answered = True
            global _served
            _served = strict_release(release.version)
            if _key(release.version) <= _key(newest_here) or _is_bad(release.version):
                return
            attempt.release = release
            attempt.checked.set()
            data = download(client, release)
            if attempt.cancelled.is_set():
                return
            attempt.path = unpack(data, release, attempt.cancelled)
    except UpdateError as exc:
        if isinstance(exc, NeedsInstaller) and attempt.release is not None:
            _remember_needs_installer(attempt.release.version)
        attempt.error = str(exc)
        attempt.unreachable = isinstance(exc, Unreachable)
    except Exception as exc:  # noqa: BLE001
        attempt.error = f"unexpected {type(exc).__name__}"
    finally:
        attempt.checked.set()
        attempt.done.set()


def newer_main(settings: config.Config) -> Callable[[], int] | None:
    """Return the main of a newer release to run instead of this one, or None."""
    try:
        return _newer_main(settings)
    except Exception as exc:  # noqa: BLE001 - the server starts regardless
        _note(f"update skipped ({type(exc).__name__}); starting {installed_version()}")
        return None


def _check(newest_here: str) -> tuple[Release, Path] | None:
    """Ask the site once and return a newer release unpacked, or None."""
    attempt = _Attempt()
    threading.Thread(target=_look, args=(attempt, newest_here), daemon=True).start()
    if not attempt.checked.wait(CHECK_SECONDS):
        attempt.cancelled.set()
        _remember_unreachable()
        _note(f"no answer from {ORIGIN} in {CHECK_SECONDS:g}s; not updating")
        return None
    if attempt.answered:
        _forget_unreachable()
    if attempt.release is not None and not attempt.done.wait(INSTALL_SECONDS):
        attempt.cancelled.set()
        _note(f"{attempt.release.version} took too long to download; not updating")
        return None
    if attempt.unreachable:
        _remember_unreachable()
    if attempt.error:
        _note(f"not updating: {attempt.error}")
    if attempt.path is not None and attempt.release is not None:
        return attempt.release, attempt.path
    return None


def _newer_main(settings: config.Config) -> Callable[[], int] | None:
    running = installed_version()
    if skip_reason(settings, running) is not None:
        return None
    local = [v for v in _unpacked() if _key(v) > _key(running)]
    newest_here = local[-1] if local else running

    fetched = None
    until = backoff_until()
    if until is None:
        fetched = _check(newest_here)
    else:
        at = time.strftime("%H:%M", time.localtime(until))
        _note(f"no connection at an earlier start; next update check after {at}")

    if fetched is not None:
        target, path = fetched[0].version, fetched[1]
    elif local:
        target, path = newest_here, STORE / newest_here
    else:
        _tidy(running)
        return None

    entry = hand_over(path, target)
    if entry is None:
        return None
    if fetched is not None:
        _note(f"updated {running} -> {target}")
    _tidy(target)
    return entry


COMMAND = DIST
INSTALL_LINES = (
    "  curl -fsSL https://packagetrack.dev/install.sh | sh      (macOS, Linux)\n"
    "  irm https://packagetrack.dev/install.ps1 | iex            (Windows PowerShell)"
)
SOFT_NEWER = (
    "A newer version of the PackageTrack tools is ready on this computer and "
    "will be used once this server restarts."
)
SOFT_LATEST = (
    "A newer version of the PackageTrack tools is available and will be "
    "fetched once this server restarts."
)
HARD = (
    "The newest version of the PackageTrack tools could not start on this "
    "computer and needs to be reinstalled;"
)
WHAT_TO_DO = f"call the update_status tool, or ask the user to run `{COMMAND} doctor`."
RESTART = "Restart the MCP server in the coding agent (or restart the agent)"


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """What the running release is, against what is on this computer."""

    kind: str
    running: str
    active: str
    latest: str | None = None
    version: str | None = None
    why: str | None = None


def _release_dirs() -> list[str]:
    try:
        return [
            entry.name
            for entry in STORE.iterdir()
            if RELEASE.fullmatch(entry.name) and entry.is_dir()
        ]
    except OSError:
        return []


def _remember_needs_installer(version: str) -> None:
    try:
        _private_dir(STORE)
        (STORE / NEEDS_REINSTALL).write_text(f"{version}\n", encoding="utf-8")
    except OSError:
        pass


def _needs_installer() -> str | None:
    try:
        text = (STORE / NEEDS_REINSTALL).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return strict_release(text)


def diagnose(
    running: str, latest: str | None = None, installed: str | None = None
) -> Diagnosis:
    """Compare a running release with the store; read only, no network."""
    installed = installed_version() if installed is None else installed
    try:
        ready = _unpacked()
    except OSError:
        ready = []
    active = max([installed, *ready], key=_key)
    bad = [v for v in _release_dirs() if _is_bad(v) and untrusted(STORE / v) is None]
    hard: tuple[str, str] | None = None
    newest_bad = max(bad, key=_key, default=None)
    if newest_bad is not None and _key(newest_bad) > _key(active):
        hard = (newest_bad, "it failed when it started")
    needs = _needs_installer()
    if needs is not None and _key(needs) > _key(active):
        if hard is None or _key(needs) > _key(hard[0]):
            hard = (
                needs,
                "it needs a newer Python or newer packages than this installation has",
            )
    if hard is not None:
        return Diagnosis("reinstall", running, active, latest, *hard)
    if _key(running) < _key(active):
        return Diagnosis("newer", running, active, latest)
    if _key(running) > _key(active):
        return Diagnosis("gone", running, active, latest)
    if latest is not None and _key(latest) > _key(active):
        return Diagnosis("latest", running, active, latest)
    return Diagnosis("ok", running, active, latest)


def describe(found: Diagnosis) -> tuple[int, str]:
    """Return doctor's exit code and the fixed sentence for `found`."""
    if found.kind == "reinstall":
        return 3, (
            f"{COMMAND} {found.version} could not start with this installation "
            f"({found.why}). Show the user these commands; do not run them "
            "without the user's approval, because they reinstall the "
            f"PackageTrack tools:\n{INSTALL_LINES}"
        )
    if found.kind == "newer":
        return 1, (
            f"{COMMAND} {found.active} is ready on this computer, but the server "
            f"that is running is {found.running}. {RESTART} to use {found.active}."
        )
    if found.kind == "gone":
        return 1, (
            f"The server that is running is {found.running}, which is no longer "
            f"on this computer; {found.active} is. {RESTART}."
        )
    if found.kind == "latest":
        return 1, (
            f"packagetrack.dev serves {COMMAND} {found.latest}, which is not on "
            f"this computer yet. {RESTART}; it updates itself when it starts."
        )
    return 0, (
        f"{COMMAND} {found.running} is running and is the newest version on "
        "this computer; nothing to do."
    )


def inactive_text(settings: config.Config, version: str) -> str | None:
    """Return why this copy does not update itself, as a sentence, or None."""
    reason = inactive_reason(settings, version)
    if reason is None:
        return None
    if reason == "turned off":
        return f"Self-update is turned off ({ENV_OFF}); nothing to do."
    return f"This copy does not update itself ({reason}); nothing to do."


def store_line() -> str:
    """Name the releases in the store, with the home folder written as ~."""
    try:
        where = "~/" + STORE.relative_to(Path.home()).as_posix()
    except (ValueError, RuntimeError, OSError):
        where = None
    items = []
    for version in sorted(_release_dirs(), key=_key):
        if untrusted(STORE / version) is not None:
            continue
        if _is_bad(version):
            items.append(f"{version} (did not start)")
        elif _ready(version) is not None:
            items.append(version)
    listed = ", ".join(items) or "none"
    if where is None:
        return f"Releases on this computer: {listed}."
    return f"Releases in {where}: {listed}."


def status_text(settings: config.Config | None = None) -> str:
    """Answer update_status: doctor's answer for this very process, read only."""
    settings = config.load() if settings is None else settings
    running = installed_version()
    if strict_release(running) is None:
        return "This copy does not update itself (development version); nothing to do."
    off = inactive_text(settings, running)
    if off is not None:
        return off
    _, text = describe(diagnose(running, _served, installed=running))
    return f"{text}\n{store_line()}"


def reply_note(settings: config.Config | None = None) -> str | None:
    """Return the note a tool reply ends with, or None."""
    try:
        running = installed_version()
        settings = config.load() if settings is None else settings
        if strict_release(running) is None or inactive_reason(settings, running):
            return None
        found = diagnose(running, _served, installed=running)
        if found.kind == "reinstall":
            _said.add("hard")
            return f"{HARD} {WHAT_TO_DO}"
        if found.kind in ("newer", "latest"):
            if "soft" in _said:
                return None
            _said.add("soft")
            first = SOFT_NEWER if found.kind == "newer" else SOFT_LATEST
            return f"{first} To see what to do, {WHAT_TO_DO}"
        return None
    except Exception:  # noqa: BLE001 - a reply never fails over its note
        return None
