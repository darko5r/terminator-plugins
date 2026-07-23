# Terminator Full Select

An unofficial Terminator plugin that adds a visual full-scrollback selection
mode and reliable copying of the complete retained terminal buffer through
Terminator's existing **Copy** context-menu item.

## Status and compatibility

`0.2.0-rc1` is a pre-release candidate. No stable release has been published.

The current version has been tested with:

- Arch Linux on x86-64 with glibc
- KDE Plasma on Wayland
- Terminator 2.1.5
- GTK 3
- VTE 0.84.0 / API 2.91
- Python 3.14.6

The supplied native library is an ELF x86-64 glibc binary. Its versioned libc
requirements do not exceed `GLIBC_2.14`. Other Linux x86-64 glibc systems,
Terminator versions, and desktop environments may work, but have not yet been
verified.

## How it works

VTE does not expose a native selection covering the complete retained
scrollback in the way required by this plugin. Terminator Full Select instead:

1. Draws a visual overlay without changing VTE's native selection state.
2. Keeps that overlay synchronized with output, scrolling, and resizing.
3. Extracts and normalizes the retained VTE buffer in Python.
4. Uses an authenticated native engine only for guarded numeric drawing
   geometry. Python remains authoritative for text extraction and semantic
   classification.
5. Prepares an invalidation-aware copy snapshot and atomically commits the
   verified text to the clipboard when **Copy** is activated.

The native engine is accepted only when its exact SHA-256, ABI versions,
structure sizes, feature flags, and build identifier match the Python plugin's
embedded contract. A mismatch or runtime failure falls back to Python for the
same frame and disables the native fast path only for the affected pane.

Transactional Copy uses the same retained-buffer extraction as the proven
fallback path. Changed terminal content invalidates prepared snapshots;
same-click extraction and a bounded retry keep the clipboard commit current.
A failed transaction preserves the previous clipboard contents.

## Features

- Full retained-scrollback copy through Terminator's native **Copy** menu item.
- Prepared transactional Copy enabled by default.
- Authenticated native numeric drawing fast path enabled by default.
- Exact same-frame Python fallbacks and independent per-pane safety latches.
- Visual selection toggle with `Ctrl+Shift+Alt+S`.
- **Escape** clears Full Select mode only while it is active.
- Context-menu actions for showing and clearing the visual selection.
- A short, single-pass confirmation badge after a successful copy.
- Content-fitted row highlighting.
- Violet semantic highlighting for detected shell commands.
- Separate highlighting for line-number prefixes.
- Lowest-visible-row recovery during resize and repaint.
- Live refresh during terminal output, scrolling, typing, and resizing.
- Unicode-aware terminal cell-width calculations.
- Complete-buffer normalization and surrounding empty-row cleanup.
- Optional row-coordinate diagnostics for troubleshooting.

## Installation for users

The prebuilt binary package is the recommended installation method. It
contains the Python plugin, the authenticated native `.so`, an installer, an
uninstaller, and verification metadata. It intentionally does not contain the
C source files.

Requirements:

- Linux x86-64 with glibc
- Terminator with GTK 3 and VTE API 2.91
- Python 3
- `tar` and `sha256sum`
- `curl` for the command-line download example, or a web browser

