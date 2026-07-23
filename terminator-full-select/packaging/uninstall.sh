#!/bin/sh

set -eu

umask 077

PROGRAM_NAME="Terminator Full Select uninstaller"
FORCE=0

fail() {
    printf '%s: ERROR: %s\n' "$PROGRAM_NAME" "$*" >&2
    exit 1
}

case "${1:-}" in
    "")
        ;;
    --force)
        FORCE=1
        ;;
    *)
        fail "Usage: $0 [--force]"
        ;;
esac

for required_command in \
    date \
    install \
    mkdir \
    rm \
    rmdir \
    sed \
    sha256sum
do
    command -v "$required_command" >/dev/null 2>&1 ||
        fail "Required command not found: $required_command"
done

if [ -n "${XDG_CONFIG_HOME:-}" ]; then
    CONFIG_HOME=$XDG_CONFIG_HOME
elif [ -n "${HOME:-}" ]; then
    CONFIG_HOME=$HOME/.config
else
    fail "Neither XDG_CONFIG_HOME nor HOME is set."
fi

if [ -n "${XDG_STATE_HOME:-}" ]; then
    STATE_HOME=$XDG_STATE_HOME
elif [ -n "${HOME:-}" ]; then
    STATE_HOME=$HOME/.local/state
else
    fail "Neither XDG_STATE_HOME nor HOME is set."
fi

PLUGIN_DIRECTORY="$CONFIG_HOME/terminator/plugins"
NATIVE_DIRECTORY="$PLUGIN_DIRECTORY/full_select_native"
TARGET_PLUGIN="$PLUGIN_DIRECTORY/full_select_visual_prototype.py"
TARGET_NATIVE="$NATIVE_DIRECTORY/libterminator_full_select_engine.so"
STATE_DIRECTORY="$STATE_HOME/terminator-full-select"
STATE_FILE="$STATE_DIRECTORY/current-install"
PERSISTENT_UNINSTALLER="$STATE_DIRECTORY/uninstall.sh"

EXPECTED_PLUGIN_SHA256=""
EXPECTED_NATIVE_SHA256=""

if [ -L "$STATE_FILE" ]; then
    [ "$FORCE" -eq 1 ] ||
        fail "Refusing to read symlinked installation state without --force."
elif [ -e "$STATE_FILE" ] && [ ! -f "$STATE_FILE" ]; then
    fail "Installation state is not a regular file; move it aside before uninstalling."
elif [ -f "$STATE_FILE" ]; then
    EXPECTED_PLUGIN_SHA256=$(
        sed -n 's/^plugin_sha256=//p' "$STATE_FILE"
    )
    EXPECTED_NATIVE_SHA256=$(
        sed -n 's/^native_sha256=//p' "$STATE_FILE"
    )
elif [ "$FORCE" -ne 1 ]; then
    fail "Installation state is missing; use --force only after reviewing the target files."
fi

verify_owned_file() {
    target_file=$1
    expected_sha256=$2

    [ -e "$target_file" ] || [ -L "$target_file" ] || return 0

    [ ! -L "$target_file" ] ||
        [ "$FORCE" -eq 1 ] ||
        fail "Refusing to remove symlink without --force: $target_file"

    [ -f "$target_file" ] ||
        [ "$FORCE" -eq 1 ] ||
        fail "Refusing to remove a non-regular file: $target_file"

    if [ "$FORCE" -ne 1 ]; then
        [ -n "$expected_sha256" ] ||
            fail "Missing recorded checksum for $target_file"

        actual_sha256=$(
            sha256sum "$target_file" |
                sed 's/[[:space:]].*$//'
        )

        [ "$actual_sha256" = "$expected_sha256" ] ||
            fail "Installed file was modified; refusing to remove it: $target_file"
    fi
}

verify_owned_file "$TARGET_PLUGIN" "$EXPECTED_PLUGIN_SHA256"
verify_owned_file "$TARGET_NATIVE" "$EXPECTED_NATIVE_SHA256"

install -d -m 0700 -- \
    "$STATE_DIRECTORY" \
    "$STATE_DIRECTORY/backups"

TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
BACKUP_DIRECTORY="$STATE_DIRECTORY/backups/uninstall-$TIMESTAMP-$$"
install -d -m 0700 -- "$BACKUP_DIRECTORY"

if [ -f "$TARGET_PLUGIN" ] && [ ! -L "$TARGET_PLUGIN" ]; then
    install -m 0600 -- \
        "$TARGET_PLUGIN" \
        "$BACKUP_DIRECTORY/full_select_visual_prototype.py"
fi

if [ -f "$TARGET_NATIVE" ] && [ ! -L "$TARGET_NATIVE" ]; then
    install -m 0600 -- \
        "$TARGET_NATIVE" \
        "$BACKUP_DIRECTORY/libterminator_full_select_engine.so"
fi

rm -f -- "$TARGET_PLUGIN"
rm -f -- "$TARGET_NATIVE"
rm -f -- "$STATE_FILE"
rm -f -- "$PERSISTENT_UNINSTALLER"

rmdir -- "$NATIVE_DIRECTORY" 2>/dev/null || true

printf 'Terminator Full Select was removed.\n'
printf 'backup=%s\n' "$BACKUP_DIRECTORY"
printf '%s\n' "Fully close and reopen Terminator."
