#!/bin/sh

set -eu

umask 022

PROGRAM_NAME="Terminator Full Select installer"

fail() {
    printf '%s: ERROR: %s\n' "$PROGRAM_NAME" "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 ||
        fail "Required command not found: $1"
}

for required_command in \
    chmod \
    cmp \
    date \
    dirname \
    install \
    mktemp \
    mv \
    rm \
    sed \
    sha256sum \
    uname
do
    require_command "$required_command"
done

if command -v python3 >/dev/null 2>&1; then
    PYTHON_COMMAND=$(command -v python3)
elif command -v python >/dev/null 2>&1 &&
    python -c 'import sys; raise SystemExit(sys.version_info[0] != 3)'
then
    PYTHON_COMMAND=$(command -v python)
else
    fail "Python 3 was not found."
fi

PACKAGE_ROOT=$(
    CDPATH= cd -- "$(dirname -- "$0")" &&
        pwd -P
)

PACKAGE_PLUGIN="$PACKAGE_ROOT/full_select_visual_prototype.py"
PACKAGE_NATIVE="$PACKAGE_ROOT/full_select_native/libterminator_full_select_engine.so"
PACKAGE_VERIFIER="$PACKAGE_ROOT/tools/verify-native-binary.py"
PACKAGE_MANIFEST="$PACKAGE_ROOT/SHA256SUMS"
PACKAGE_VERSION_FILE="$PACKAGE_ROOT/VERSION"
PACKAGE_UNINSTALLER="$PACKAGE_ROOT/uninstall.sh"

for required_file in \
    "$PACKAGE_PLUGIN" \
    "$PACKAGE_NATIVE" \
    "$PACKAGE_VERIFIER" \
    "$PACKAGE_MANIFEST" \
    "$PACKAGE_VERSION_FILE" \
    "$PACKAGE_UNINSTALLER"
do
    [ -f "$required_file" ] ||
        fail "Required package file is missing: $required_file"
    [ ! -L "$required_file" ] ||
        fail "Required package file must not be a symlink: $required_file"
done

case "$(uname -m)" in
    x86_64 | amd64)
        ;;
    *)
        fail "This package requires Linux x86-64."
        ;;
esac

(
    CDPATH= cd -- "$PACKAGE_ROOT"
    sha256sum -c SHA256SUMS
) || fail "Package checksum verification failed."

"$PYTHON_COMMAND" "$PACKAGE_VERIFIER" \
    --plugin "$PACKAGE_PLUGIN" \
    --binary "$PACKAGE_NATIVE" ||
    fail "Native binary contract verification failed."

PACKAGE_VERSION=$(sed -n '1p' "$PACKAGE_VERSION_FILE")
[ -n "$PACKAGE_VERSION" ] ||
    fail "VERSION is empty."

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
TARGET_UNINSTALLER="$STATE_DIRECTORY/uninstall.sh"

install -d -m 0755 -- \
    "$PLUGIN_DIRECTORY" \
    "$NATIVE_DIRECTORY"
install -d -m 0700 -- \
    "$STATE_DIRECTORY" \
    "$STATE_DIRECTORY/backups"

validate_target() {
    target_file=$1

    [ ! -L "$target_file" ] ||
        fail "Refusing to replace symlink: $target_file"

    if [ -e "$target_file" ] && [ ! -f "$target_file" ]; then
        fail "Refusing to replace a non-regular file: $target_file"
    fi
}

validate_target "$TARGET_PLUGIN"
validate_target "$TARGET_NATIVE"
validate_target "$TARGET_UNINSTALLER"
validate_target "$STATE_FILE"

TIMESTAMP=$(date -u '+%Y%m%dT%H%M%SZ')
BACKUP_DIRECTORY="$STATE_DIRECTORY/backups/$TIMESTAMP-$$"
install -d -m 0700 -- "$BACKUP_DIRECTORY"

PLUGIN_EXISTED=0
NATIVE_EXISTED=0
UNINSTALLER_EXISTED=0
STATE_EXISTED=0
PLUGIN_COMMITTED=0
NATIVE_COMMITTED=0
UNINSTALLER_COMMITTED=0
STATE_COMMITTED=0
INSTALL_COMPLETE=0
PLUGIN_TEMP=""
NATIVE_TEMP=""
UNINSTALLER_TEMP=""
STATE_TEMP=""

if [ -f "$TARGET_PLUGIN" ]; then
    install -m 0600 -- \
        "$TARGET_PLUGIN" \
        "$BACKUP_DIRECTORY/full_select_visual_prototype.py"
    PLUGIN_EXISTED=1
fi

if [ -f "$TARGET_NATIVE" ]; then
    install -m 0600 -- \
        "$TARGET_NATIVE" \
        "$BACKUP_DIRECTORY/libterminator_full_select_engine.so"
    NATIVE_EXISTED=1
fi

if [ -f "$TARGET_UNINSTALLER" ]; then
    install -m 0600 -- \
        "$TARGET_UNINSTALLER" \
        "$BACKUP_DIRECTORY/uninstall.sh"
    UNINSTALLER_EXISTED=1
fi

if [ -f "$STATE_FILE" ]; then
    install -m 0600 -- \
        "$STATE_FILE" \
        "$BACKUP_DIRECTORY/current-install"
    STATE_EXISTED=1
fi

