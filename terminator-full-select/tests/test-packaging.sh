#!/usr/bin/env bash

set -euo pipefail

fail() {
    printf 'test-packaging.sh: ERROR: %s\n' "$*" >&2
    exit 1
}

script_dir="$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &&
        pwd -P
)"
project_dir="$(dirname -- "$script_dir")"
version="$(sed -n '1p' "$project_dir/VERSION")"
package_name="terminator-full-select-${version}-linux-x86_64-glibc"
archive_path="$project_dir/dist/${package_name}.tar.gz"

(
    cd "$project_dir"
    sha256sum -c SHA256SUMS
)

"$project_dir/scripts/build-binary-package.sh" --allow-dirty

[[ -f "$archive_path" ]] ||
    fail "Expected binary archive was not produced."

workspace="$(mktemp -d /tmp/terminator-full-select-package-test.XXXXXX)"
cleanup() {
    rm -rf -- "$workspace"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

tar -xzf "$archive_path" -C "$workspace"
package_dir="$workspace/$package_name"

[[ -d "$package_dir" ]] ||
    fail "Package root is missing after extraction."

unexpected_source="$(
    find "$package_dir" -type f \
        \( -name '*.c' -o -name '*.h' -o -name meson.build \) \
        -print -quit
)"
if [[ -n "$unexpected_source" ]]; then
    fail "Binary package contains native source files."
fi

(
    cd "$package_dir"
    sha256sum -c SHA256SUMS
)

frame_abi_mismatch_plugin="$workspace/frame-abi-mismatch.py"
sed \
    's/^NATIVE_ENGINE_FRAME_ABI_VERSION = 2$/NATIVE_ENGINE_FRAME_ABI_VERSION = 3/' \
    "$package_dir/full_select_visual_prototype.py" \
    > "$frame_abi_mismatch_plugin"

cmp -s \
    "$package_dir/full_select_visual_prototype.py" \
    "$frame_abi_mismatch_plugin" &&
    fail "Frame-ABI mismatch test could not alter the plugin contract."

if python3 "$package_dir/tools/verify-native-binary.py" \
    --plugin "$frame_abi_mismatch_plugin" \
    --binary "$package_dir/full_select_native/libterminator_full_select_engine.so"
then
    fail "Native verifier accepted an incompatible frame-ABI contract."
fi

rollback_home="$workspace/rollback-home"
rollback_config="$workspace/rollback-config"
rollback_state="$workspace/rollback-state"
rollback_bin="$workspace/rollback-bin"
rollback_marker="$workspace/rollback-marker"
real_mv="$(command -v mv)"
rollback_plugin="$rollback_config/terminator/plugins/full_select_visual_prototype.py"
rollback_native="$rollback_config/terminator/plugins/full_select_native/libterminator_full_select_engine.so"
rollback_uninstaller="$rollback_state/terminator-full-select/uninstall.sh"
rollback_state_file="$rollback_state/terminator-full-select/current-install"
rollback_expected="$workspace/rollback-expected"

mkdir -p \
    "$rollback_home" \
    "$(dirname -- "$rollback_plugin")" \
    "$(dirname -- "$rollback_native")" \
    "$(dirname -- "$rollback_uninstaller")" \
    "$rollback_bin" \
    "$rollback_expected"
ln -s -- "$project_dir/tests/fail-state-mv.sh" "$rollback_bin/mv"
printf 'previous plugin\n' > "$rollback_plugin"
printf 'previous native\n' > "$rollback_native"
printf '#!/bin/sh\nprintf previous-uninstaller\n' > "$rollback_uninstaller"
printf 'version=previous\n' > "$rollback_state_file"
chmod 0644 "$rollback_plugin" "$rollback_native"
chmod 0700 "$rollback_uninstaller"
chmod 0600 "$rollback_state_file"
cp -a -- \
    "$rollback_plugin" \
    "$rollback_native" \
    "$rollback_uninstaller" \
    "$rollback_state_file" \
    "$rollback_expected/"

if PATH="$rollback_bin:$PATH" \
TFSE_TEST_REAL_MV="$real_mv" \
TFSE_TEST_MV_FAILURE_MARKER="$rollback_marker" \
HOME="$rollback_home" \
XDG_CONFIG_HOME="$rollback_config" \
XDG_STATE_HOME="$rollback_state" \
    "$package_dir/install.sh"
then
    fail "Installer unexpectedly succeeded after an injected state-commit failure."
fi

cmp -s "$rollback_expected/full_select_visual_prototype.py" "$rollback_plugin" ||
    fail "Installer rollback did not restore the previous plugin."
cmp -s "$rollback_expected/libterminator_full_select_engine.so" "$rollback_native" ||
    fail "Installer rollback did not restore the previous native binary."
