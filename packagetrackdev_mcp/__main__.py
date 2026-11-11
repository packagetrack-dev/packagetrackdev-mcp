"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from packagetrackdev_mcp import config, update

COMMAND = "packagetrackdev-mcp"


class _Version(argparse.Action):
    """Print the installed version, and the updated one that runs, then exit."""


    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(
            option_strings,
            dest,
            nargs=0,
            default=argparse.SUPPRESS,
            help="show the version and exit",
        )

    def __call__(self, parser, namespace, values, option_string=None):
        settings = config.load(server=getattr(namespace, "server", None))
        sys.stdout.write(f"{COMMAND} {update.describe_version(settings)}\n")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=(
            "Serve the PackageTrack archive to a coding agent over the Model "
            "Context Protocol (stdio)."
        ),
        epilog=(
            "At start the server updates itself to the newest release from "
            f"packagetrack.dev; {update.ENV_OFF}=1 turns that off. "
            f"`{COMMAND} doctor --running X.Y.Z` explains a note about "
            "versions at the end of a reply."
        ),
    )
    parser.add_argument("--version", action=_Version)
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
    if sys.argv[1:2] == ["doctor"]:
        from packagetrackdev_mcp import doctor

        return doctor.main()
    args = build_parser().parse_args()
    settings = config.load(api_key=args.api_key, server=args.server)
    newer = update.newer_main(settings)
    if newer is not None:
        return newer()

    from packagetrackdev_mcp.server import serve

    return serve(settings)


if __name__ == "__main__":
    sys.exit(main())
