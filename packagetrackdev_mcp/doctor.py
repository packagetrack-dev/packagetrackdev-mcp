"""Say why the running server and the releases on this computer differ."""

from __future__ import annotations

import sys
from typing import TextIO

from packagetrackdev_mcp import config, update

BAD_ARGS = (
    "doctor takes --running X.Y.Z and --latest X.Y.Z, both optional, each a "
    "version like 0.2.0; nothing was checked."
)


def _parse(argv: list[str]) -> tuple[str | None, str | None] | None:
    found: dict[str, str] = {}
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag not in ("--running", "--latest") or flag in found:
            return None
        if i + 1 >= len(argv):
            return None
        value = update.strict_release(argv[i + 1])
        if value is None:
            return None
        found[flag] = value
        i += 2
    return found.get("--running"), found.get("--latest")


def run(argv: list[str], out: TextIO) -> int:
    """Run doctor with `argv` (after the word doctor); return its exit code."""
    parsed = _parse(argv)
    if parsed is None:
        out.write(BAD_ARGS + "\n")
        return 2
    running, latest = parsed
    installed = update.installed_version()
    off = update.inactive_text(config.load(), installed)
    if off is not None:
        out.write(off + "\n")
        return 0
    found = update.diagnose(running or installed, latest)
    if running is None and found.kind in ("ok", "newer", "gone"):
        out.write(
            f"{update.COMMAND} {found.active} is the newest version on this "
            "computer. A server that was already running when it arrived keeps "
            f"its older version until it restarts: {update.RESTART[0].lower()}"
            f"{update.RESTART[1:]}.\n"
        )
        return 0
    code, text = update.describe(found)
    out.write(text + "\n")
    return code


def main() -> int:
    return run(sys.argv[2:], sys.stdout)
