#!/usr/bin/env bash

set -euo pipefail

umask 022

fail() {
    printf 'build-binary-package.sh: ERROR: %s\n' "$*" >&2
    exit 1
}

allow_dirty=0
case "${1:-}" in
    "")
        ;;
    --allow-dirty)
        allow_dirty=1
        ;;
    *)
        fail "Usage: $0 [--allow-dirty]"
        ;;
esac

script_dir="$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
        pwd -P
)"
project_dir="$(dirname -- "$script_dir")"
native_source_dir="$project_dir/full_select_native"
dist_dir="$project_dir/dist"

for required_command in \
    dirname \
    find \
    git \
    install \
    meson \
    mktemp \
    mv \
    python3 \
    rm \
    sed \
    sha256sum \
    sort \
    tar \
    uname \
    xargs
do
    command -v "$required_command" >/dev/null 2>&1 ||
        fail "Required command not found: $required_command"
done

source_commit="$(git -C "$project_dir" rev-parse HEAD)"
if [[ -n "$(git -C "$project_dir" status --porcelain)" ]]; then
    [[ "$allow_dirty" -eq 1 ]] ||
        fail "The repository is not clean. Commit reviewed source changes before building a release package."
    source_tree_clean=false
else
    source_tree_clean=true
fi

version="$(sed -n '1p' "$project_dir/VERSION")"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-rc[0-9]+$ ]] ||
    fail "VERSION must use the form X.Y.Z-rcN."

case "$(uname -m)" in
    x86_64 | amd64)
        ;;
    *)
        fail "The initial binary package target is Linux x86-64."
        ;;
esac

package_name="terminator-full-select-${version}-linux-x86_64-glibc"
archive_name="${package_name}.tar.gz"
archive_path="$dist_dir/$archive_name"
archive_manifest="$dist_dir/SHA256SUMS"
source_tree_binary="$native_source_dir/libterminator_full_select_engine.so"

if [[ -e "$source_tree_binary" || -L "$source_tree_binary" ]]; then
    fail "The native binary must not be stored in the source tree: $source_tree_binary"
fi

workspace="$(mktemp -d /tmp/terminator-full-select-package.XXXXXX)"
cleanup() {
    rm -rf -- "$workspace"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

build_dir="$workspace/build"
package_dir="$workspace/$package_name"

meson setup \
    --buildtype=release \
    "$build_dir" \
    "$native_source_dir"
meson compile -C "$build_dir"

rebuilt_binary="$build_dir/libterminator_full_select_engine.so"
[[ -f "$rebuilt_binary" ]] ||
    fail "Native build did not produce the expected shared library."

python3 "$project_dir/scripts/verify-native-binary.py" \
    --plugin "$project_dir/full_select_visual_prototype.py" \
    --binary "$rebuilt_binary"

install -Dm644 -- \
    "$project_dir/full_select_visual_prototype.py" \
    "$package_dir/full_select_visual_prototype.py"
install -Dm644 -- \
    "$rebuilt_binary" \
    "$package_dir/full_select_native/libterminator_full_select_engine.so"
install -Dm755 -- \
    "$project_dir/packaging/install.sh" \
    "$package_dir/install.sh"
install -Dm755 -- \
    "$project_dir/packaging/uninstall.sh" \
    "$package_dir/uninstall.sh"
install -Dm755 -- \
    "$project_dir/scripts/verify-native-binary.py" \
    "$package_dir/tools/verify-native-binary.py"
install -Dm644 -- \
    "$project_dir/README.md" \
    "$package_dir/README.md"
install -Dm644 -- \
    "$project_dir/LICENSE" \
    "$package_dir/LICENSE"
install -Dm644 -- \
    "$project_dir/VERSION" \
    "$package_dir/VERSION"

unexpected_source="$(
    find "$package_dir" -type f \
        \( -name '*.c' -o -name '*.h' -o -name meson.build \) \
        -print -quit
)"
if [[ -n "$unexpected_source" ]]; then
    fail "Binary package unexpectedly contains native source files."
fi

(
    cd "$package_dir"
    find . -type f ! -name SHA256SUMS -print0 |
        LC_ALL=C sort -z |
        xargs -0 sha256sum > SHA256SUMS
    sha256sum -c SHA256SUMS
)

install -d -m 0755 -- "$dist_dir"

source_date_epoch="$(
    git -C "$project_dir" log -1 --format='%ct'
)"

tar \
    --sort=name \
    --mtime="@${source_date_epoch}" \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    -C "$workspace" \
    -czf "$archive_path" \
    "$package_name"

(
    cd "$dist_dir"
    sha256sum "$archive_name" > SHA256SUMS
    sha256sum -c SHA256SUMS
)

printf 'binary_package=%s\n' "$archive_path"
printf 'binary_package_manifest=%s\n' "$archive_manifest"
printf 'binary_package_version=%s\n' "$version"
printf 'binary_package_platform=linux-x86_64-glibc\n'
printf 'binary_package_native_sources=False\n'
printf 'binary_package_source_commit=%s\n' "$source_commit"
printf 'binary_package_source_tree_clean=%s\n' "$source_tree_clean"
