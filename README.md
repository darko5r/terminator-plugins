# Terminator Plugins

A collection of plugins and enhancements for the Terminator terminal emulator.

## Available plugins

### Terminator Full Select

Full-scrollback selection and copy support for Terminator.

- [Project files and documentation](terminator-full-select/)
- Latest published pre-release: [`v0.1.0-rc1`](https://github.com/darko5r/terminator-plugins/releases/tag/v0.1.0-rc1)
- Status: release candidate; no stable release has been published yet.

Installation, usage, compatibility information, checksums, and known
limitations are documented in the plugin README.

## Repository layout

- `terminator-full-select/` — Terminator Full Select project directory
  - `full_select_visual_prototype.py` — plugin source
  - `README.md` — project documentation
  - `SHA256SUMS` — release artifact checksum
  - `LICENSE` — project license copy
  - `.gitignore` — project-specific ignore rules

## Releases

Releases are versioned independently for each plugin.

The existing `v0.1.0-rc1` tag is a historical pre-release candidate for
Terminator Full Select. It is not a stable or full release.

Future releases will use plugin-specific tag names, such as
`terminator-full-select-vX.Y.Z`.

## License

This repository is licensed under the MIT License. Individual plugin
directories may also contain a license copy for standalone use.
