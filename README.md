# Terminator Plugins

A collection of plugins and enhancements for the Terminator terminal emulator.

## Available plugins

### Terminator Full Select

Full-scrollback selection and copy support for Terminator.

- [Project files and documentation](terminator-full-select/)
- Current pre-release: [`0.2.0-rc1`](https://github.com/darko5r/terminator-plugins/releases/tag/terminator-full-select-v0.2.0-rc1)
- Prebuilt user package: Linux x86-64 with glibc
- Status: release candidate; no stable release has been published.

Installation, usage, compatibility information, checksums, and known
limitations are documented in the plugin README.

## Repository layout

- `terminator-full-select/` — Terminator Full Select project directory
  - `VERSION` — current project version
  - `full_select_visual_prototype.py` — plugin source
  - `full_select_native/` — reviewed native engine source and build definition
  - `packaging/` — binary-package installer and uninstaller
  - `scripts/` — verification, build, checksum, and developer tools
  - `tests/` — packaging and safety tests
  - `README.md` — project documentation
  - `SHA256SUMS` — authoritative source-tree checksums
  - `LICENSE` — project license copy
  - `.gitignore` — project-specific ignore rules

Generated archives and native build outputs are intentionally excluded from
the source tree.

## Releases

Releases are versioned independently for each plugin.

The `v0.1.0-rc1` tag is a historical pre-release candidate for
Terminator Full Select. It is not a stable or full release.

Current and future releases use plugin-specific tag names, such as
`terminator-full-select-v0.2.0-rc1`.

## License

This repository is licensed under the MIT License. Individual plugin
directories may also contain a license copy for standalone use.
