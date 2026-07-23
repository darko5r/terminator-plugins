#!/usr/bin/env bash

set -euo pipefail

umask 022

fail() {
    printf 'install-developer.sh: ERROR: %s\n' "$*" >&2
    exit 1
}

script_dir="$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
        pwd -P
)"
project_dir="$(dirname -- "$script_dir")"
native_source_dir="$project_dir/full_select_native"

for required_command in \
    dirname \
    find \
    install \
    meson \
    mktemp \
    python3 \
    rm \
    sha256sum \
    sort \
    xargs
do
    command -v "$required_command" >/dev/null 2>&1 ||
        fail "Required command not found: $required_command"
done

workspace="$(mktemp -d /tmp/terminator-full-select-dev-install.XXXXXX)"
cleanup() {
    rm -rf -- "$workspace"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

build_dir="$workspace/build"
package_dir="$workspace/package"

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
    --binary "$rebuilt_binary" ||
    fail "The rebuilt binary does not match the authenticated plugin contract. Update the contract only through the reviewed native-development workflow."

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

(
    cd "$package_dir"
    find . -type f ! -name SHA256SUMS -print0 |
        LC_ALL=C sort -z |
        xargs -0 sha256sum > SHA256SUMS
    sha256sum -c SHA256SUMS
)

"$package_dir/install.sh"
