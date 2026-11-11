"""Self-update at start: when it runs, what it refuses, and that it never blocks."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from packagetrackdev_mcp import __main__ as entry
from packagetrackdev_mcp import config, server, update

DEFAULT = config.Config(api_key=None, server=config.DEFAULT_SERVER)
PACKAGE_DIR = Path(server.__file__).resolve().parent


def make_wheel(
    version: str,
    main: str = "def main():\n    return 42\n",
    requires: tuple[str, ...] = (),
    python: str = ">=3.11",
    extra: dict[str, str] | None = None,
    package_dir: Path | None = None,
) -> bytes:
    """A wheel of this server at `version`, small or built from the real package."""
    buffer = io.BytesIO()
    info = f"packagetrackdev_mcp-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.4",
        "Name: packagetrackdev-mcp",
        f"Version: {version}",
        f"Requires-Python: {python}",
        *(f"Requires-Dist: {line}" for line in requires),
        "",
        "The server.",
    ]
    with zipfile.ZipFile(buffer, "w") as archive:
        if package_dir is None:
            archive.writestr("packagetrackdev_mcp/__init__.py", "")
            archive.writestr("packagetrackdev_mcp/__main__.py", main)
        else:
            for path in sorted(package_dir.rglob("*.py")):
                name = path.relative_to(package_dir.parent).as_posix()
                archive.writestr(name, path.read_text(encoding="utf-8"))
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
        archive.writestr(f"{info}/METADATA", "\n".join(metadata) + "\n")
        archive.writestr(f"{info}/RECORD", "")
    return buffer.getvalue()


def document(version: str, data: bytes, /, **override) -> dict:
    entry = {
        "version": version,
        "url": f"https://packagetrack.dev/cli/{update.wheel_name(version)}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    entry.update(override)
    return {"packagetrackdev": {"version": "9.9.9"}, "packagetrackdev-mcp": entry}


class Site:
    """packagetrack.dev as a MockTransport, recording what was asked."""

    def __init__(self, version: str = "0.2.1", data: bytes | None = None, **override):
        self.data = make_wheel(version) if data is None else data
        self.document = document(version, self.data, **override)
        self.requests: list[str] = []
        self.fail: Exception | None = None
        self.delay = 0.0
        self.released = threading.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.delay:
            self.released.wait(self.delay)
        if self.fail is not None:
            raise self.fail
        if request.url.path == "/cli/versions.json":
            return httpx.Response(200, json=self.document)
        return httpx.Response(200, content=self.data)


@pytest.fixture
def site(monkeypatch, tmp_path):
    """A release 0.2.0 that would update, with the store under tmp_path."""
    site = Site()
    monkeypatch.setattr(update, "STORE", tmp_path / "store")
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    monkeypatch.setattr(update, "_editable", lambda: False)
    monkeypatch.setattr(update, "_stdin_is_a_terminal", lambda: False)
    monkeypatch.setattr(
        update,
        "_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(site.handler), follow_redirects=False
        ),
    )
    for name in (update.ENV_OFF, update.ENV_UPDATED, config.ENV_SERVER):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")

    saved_path = list(sys.path)
    saved = {n: m for n, m in sys.modules.items() if update._ours(n)}
    yield site
    site.released.set()
    for name in [n for n in sys.modules if update._ours(n)]:
        del sys.modules[name]
    sys.modules.update(saved)
    sys.path[:] = saved_path
    os.environ.pop(update.ENV_UPDATED, None)


def _store_entries() -> list[str]:
    if not update.STORE.exists():
        return []
    return sorted(p.name for p in update.STORE.iterdir())


def test_a_newer_release_is_unpacked_and_handed_over(site, capsys):
    main = update.newer_main(DEFAULT)
    assert main is not None and main() == 42
    assert (update.STORE / "0.2.1" / update.READY).is_file()
    assert os.environ[update.ENV_UPDATED] == "0.2.1"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "updated 0.2.0 -> 0.2.1" in captured.err
    assert site.requests == [
        "https://packagetrack.dev/cli/versions.json",
        "https://packagetrack.dev/cli/packagetrackdev_mcp-0.2.1-py3-none-any.whl",
    ]


@pytest.mark.parametrize("version", ["0.2.0", "0.1.9"])
def test_the_same_or_an_older_release_downloads_nothing(site, capsys, version):
    site.document = document(version, site.data)
    assert update.newer_main(DEFAULT) is None
    assert site.requests == ["https://packagetrack.dev/cli/versions.json"]
    assert _store_entries() == []
    assert capsys.readouterr() == ("", "")


def test_a_release_unpacked_earlier_starts_without_the_network(site, capsys):
    assert update.newer_main(DEFAULT) is not None
    site.fail = httpx.ConnectError("offline")
    for name in [n for n in sys.modules if update._ours(n)]:
        del sys.modules[name]
    os.environ.pop(update.ENV_UPDATED)
    main = update.newer_main(DEFAULT)
    assert main is not None and main() == 42
    assert "not updating" in capsys.readouterr().err


@pytest.mark.parametrize(
    "setup",
    [
        lambda mp: mp.setenv(update.ENV_OFF, "1"),
        lambda mp: mp.setenv(update.ENV_UPDATED, "0.2.0"),
        lambda mp: mp.setattr(update, "installed_version", lambda: "0.2.1.dev0"),
        lambda mp: mp.setattr(update, "installed_version", lambda: "0.2.0+local"),
        lambda mp: mp.setattr(update, "installed_version", lambda: "0.0.0"),
        lambda mp: mp.setattr(update, "_editable", lambda: True),
        lambda mp: mp.setattr(update, "_stdin_is_a_terminal", lambda: True),
    ],
    ids=["off", "handed-over", "dev", "local", "not-installed", "editable", "tty"],
)
def test_it_does_not_run(site, monkeypatch, capsys, setup):
    setup(monkeypatch)
    assert update.newer_main(DEFAULT) is None
    assert site.requests == []
    assert capsys.readouterr() == ("", "")


def test_a_server_other_than_the_default_never_reaches_production(site):
    local = config.Config(api_key=None, server="http://127.0.0.1:3000")
    assert update.newer_main(local) is None
    assert site.requests == []


def test_the_default_server_comes_from_the_config_not_a_guess(site, monkeypatch):
    monkeypatch.setenv(config.ENV_SERVER, "http://127.0.0.1:8000")
    assert update.skip_reason(config.load(), "0.2.0") == "not the default server"


def test_no_answer_in_time_starts_the_installed_release(site, monkeypatch, capsys):
    monkeypatch.setattr(update, "CHECK_SECONDS", 0.2)
    site.delay = 10.0
    started = time.monotonic()
    assert update.newer_main(DEFAULT) is None
    assert time.monotonic() - started < 2.0
    assert "no answer from https://packagetrack.dev" in capsys.readouterr().err


def test_a_slow_download_is_abandoned(site, monkeypatch, capsys):
    monkeypatch.setattr(update, "INSTALL_SECONDS", 0.2)
    original = site.handler

    def slow_wheel(request):
        if request.url.path.endswith(".whl"):
            site.released.wait(10.0)
        return original(request)

    site.handler = slow_wheel
    assert update.newer_main(DEFAULT) is None
    assert "took too long" in capsys.readouterr().err
    site.released.set()


def test_no_connection_starts_the_installed_release(site, capsys):
    site.fail = httpx.ConnectError("no route")
    assert update.newer_main(DEFAULT) is None
    err = capsys.readouterr().err
    assert "could not reach https://packagetrack.dev (ConnectError)" in err


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="oops"),
        httpx.Response(302, headers={"location": "https://elsewhere.example/v.json"}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"packagetrackdev": {"version": "1.0.0"}}),
    ],
    ids=["500", "redirect", "not-json", "no-entry"],
)
def test_an_unusable_answer_starts_the_installed_release(site, capsys, response):
    site.handler = lambda request: response
    assert update.newer_main(DEFAULT) is None
    assert "not updating" in capsys.readouterr().err
    assert _store_entries() == []


@pytest.mark.parametrize(
    "override",
    [
        {"url": "https://evil.example/cli/packagetrackdev_mcp-0.2.1-py3-none-any.whl"},
        {
            "url": "http://packagetrack.dev/cli/packagetrackdev_mcp-0.2.1-py3-none-any.whl"
        },
        {
            "url": "https://packagetrack.dev/cli/packagetrackdev_mcp-0.3.0-py3-none-any.whl"
        },
        {"version": "0.2.1rc1"},
        {"sha256": "not-a-digest"},
    ],
    ids=["other-host", "plain-http", "other-file", "prerelease", "bad-digest"],
)
def test_a_release_named_anywhere_but_our_own_wheel_is_refused(site, capsys, override):
    site.document = document("0.2.1", site.data, **override)
    assert update.newer_main(DEFAULT) is None
    assert site.requests == ["https://packagetrack.dev/cli/versions.json"]
    assert "not updating" in capsys.readouterr().err


def test_a_download_that_does_not_match_its_checksum_installs_nothing(site, capsys):
    site.document = document("0.2.1", b"something else")
    assert update.newer_main(DEFAULT) is None
    assert "does not match its checksum" in capsys.readouterr().err
    assert _store_entries() == []


def test_a_truncated_download_installs_nothing(site, capsys):
    truncated = site.data[: len(site.data) // 2]
    site.data = truncated
    site.document = document("0.2.1", truncated)
    assert update.newer_main(DEFAULT) is None
    assert "not a wheel" in capsys.readouterr().err
    assert _store_entries() == []


def test_a_wheel_reaching_outside_its_directory_is_refused(site, capsys, tmp_path):
    data = make_wheel("0.2.1", extra={"../escaped.py": "print('no')"})
    site.data, site.document = data, document("0.2.1", data)
    assert update.newer_main(DEFAULT) is None
    assert "unexpected file" in capsys.readouterr().err
    assert not list(tmp_path.rglob("escaped.py"))
    assert _store_entries() == []


def test_a_wheel_carrying_another_package_is_refused(site, capsys):
    data = make_wheel("0.2.1", extra={"sitecustomize.py": "import os"})
    site.data, site.document = data, document("0.2.1", data)
    assert update.newer_main(DEFAULT) is None
    assert "unexpected file 'sitecustomize.py'" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kwargs", "said"),
    [
        ({"requires": ("httpx>=99",)}, "needs httpx>=99"),
        ({"requires": ("no-such-package-here>=1",)}, "which is not installed"),
        ({"requires": ("httpx~=0.28",)}, "cannot check the requirement"),
        ({"requires": ("pywin32>=1; sys_platform == 'win32'",)}, "cannot check"),
        ({"python": ">=3.99"}, "needs Python >=3.99"),
    ],
    ids=["too-old", "missing", "unknown-operator", "marker", "python"],
)
def test_a_release_this_environment_cannot_run_is_not_taken(site, capsys, kwargs, said):
    data = make_wheel("0.2.1", **kwargs)
    site.data, site.document = data, document("0.2.1", data)
    assert update.newer_main(DEFAULT) is None
    assert said in capsys.readouterr().err
    noted = said.startswith("needs") or "not installed" in said
    assert _store_entries() == ([update.NEEDS_REINSTALL] if noted else [])
    if noted:
        assert (update.STORE / update.NEEDS_REINSTALL).read_text(
            encoding="utf-8"
        ) == "0.2.1\n"


def test_an_extra_does_not_count_as_a_requirement(site):
    data = make_wheel("0.2.1", requires=("httpx>=0.20", "rich>=99; extra == 'pretty'"))
    site.data, site.document = data, document("0.2.1", data)
    assert update.newer_main(DEFAULT) is not None


def test_a_release_that_fails_to_import_is_put_back_and_remembered(site, capsys):
    original = sys.modules["packagetrackdev_mcp"]
    data = make_wheel("0.2.1", main="raise RuntimeError('broken release')\n")
    site.data, site.document = data, document("0.2.1", data)

    assert update.newer_main(DEFAULT) is None
    assert sys.modules["packagetrackdev_mcp"] is original
    assert update.ENV_UPDATED not in os.environ
    err = capsys.readouterr().err
    assert "0.2.1 did not start (RuntimeError); starting 0.2.0" in err
    assert (update.STORE / "0.2.1" / update.BAD).is_file()

    site.requests.clear()
    assert update.newer_main(DEFAULT) is None
    assert site.requests == ["https://packagetrack.dev/cli/versions.json"]


def test_nothing_it_does_can_raise(site, monkeypatch, capsys):
    def explode():
        raise PermissionError("store not writable")

    monkeypatch.setattr(update, "_unpacked", explode)
    assert update.newer_main(DEFAULT) is None
    assert "update skipped (PermissionError)" in capsys.readouterr().err


def _last_check() -> Path:
    return update.STORE / update.LAST_CHECK


def test_no_connection_holds_the_next_starts_off_for_an_hour(site, capsys):
    site.fail = httpx.ConnectError("no route")
    assert update.newer_main(DEFAULT) is None
    assert _last_check().is_file()
    site.requests.clear()
    capsys.readouterr()

    assert update.newer_main(DEFAULT) is None
    assert site.requests == []
    assert "next update check after" in capsys.readouterr().err


def test_a_stamp_written_late_in_a_second_still_holds(site, monkeypatch):
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: 1_000_000.9)
        update._remember_unreachable()
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: 1_000_000.95)
        until = update.backoff_until()
    assert until == 1_000_000 + update.BACKOFF_SECONDS


def test_a_timeout_holds_the_next_starts_off_too(site, monkeypatch):
    monkeypatch.setattr(update, "CHECK_SECONDS", 0.2)
    site.delay = 10.0
    assert update.newer_main(DEFAULT) is None
    assert _last_check().is_file()


def test_an_hour_later_it_asks_again_and_an_answer_clears_the_wait(site):
    update.STORE.mkdir(parents=True)
    _last_check().write_text(f"{int(time.time()) - update.BACKOFF_SECONDS - 5}\n")
    assert update.newer_main(DEFAULT) is not None
    assert site.requests[0] == "https://packagetrack.dev/cli/versions.json"
    assert not _last_check().exists()


def test_an_answer_with_nothing_newer_clears_the_wait_as_well(site):
    update.STORE.mkdir(parents=True)
    _last_check().write_text(f"{int(time.time()) - update.BACKOFF_SECONDS - 5}\n")
    site.document = document("0.2.0", site.data)
    assert update.newer_main(DEFAULT) is None
    assert not _last_check().exists()


def test_a_site_that_answers_badly_is_not_a_lost_connection(site):
    site.handler = lambda request: httpx.Response(500)
    assert update.newer_main(DEFAULT) is None
    assert not _last_check().exists()


@pytest.mark.parametrize("stamp", ["garbage", str(time.time() + 86_400)])
def test_an_unreadable_or_future_stamp_does_not_hold_checks_off(site, stamp):
    update.STORE.mkdir(parents=True)
    _last_check().write_text(stamp)
    update.newer_main(DEFAULT)
    assert site.requests[:1] == ["https://packagetrack.dev/cli/versions.json"]


def test_while_waiting_a_release_unpacked_earlier_still_runs(site):
    assert update.newer_main(DEFAULT) is not None
    for name in [n for n in sys.modules if update._ours(n)]:
        del sys.modules[name]
    os.environ.pop(update.ENV_UPDATED)
    _last_check().write_text(f"{int(time.time())}\n")
    site.requests.clear()
    main = update.newer_main(DEFAULT)
    assert main is not None and main() == 42
    assert site.requests == []


def test_tidying_keeps_the_stamp(site):
    update.STORE.mkdir(parents=True)
    _last_check().write_text("1")
    update._tidy("0.2.0")
    assert _last_check().exists()


def _version_line(capsys, *args: str) -> str:
    with pytest.raises(SystemExit) as stopped:
        entry.build_parser().parse_args([*args, "--version"])
    assert stopped.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


def test_version_names_the_update_that_runs(site, capsys):
    assert update.newer_main(DEFAULT) is not None
    capsys.readouterr()
    os.environ.pop(update.ENV_UPDATED)
    line = _version_line(capsys)
    assert line == "packagetrackdev-mcp 0.2.0 (active: 0.2.1)\n"


def test_version_without_an_update_is_the_installed_one(site, capsys):
    assert _version_line(capsys) == "packagetrackdev-mcp 0.2.0\n"


@pytest.mark.parametrize(
    "setup",
    [
        lambda mp: mp.setenv(update.ENV_OFF, "1"),
        lambda mp: mp.setenv(config.ENV_SERVER, "http://127.0.0.1:3000"),
        lambda mp: mp.setattr(update, "_editable", lambda: True),
    ],
    ids=["off", "local-server", "editable"],
)
def test_version_leaves_out_an_update_that_would_not_run(
    site, capsys, monkeypatch, setup
):
    (update.STORE / "0.2.1").mkdir(parents=True)
    (update.STORE / "0.2.1" / update.READY).write_text("x")
    setup(monkeypatch)
    assert _version_line(capsys) == "packagetrackdev-mcp 0.2.0\n"


def test_version_leaves_out_a_release_marked_bad(site, capsys):
    (update.STORE / "0.2.1").mkdir(parents=True)
    (update.STORE / "0.2.1" / update.READY).write_text("x")
    (update.STORE / "0.2.1" / update.BAD).write_text("ImportError")
    assert _version_line(capsys) == "packagetrackdev-mcp 0.2.0\n"


def test_version_reads_the_server_flag(site, capsys):
    (update.STORE / "0.2.1").mkdir(parents=True)
    (update.STORE / "0.2.1" / update.READY).write_text("x")
    line = _version_line(capsys, "--server", "http://127.0.0.1:8000")
    assert line == "packagetrackdev-mcp 0.2.0\n"


def test_two_servers_unpacking_the_same_release_share_one_directory(site):
    release = update.Release("0.2.1", "unused", hashlib.sha256(site.data).hexdigest())
    first = update.unpack(site.data, release, threading.Event())
    second = update.unpack(site.data, release, threading.Event())
    assert first == second == update.STORE / "0.2.1"
    assert _store_entries() == ["0.2.1"]


def test_old_releases_are_removed_and_the_newest_two_kept(site):
    for version in ("0.1.9", "0.2.1", "0.2.2", "0.2.3"):
        (update.STORE / version).mkdir(parents=True)
        (update.STORE / version / update.READY).write_text("x")
    update._tidy("0.2.0")
    assert _store_entries() == ["0.2.2", "0.2.3"]
    update._tidy("0.2.3")
    assert _store_entries() == ["0.2.3"]


def test_a_release_older_than_the_installed_one_is_never_handed_over_to(
    site, monkeypatch
):
    assert update.newer_main(DEFAULT) is not None
    for name in [n for n in sys.modules if update._ours(n)]:
        del sys.modules[name]
    os.environ.pop(update.ENV_UPDATED)
    monkeypatch.setattr(update, "installed_version", lambda: "0.3.0")
    site.document = document("0.2.1", site.data)
    assert update.newer_main(DEFAULT) is None
    assert _store_entries() == []


@pytest.mark.parametrize(
    ("version", "specifier", "ok"),
    [
        ("2.2.0", ">=2,<3", True),
        ("3.0", ">=2,<3", False),
        ("0.28.1", ">=0.28", True),
        ("0.27.9", ">=0.28", False),
        ("2.13.5rc1", ">=2.13", True),
        ("3.11.4", ">=3.11", True),
        ("1.0", "", True),
    ],
)
def test_satisfies(version, specifier, ok):
    assert update.satisfies(version, specifier) is ok


def test_the_entry_point_names_the_same_command_and_version_as_the_server():
    assert entry.COMMAND == server.COMMAND
    assert update.installed_version() == server._version()


def test_nothing_before_the_server_prints_to_stdout():
    """stdout carries JSON-RPC; one stray line and the client drops the server."""
    import ast

    for module in (update, entry):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        exempt = {
            id(node)
            for top in tree.body
            if isinstance(top, ast.ClassDef) and top.name == "_Version"
            for node in ast.walk(top)
        }
        for node in ast.walk(tree):
            if id(node) in exempt:
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                targets = [k.value for k in node.keywords if k.arg == "file"]
                assert targets and ast.unparse(targets[0]) == "sys.stderr", (
                    f"{module.__name__}:{node.lineno}"
                )
            assert not (isinstance(node, ast.Attribute) and node.attr == "stdout"), (
                f"{module.__name__}:{node.lineno}"
            )


def test_the_arguments_parse_without_importing_the_server():
    script = (
        "import sys; from packagetrackdev_mcp import __main__ as m; "
        "m.build_parser().parse_args([]); "
        "print('packagetrackdev_mcp.server' in sys.modules, 'mcp' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert out.split() == ["False", "False"]


BOOTSTRAP = """
import sys
from pathlib import Path
import httpx
from packagetrackdev_mcp import __main__ as entry, update