cmp -s "$rollback_expected/uninstall.sh" "$rollback_uninstaller" ||
    fail "Installer rollback did not restore the previous uninstaller."
cmp -s "$rollback_expected/current-install" "$rollback_state_file" ||
    fail "Installer rollback did not restore the previous installation state."
[[ "$(stat -c '%a' "$rollback_uninstaller")" == 700 ]] ||
    fail "Installer rollback did not restore the uninstaller mode."

test_home="$workspace/home"
test_config="$workspace/config"
test_state="$workspace/state"

mkdir -p "$test_home" "$test_config" "$test_state"

package_native="$package_dir/full_select_native/libterminator_full_select_engine.so"
package_native_backup="$workspace/package-native.backup"
cp -- "$package_native" "$package_native_backup"
printf 'tamper\n' >> "$package_native"

if python3 "$package_dir/tools/verify-native-binary.py" \
    --plugin "$package_dir/full_select_visual_prototype.py" \
    --binary "$package_native"
then
    fail "Native verifier accepted a modified binary."
fi

if HOME="$test_home" \
XDG_CONFIG_HOME="$test_config" \
XDG_STATE_HOME="$test_state" \
    "$package_dir/install.sh"
then
    fail "Installer accepted a package with a modified native binary."
fi

mv -f -- "$package_native_backup" "$package_native"

installed_plugin="$test_config/terminator/plugins/full_select_visual_prototype.py"
installed_native="$test_config/terminator/plugins/full_select_native/libterminator_full_select_engine.so"
persistent_uninstaller="$test_state/terminator-full-select/uninstall.sh"
symlink_target="$workspace/symlink-target"

mkdir -p -- "$(dirname -- "$installed_plugin")"
printf 'must remain unchanged\n' > "$symlink_target"
ln -s -- "$symlink_target" "$installed_plugin"

if HOME="$test_home" \
XDG_CONFIG_HOME="$test_config" \
XDG_STATE_HOME="$test_state" \
    "$package_dir/install.sh"
then
    fail "Installer accepted a symlink at the plugin target."
fi

[[ "$(<"$symlink_target")" == "must remain unchanged" ]] ||
    fail "Installer changed the target of a refused symlink."
rm -f -- "$installed_plugin"

HOME="$test_home" \
XDG_CONFIG_HOME="$test_config" \
XDG_STATE_HOME="$test_state" \
    "$package_dir/install.sh"

cmp -s \
    "$package_dir/full_select_visual_prototype.py" \
    "$installed_plugin" ||
    fail "Installed Python plugin differs from the package."
cmp -s \
    "$package_dir/full_select_native/libterminator_full_select_engine.so" \
    "$installed_native" ||
    fail "Installed native binary differs from the package."
[[ -x "$persistent_uninstaller" ]] ||
    fail "Persistent uninstaller was not installed."
[[ "$(stat -c '%a' "$test_state/terminator-full-select")" == 700 ]] ||
    fail "Installer state directory is not private."
[[ "$(stat -c '%a' "$persistent_uninstaller")" == 700 ]] ||
    fail "Persistent uninstaller mode is not 0700."

printf '\n# local modification\n' >> "$installed_plugin"

if HOME="$test_home" \
XDG_CONFIG_HOME="$test_config" \
XDG_STATE_HOME="$test_state" \
    "$persistent_uninstaller"
then
    fail "Uninstaller accepted a modified installed file."
fi

HOME="$test_home" \
XDG_CONFIG_HOME="$test_config" \
XDG_STATE_HOME="$test_state" \
    "$package_dir/install.sh"

sentinel="$test_config/terminator/plugins/full_select_native/keep-me.txt"
printf 'unrelated file\n' > "$sentinel"

HOME="$test_home" \
XDG_CONFIG_HOME="$test_config" \
XDG_STATE_HOME="$test_state" \
    "$persistent_uninstaller"

[[ ! -e "$installed_plugin" ]] ||
    fail "Python plugin remains after uninstall."
[[ ! -e "$installed_native" ]] ||
    fail "Native binary remains after uninstall."
[[ -f "$sentinel" ]] ||
    fail "Uninstaller removed an unrelated native-directory file."
[[ ! -e "$persistent_uninstaller" ]] ||
    fail "Persistent uninstaller remains after uninstall."

printf 'binary_package_test=passed\n'
printf 'install_transaction_rollback=passed\n'
printf 'abi_handshake_rejection=passed\n'
printf 'native_contract_tamper_rejection=passed\n'
printf 'tampered_package_rejection=passed\n'
printf 'target_symlink_rejection=passed\n'
printf 'modified_file_protection=passed\n'
printf 'unrelated_file_preservation=passed\n'
printf 'source_manifest_verification=passed\n'
