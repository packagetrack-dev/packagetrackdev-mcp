"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from packagetrackdev_mcp import config
from packagetrackdev_mcp.server import COMMAND, _version, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=(
            "Serve the PackageTrack archive to a coding agent over the Model "
            "Context Protocol (stdio)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{COMMAND} {_version()}"
    )
    parser.add_argument(
        "--api-key",
        default=None,
        dest="api_key",
        help="for the project tools; default: PACKAGETRACK_API_KEY, then the "
        "file `packagetrackdev login` writes",
    )
    parser.add_argument(
        "--server",
        default=None,
        help=f"default: PACKAGETRACK_SERVER, then {config.DEFAULT_SERVER}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return serve(config.load(api_key=args.api_key, server=args.server))


if __name__ == "__main__":
    sys.exit(main())