Download the archive and `SHA256SUMS` from the
[`terminator-full-select-v0.2.0-rc1` pre-release](https://github.com/darko5r/terminator-plugins/releases/tag/terminator-full-select-v0.2.0-rc1).
The following commands perform the same download in a new directory:

```bash
mkdir -p terminator-full-select-install
cd terminator-full-select-install

tag=terminator-full-select-v0.2.0-rc1
archive=terminator-full-select-0.2.0-rc1-linux-x86_64-glibc.tar.gz
base=https://github.com/darko5r/terminator-plugins/releases/download

curl -fLO "$base/$tag/$archive"
curl -fLO "$base/$tag/SHA256SUMS"
sha256sum -c SHA256SUMS

tar -xzf "$archive"
cd "${archive%.tar.gz}"
./install.sh
```

Do not run the installer with `sudo`. It verifies every package file and the
native ABI contract, backs up an existing installation, and atomically installs
only into the current user's Terminator plugin directory.

Fully close and reopen Terminator. Open **Preferences**, select **Plugins**,
and enable:

```text
FullSelectVisualPrototype
```

### Uninstall

The installer prints the persistent uninstaller path. With the default XDG
locations, run:

```bash
"${XDG_STATE_HOME:-$HOME/.local/state}/terminator-full-select/uninstall.sh"
```

The uninstaller backs up the installed files before removal and leaves
unrelated files alone. It refuses to remove a plugin or native library that
was modified after installation. Use `--force` only after reviewing those
files and deciding that removal is intended.

## Installation for developers

The source tree contains the Python plugin, native C and header files, the
Meson build definition, packaging tools, and tests. It intentionally does not
store a compiled `.so`.

Clone and verify the source:

```bash
git clone https://github.com/darko5r/terminator-plugins.git
cd terminator-plugins/terminator-full-select
sha256sum -c SHA256SUMS
```

Install distribution packages that provide:

- Terminator, GTK 3, VTE API 2.91, and PyGObject
- Python 3
- Meson and Ninja
- a C17 compiler and linker
- GNU `sha256sum` and standard Unix installation tools

Then run:

```bash
./scripts/install-developer.sh
```

The developer installer builds the native engine in a temporary directory,
verifies the result against the exact authenticated contract embedded in the
plugin, creates a package-shaped staging tree, and invokes the same user
installer.

The current security policy requires a byte-identical native binary, not just
ABI compatibility. A different compiler, linker, or build environment may
produce a safe but non-identical binary that is deliberately rejected. Most
users should therefore install the reviewed prebuilt package. Changing the
native contract belongs in a separate reviewed native-development workflow.

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

The plugin adds:

- **Full Select: show visual selection**
- **Full Select: clear visual selection**
- **Full Select: write row-coordinate report**

The normal **Copy** item is enabled and extended only while Full Select mode is
active. When the mode is inactive, Terminator's standard Copy behavior is
unchanged.

The full-scrollback handler is connected to the right-click **Copy** item.
Terminator's keyboard Copy shortcut is not guaranteed to invoke the same
full-scrollback path.

"Complete scrollback" means the content still retained by VTE. Content already
discarded because of the terminal profile's scrollback limit cannot be copied.

## Runtime fallback switches

These process-local switches do not change installed files:

```bash
TFSE_DISABLE_NATIVE_FAST_PATH=1 terminator
TFSE_DISABLE_TRANSACTIONAL_COPY=1 terminator
```

The first restores Python numeric drawing. The second restores the prior exact
Copy path. Fully close all Terminator processes before launching with a switch.

## Privacy and diagnostics

The plugin runtime works locally. It does not use the network, launch
subprocesses, or contain embedded credentials. It loads the authenticated
native library beside the Python plugin with `ctypes`.

Copying the complete scrollback can place commands, output, paths, tokens, and
other sensitive terminal content on the system clipboard.

Runtime diagnostics may be written to:

```text
/tmp/terminator-fullselect-overlay.txt
/tmp/terminator-fullselect-row-coordinate-probe.txt
/tmp/terminator-fullselect-wrap-edge-probe.txt
/tmp/terminator-fullselect-resize-event-trace.txt
```

The row-coordinate report can include extracted terminal lines. Do not
generate or share diagnostics when the terminal contains sensitive
information. These files inherit permissions from the Terminator process and
its `umask`, which matters on shared systems.

Remove them after troubleshooting:

```bash
rm -f \
  /tmp/terminator-fullselect-overlay.txt \
  /tmp/terminator-fullselect-row-coordinate-probe.txt \
  /tmp/terminator-fullselect-wrap-edge-probe.txt \
  /tmp/terminator-fullselect-resize-event-trace.txt
```

## Verification and packaging

`SHA256SUMS` is the authoritative manifest for the source project:

```bash
sha256sum -c SHA256SUMS
```

Maintainers update it only after reviewing the complete source diff:

```bash
./scripts/update-source-checksums.sh
sha256sum -c SHA256SUMS
```

Run the packaging and safety suite with:

```bash
./tests/test-packaging.sh
```

After committing the reviewed release source, build a reproducible user
archive from a clean tree:

```bash
./scripts/build-binary-package.sh
```

The archive and its release checksum are written beneath `dist/`, which is
ignored by Git. The builder rejects a native binary stored in the source tree,
and the binary package rejects C source, headers, and `meson.build`.

## Known limitations

- This is a pre-release candidate with one confirmed test environment.
- The initial prebuilt package supports only Linux x86-64 with glibc.
- A small flicker can still appear during continuous active resizing while
  the mouse button remains held. Rendering stabilizes when motion stops.
- The visual overlay simulates full selection; it is not a native VTE
  selection.
- Full-scrollback copying depends on locating Terminator's native right-click
  **Copy** item.
- Shell-command recognition is heuristic and may vary with prompts, themes,
  shells, and color configurations.
- Diagnostic files do not yet enforce private `0600` permissions themselves.
- The source filename and plugin class retain the word `Prototype`.

## Release history

- `0.2.0-rc1` adds the authenticated native numeric drawing path, guarded
  fallbacks, default transactional Copy, reproducible packaging, and separate
  user/developer installation paths.
- [`v0.1.0-rc1`](https://github.com/darko5r/terminator-plugins/releases/tag/v0.1.0-rc1)
  is a historical pre-release candidate. It is not a stable or full release.

## License

Terminator Full Select is available under the [MIT License](LICENSE).

This project is not part of, or endorsed by, the Terminator project.
