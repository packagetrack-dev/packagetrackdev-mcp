# Changelog

All notable changes to this project are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] - 2026-11-11

### Added

- `import_project` reads `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`,
  `Pipfile.lock`, `pubspec.lock`, `packages.lock.json`, `gradle.lockfile`
  and `pom.xml`, on your machine like the other lock files.
- `bun.lockb` is refused with the command that writes the text `bun.lock`.

## [0.2.1] - 2026-11-02

### Added

- The server updates itself when it starts: it takes the newest release it can check (SHA256) and run here, and keeps the installed one otherwise.
- When a newer release is waiting for a restart, tool replies end with a short note.
- `update_status` tool and `packagetrackdev-mcp doctor`: say what to do when an update is waiting or could not start, reading local files only.
- `--version` shows the installed and the running release.

### Changed

- A release folder another user owns or can write to is skipped.

## [0.2.0] - 2026-10-20

### Added

- `import_project`: push a project from the agent. It reads a lock file on your machine (`file_path`) or takes names and versions, and sends only those. A new name makes a new project; an existing name is updated. Needs a PackageTrack API key.

### Fixed

- `package_changes` and `project_report` handle deprecated packages, downgrade and prerelease bounds, a bound that is not a version, and an archive that is behind the registry.
- Counts read "1 package", and packages behind through a parent are counted.

## [0.1.1] - 2026-09-15

### Fixed

- The documented `uv tool install` line now works: it uses `--find-links https://packagetrack.dev/cli/`.
  A `...-latest.whl` URL is rejected by uv before the request is made.

## [0.1.0] - 2026-09-14

### Added

- First release as a separate package. Previously shipped as `packagetrackdev mcp`.
- Tools: `package_changes`, `package_versions`, `search_packages`, `list_projects`, `project_report`.

[Unreleased]: https://github.com/packagetrack-dev/packagetrackdev-mcp/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/packagetrack-dev/packagetrackdev-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/packagetrack-dev/packagetrackdev-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/packagetrack-dev/packagetrackdev-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/packagetrack-dev/packagetrackdev-mcp/releases/tag/v0.1.0