rollback_file() {
    target_file=$1
    backup_file=$2
    existed_before=$3
    target_directory=$4
    restore_mode=$5

    if [ "$existed_before" -eq 1 ]; then
        restore_temp=$(mktemp "$target_directory/.tfse-restore.XXXXXX")
        install -m "$restore_mode" -- "$backup_file" "$restore_temp"
        mv -f -- "$restore_temp" "$target_file"
    else
        rm -f -- "$target_file"
    fi
}

cleanup() {
    exit_status=$?
    trap - 0 HUP INT TERM

    [ -z "$PLUGIN_TEMP" ] ||
        rm -f -- "$PLUGIN_TEMP"
    [ -z "$NATIVE_TEMP" ] ||
        rm -f -- "$NATIVE_TEMP"
    [ -z "$UNINSTALLER_TEMP" ] ||
        rm -f -- "$UNINSTALLER_TEMP"
    [ -z "$STATE_TEMP" ] ||
        rm -f -- "$STATE_TEMP"

    if [ "$INSTALL_COMPLETE" -ne 1 ]; then
        if [ "$UNINSTALLER_COMMITTED" -eq 1 ]; then
            rollback_file \
                "$TARGET_UNINSTALLER" \
                "$BACKUP_DIRECTORY/uninstall.sh" \
                "$UNINSTALLER_EXISTED" \
                "$STATE_DIRECTORY" \
                0700
        fi

        if [ "$PLUGIN_COMMITTED" -eq 1 ]; then
            rollback_file \
                "$TARGET_PLUGIN" \
                "$BACKUP_DIRECTORY/full_select_visual_prototype.py" \
                "$PLUGIN_EXISTED" \
                "$PLUGIN_DIRECTORY" \
                0644
        fi

        if [ "$NATIVE_COMMITTED" -eq 1 ]; then
            rollback_file \
                "$TARGET_NATIVE" \
                "$BACKUP_DIRECTORY/libterminator_full_select_engine.so" \
                "$NATIVE_EXISTED" \
                "$NATIVE_DIRECTORY" \
                0644
        fi

        if [ "$STATE_COMMITTED" -eq 1 ]; then
            rollback_file \
                "$STATE_FILE" \
                "$BACKUP_DIRECTORY/current-install" \
                "$STATE_EXISTED" \
                "$STATE_DIRECTORY" \
                0600
        fi
    fi

    exit "$exit_status"
}

trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

NATIVE_TEMP=$(mktemp "$NATIVE_DIRECTORY/.tfse-native.XXXXXX")
install -m 0644 -- "$PACKAGE_NATIVE" "$NATIVE_TEMP"
mv -f -- "$NATIVE_TEMP" "$TARGET_NATIVE"
NATIVE_TEMP=""
NATIVE_COMMITTED=1

PLUGIN_TEMP=$(mktemp "$PLUGIN_DIRECTORY/.tfse-plugin.XXXXXX")
install -m 0644 -- "$PACKAGE_PLUGIN" "$PLUGIN_TEMP"
mv -f -- "$PLUGIN_TEMP" "$TARGET_PLUGIN"
PLUGIN_TEMP=""
PLUGIN_COMMITTED=1

cmp -s "$PACKAGE_NATIVE" "$TARGET_NATIVE" ||
    fail "Installed native binary verification failed."
cmp -s "$PACKAGE_PLUGIN" "$TARGET_PLUGIN" ||
    fail "Installed Python plugin verification failed."

UNINSTALLER_TEMP=$(mktemp "$STATE_DIRECTORY/.uninstall.XXXXXX")
install -m 0700 -- "$PACKAGE_UNINSTALLER" "$UNINSTALLER_TEMP"
mv -f -- "$UNINSTALLER_TEMP" "$TARGET_UNINSTALLER"
UNINSTALLER_TEMP=""
UNINSTALLER_COMMITTED=1

PLUGIN_SHA256=$(sha256sum "$TARGET_PLUGIN" | sed 's/[[:space:]].*$//')
NATIVE_SHA256=$(sha256sum "$TARGET_NATIVE" | sed 's/[[:space:]].*$//')
UNINSTALLER_SHA256=$(
    sha256sum "$TARGET_UNINSTALLER" |
        sed 's/[[:space:]].*$//'
)

STATE_TEMP=$(mktemp "$STATE_DIRECTORY/.current-install.XXXXXX")
{
    printf 'version=%s\n' "$PACKAGE_VERSION"
    printf 'plugin_path=%s\n' "$TARGET_PLUGIN"
    printf 'plugin_sha256=%s\n' "$PLUGIN_SHA256"
    printf 'native_path=%s\n' "$TARGET_NATIVE"
    printf 'native_sha256=%s\n' "$NATIVE_SHA256"
    printf 'uninstaller_path=%s\n' "$TARGET_UNINSTALLER"
    printf 'uninstaller_sha256=%s\n' "$UNINSTALLER_SHA256"
    printf 'backup_directory=%s\n' "$BACKUP_DIRECTORY"
} > "$STATE_TEMP"
chmod 0600 "$STATE_TEMP"
STATE_COMMITTED=1
mv -f -- "$STATE_TEMP" "$STATE_FILE"
STATE_TEMP=""

INSTALL_COMPLETE=1

printf 'Terminator Full Select %s installed successfully.\n' \
    "$PACKAGE_VERSION"
printf 'plugin=%s\n' "$TARGET_PLUGIN"
printf 'native_engine=%s\n' "$TARGET_NATIVE"
printf 'uninstaller=%s\n' "$TARGET_UNINSTALLER"
printf 'backup=%s\n' "$BACKUP_DIRECTORY"
printf '%s\n' \
    "Fully close and reopen Terminator, then enable FullSelectVisualPrototype."