wheel = Path(sys.argv[1]).read_bytes()
answer = Path(sys.argv[2]).read_text()
mode = sys.argv[3]
update.STORE = Path(sys.argv[4])
update.installed_version = lambda: "0.2.0"
update._editable = lambda: False

def handler(request):
    if mode == "offline":
        raise httpx.ConnectError("offline")
    if request.url.path.endswith("versions.json"):
        return httpx.Response(200, text=answer)
    return httpx.Response(200, content=wheel)

update._client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
sys.argv = ["packagetrackdev-mcp"]
sys.exit(entry.main())
"""

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


HANDED_OVER = "9.9.9"


def _start(tmp_path: Path, mode: str) -> tuple[list[dict], str]:
    data = make_wheel(HANDED_OVER, package_dir=PACKAGE_DIR)
    (tmp_path / "wheel.whl").write_bytes(data)
    (tmp_path / "versions.json").write_text(json.dumps(document(HANDED_OVER, data)))
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PACKAGETRACK_")
    }
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            BOOTSTRAP,
            str(tmp_path / "wheel.whl"),
            str(tmp_path / "versions.json"),
            mode,
            str(tmp_path / "store"),
        ],
        input=(json.dumps(INITIALIZE) + "\n").encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    lines = process.stdout.decode("utf-8").splitlines()
    messages = [json.loads(line) for line in lines]
    return messages, process.stderr.decode("utf-8", errors="replace")


def _server_info(messages: list[dict]) -> dict:
    (answer,) = [m for m in messages if m.get("id") == 1]
    return answer["result"]["serverInfo"]


def test_the_real_server_hands_over_and_answers_as_the_new_release(tmp_path):
    messages, err = _start(tmp_path, "online")
    assert _server_info(messages)["version"] == HANDED_OVER, err
    assert f"updated 0.2.0 -> {HANDED_OVER}" in err


def test_the_real_server_starts_offline_with_the_installed_release(tmp_path):
    messages, err = _start(tmp_path, "offline")
    assert _server_info(messages)["name"] == server.SERVER_NAME, err
    assert _server_info(messages)["version"] != HANDED_OVER
    assert "could not reach https://packagetrack.dev" in err
