# Terminator Full Select

An unofficial Terminator plugin that provides a visual full-scrollback
selection mode and copies the complete retained terminal buffer through
Terminator's native **Copy** context-menu item.

## Status

`v0.1.0-rc1` is a release candidate.

The current version has been tested with:

- Terminator 2.1.5
- GTK 3
- VTE 0.84.0 / API 2.91
- Python 3
- Arch Linux with KDE Plasma on Wayland

Other versions and desktop environments may work, but have not yet been
verified.

## How it works

VTE does not expose a native selection covering the complete retained
scrollback in the way required by this plugin.

Terminator Full Select therefore:

1. Draws a visual selection overlay over terminal content.
2. Keeps the overlay synchronized while the terminal changes or scrolls.
3. Integrates with Terminator's existing right-click **Copy** menu item.
4. Extracts the complete retained terminal buffer when **Copy** is activated
   while Full Select mode is active.
5. Places the normalized Unicode text on the system clipboard.

The plugin does not modify VTE's native selection state.

## Features

- Full-scrollback copy through Terminator's native **Copy** menu item.
- Visual selection toggle with `Ctrl+Shift+Alt+S`.
- **Escape** clears Full Select mode only while it is active.
- Context-menu actions for showing and clearing the visual selection.
- Content-fitted row highlighting.
- Semantic highlighting for detected shell commands.
- Separate highlighting for line-number prefixes.
- Live refresh during terminal output, scrolling, typing, and resizing.
- Unicode-aware terminal cell-width calculations.
- Complete-buffer normalization and surrounding empty-row cleanup.
- Optional row-coordinate diagnostics for troubleshooting.

## Installation

Clone the repository:

```bash
git clone https://github.com/darko5r/terminator-full-select.git
cd terminator-full-select
```

Install the plugin for the current user:

```bash
install -Dm644 \
  full_select_visual_prototype.py \
  "$HOME/.config/terminator/plugins/full_select_visual_prototype.py"
```

Fully close and reopen Terminator. Then open Terminator's preferences, select
the **Plugins** section, and enable:

```text
FullSelectVisualPrototype
```

## Usage

### Keyboard

1. Focus the desired terminal pane.
2. Press `Ctrl+Shift+Alt+S` to activate Full Select mode.
3. Right-click the terminal and choose Terminator's existing **Copy** item.
4. Press **Escape**, or press `Ctrl+Shift+Alt+S` again, to clear the visual
   selection.

The plugin consumes **Escape** only while Full Select mode is active. Other
keyboard input continues to the terminal normally.

### Context menu

The plugin adds these entries:

- **Full Select: show visual selection**
- **Full Select: clear visual selection**
- **Full Select: write row-coordinate report**

The normal **Copy** item is enabled and extended while Full Select mode is
active. When the mode is inactive, Terminator's standard Copy behavior is
unchanged.

The complete-scrollback handler is currently connected to the right-click
**Copy** menu item. Do not assume that Terminator's keyboard Copy shortcut
invokes the same full-scrollback behavior.

## Privacy and diagnostics

The plugin performs its work locally. The reviewed `v0.1.0-rc1` source does
not contain networking, subprocess execution, native-code loading, or embedded
credentials.

Be aware of the following:

- Copying the complete scrollback can place commands, output, paths, tokens, or
  other sensitive terminal content on the system clipboard.
- Runtime diagnostic information is written to:

  ```text
  /tmp/terminator-fullselect-overlay.txt
  ```

- Choosing **Full Select: write row-coordinate report** writes:

  ```text
  /tmp/terminator-fullselect-row-coordinate-probe.txt
  ```

- The row-coordinate report can contain extracted terminal lines. Do not
  generate or share it when the terminal contains sensitive information.
- Diagnostic files use the permissions produced by the Terminator process
  and its current `umask`. This matters particularly on shared systems.

The diagnostic files can be removed after troubleshooting:

```bash
rm -f \
  /tmp/terminator-fullselect-overlay.txt \
  /tmp/terminator-fullselect-row-coordinate-probe.txt
```

## Known limitations

- This is a release candidate and currently has one confirmed test
  environment.
- The visual overlay simulates full selection; it is not a native VTE
  selection.
- Full-scrollback copying currently depends on locating Terminator's native
  right-click **Copy** menu item.
- Shell-command recognition is heuristic and may vary with prompts, themes,
  shells, and color configurations.
- Diagnostic files do not yet enforce private `0600` permissions themselves.
- The source filename and plugin class retain the word `Prototype`.

## Frozen release-candidate artifact

The plugin file in `v0.1.0-rc1` is preserved byte-for-byte from the tested
version.

SHA-256:

```text
63359b56e585244c87c77aae24a9fb7ec79a470574f0c535c057922cd76f54fb  full_select_visual_prototype.py
```

Verify it with:

```bash
sha256sum -c SHA256SUMS
```

The opening module docstring describes an earlier visual-only development
stage and is outdated. This README describes the actual behavior of the
frozen release-candidate artifact. The source docstring can be corrected in a
later release candidate without changing this preserved version.

## License

Terminator Full Select is available under the [MIT License](LICENSE).

This project is not part of, or endorsed by, the Terminator project.
