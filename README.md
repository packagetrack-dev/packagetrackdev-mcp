# packagetrackdev-mcp

[![CI](https://github.com/packagetrack-dev/packagetrackdev-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/packagetrack-dev/packagetrackdev-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Model Context Protocol](https://modelcontextprotocol.io) server for
[PackageTrack](https://packagetrack.dev). Gives coding agents the release
history of packages on PyPI, npm, crates.io, Packagist, NuGet, pub.dev, Go
and Maven Central: what changed between two versions, which versions exist,
which have been withdrawn.

## Install

macOS and Linux:

```sh
curl -fsSL https://packagetrack.dev/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://packagetrack.dev/install.ps1 | iex
```

Installs `uv` if needed, then this server and the
[packagetrackdev](https://github.com/packagetrack-dev/packagetrackdev) CLI
as `uv` tools, each checked against its published SHA256 first. On Windows,
close the coding agent that runs this server before installing over it:
Windows keeps a running program's files locked. To reinstall or update, run
the same command again. The checked wheels stay in `~/.packagetrack/wheels`:
`uv` records the file it installed from, and `uv tool upgrade --all` reads it
again.

Limits: the checksum covers this package's own wheel. Its dependencies (the
MCP SDK and what it needs) are downloaded by `uv` from PyPI and rest on
PyPI's own checks, not on ours.

Where scripts are blocked, or to install only the server, follow
[the steps by hand](https://packagetrack.dev/install/manual): download the
file, check its SHA256 yourself, install it with `uv`.

Requires Python 3.11+.

## Configuration

Claude Code:

```sh
claude mcp add -s user packagetrackdev -- packagetrackdev-mcp
```

Claude Desktop (`claude_desktop_config.json`), Cursor, Windsurf and other
clients with an `mcpServers` map:

```json
{
  "mcpServers": {
    "packagetrackdev": {
      "command": "packagetrackdev-mcp"
    }
  }
}
```

VS Code (`mcp.json`) uses `servers` instead of `mcpServers`.

Optional environment variables:

| Variable | Description |
|---|---|
| `PACKAGETRACK_API_KEY` | Enables the project tools. Create a key at [packagetrack.dev/app/keys](https://packagetrack.dev/app/keys). Also read from `~/.packagetrack/config.toml`, written by `packagetrackdev login`. |
| `PACKAGETRACK_SERVER` | Server URL. Default: `https://packagetrack.dev`. |
| `PACKAGETRACK_NO_AUTO_UPDATE` | Set to `1` to turn off updates at start. |

## Tools

| Tool | Description | Key |
|---|---|---|
| `package_changes` | Release notes for every version between `since` and `until`, oldest first. Long ranges are paged. | no |
| `package_versions` | Summary, source repository, latest stable version and recent releases with prerelease and withdrawn flags. | no |
| `search_packages` | Search the archive by name. | no |
| `list_projects` | Projects pushed with `packagetrackdev push` and how far behind each is. | yes |
| `project_report` | Withdrawn versions and outdated dependencies in one project, with the notable release-note lines. | yes |
| `import_project` | Push a project: reads a lock file on your machine (`file_path`) or takes names and versions. A new name makes a new project; an existing one is updated. | yes |

A package the archive does not hold yet is fetched on request; ask again
after a minute.

Example prompt:

```
Upgrade httpx to the newest release and tell me what changed since 0.24.1
```

Agents do not always reach for tools on their own. Adding one line to
`CLAUDE.md` or `.cursorrules` helps:

```
Before upgrading a dependency, call packagetrack's package_changes with the installed and target versions.
```

## Updates

The server updates itself when it starts. Before any tool runs it asks
`https://packagetrack.dev/cli/versions.json` for the newest release. If that
release is newer, the server downloads it, checks its SHA256, unpacks it
under `~/.packagetrack/mcp/` and runs it. Nothing installed is overwritten,
so this works the same on Linux, macOS and Windows.

If the site cannot be reached within a few seconds, or the download fails,
the installed release starts. After a lost connection the server waits an
hour before asking again. A release that needs newer dependencies than the
installed ones is not taken; run the install script again to get it.

`packagetrackdev-mcp --version` shows the installed release and, when an
update is the one that runs, that one too: `0.2.0 (active: 0.2.1)`.

A server that is already running keeps its release until the coding agent
restarts it. When a newer release is waiting, or the newest one could not
start here, tool replies end with a short note. The `update_status` tool
says what to do, reading local files only; `packagetrackdev-mcp doctor`
says the same in a terminal. When only the install script can fix it,
both show the install command, for the user to run.

Updates are off for a server pointed at another `PACKAGETRACK_SERVER`, for
an editable install, and with `PACKAGETRACK_NO_AUTO_UPDATE=1`, which also
returns to the installed release.

Limits: the checksum covers this package's own wheel, and its dependencies
come from PyPI through `uv`. Releases are not signed: the checksum comes
from the same site as the wheel. Releases on this computer are trusted as
far as your user account is; on macOS and Linux a release folder that
another user owns or can write to is skipped.

## Uninstall

```sh
uv tool uninstall packagetrackdev-mcp packagetrackdev
claude mcp remove packagetrackdev
```

Then delete `~/.packagetrack/wheels` (`%USERPROFILE%\.packagetrack\wheels`
on Windows), the checked wheels `uv` installed from, and
`~/.packagetrack/mcp`, the updates the server downloaded. Uninstall first:
while a tool is installed, `uv tool upgrade --all` reads its wheel there. On
Windows, close the coding agent before uninstalling. The rest of
`~/.packagetrack` holds your settings and API key (`config.toml`); delete
the whole folder to remove everything.

## Privacy

A tool call sends package names and version strings, plus the project name
for `import_project`. A lock file is read on your machine; no code, file
contents, file paths or repository names leave it. The server stores nothing
and calls no model.

Each start also sends one request to `packagetrack.dev` for the newest
version number, unless updates are turned off.

## Debugging

Run the server under the MCP Inspector to call tools by hand:

```sh
npx @modelcontextprotocol/inspector packagetrackdev-mcp
```

## Development

```sh
uv sync
uv run pytest
```

## License

MIT
