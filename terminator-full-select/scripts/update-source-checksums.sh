#!/usr/bin/env bash

set -euo pipefail

umask 022
export LC_ALL=C

fail() {
    printf 'update-source-checksums.sh: ERROR: %s\n' "$*" >&2
    exit 1
}

script_dir="$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
        pwd -P
)"
project_dir="$(dirname -- "$script_dir")"
manifest="$project_dir/SHA256SUMS"

for required_command in \
    chmod \
    dirname \
    mktemp \
    mv \
    rm \
    sha256sum
do
    command -v "$required_command" >/dev/null 2>&1 ||
        fail "Required command not found: $required_command"
done

source_files=(
    ".gitignore"
    "LICENSE"
    "README.md"
    "VERSION"
    "full_select_native/meson.build"
    "full_select_native/tfse_engine.c"
    "full_select_native/tfse_engine.h"
    "full_select_visual_prototype.py"
    "packaging/install.sh"
    "packaging/uninstall.sh"
    "scripts/build-binary-package.sh"
    "scripts/install-developer.sh"
    "scripts/update-source-checksums.sh"
    "scripts/verify-native-binary.py"
    "tests/fail-state-mv.sh"
    "tests/test-packaging.sh"
)

for relative_path in "${source_files[@]}"; do
    target_file="$project_dir/$relative_path"
    [[ -f "$target_file" && ! -L "$target_file" ]] ||
        fail "Required source file is missing, non-regular, or symlinked: $relative_path"
done

temporary_manifest="$(mktemp "$project_dir/.SHA256SUMS.XXXXXX")"
cleanup() {
    rm -f -- "$temporary_manifest"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

(
    cd "$project_dir"
    sha256sum -- "${source_files[@]}"
) > "$temporary_manifest"

chmod 0644 "$temporary_manifest"
mv -f -- "$temporary_manifest" "$manifest"
temporary_manifest=""

(
    cd "$project_dir"
    sha256sum -c SHA256SUMS
)

printf 'source_manifest=%s\n' "$manifest"
printf 'source_manifest_file_count=%s\n' "${#source_files[@]}"
printf 'source_manifest_updated=True\n'
