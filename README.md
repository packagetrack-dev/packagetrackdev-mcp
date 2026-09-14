# packagetrackdev-mcp

[![CI](https://github.com/packagetrack-dev/packagetrackdev-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/packagetrack-dev/packagetrackdev-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[Model Context Protocol](https://modelcontextprotocol.io) server for
[PackageTrack](https://packagetrack.dev). Gives coding agents the release
history of packages on PyPI, npm, crates.io, Packagist, NuGet, pub.dev, Go
and Maven Central: what changed between two versions, which versions exist,
which have been withdrawn.

## Install

```sh
curl -fsSL https://packagetrack.dev/install.sh | sh
```

Installs `uv` if needed, then this server and the
[packagetrackdev](https://github.com/packagetrack-dev/packagetrackdev) CLI
as `uv` tools. To install only the server:

```sh
uv tool install --find-links https://packagetrack.dev/cli/ packagetrackdev-mcp
```

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

## Tools

| Tool | Description | Key |
|---|---|---|
| `package_changes` | Release notes for every version between `since` and `until`, oldest first. Long ranges are paged. | no |
| `package_versions` | Summary, source repository, latest stable version and recent releases with prerelease and withdrawn flags. | no |
| `search_packages` | Search the archive by name. | no |
| `list_projects` | Projects pushed with `packagetrackdev push` and how far behind each is. | yes |
| `project_report` | Withdrawn versions and outdated dependencies in one project, with the notable release-note lines. | yes |

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

## Privacy

A tool call sends a package name and version strings. No code, file paths or
repository names leave the machine. The server stores nothing and calls no
model.

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
