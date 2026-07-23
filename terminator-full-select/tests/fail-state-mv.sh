#!/usr/bin/env bash

set -euo pipefail

real_mv=${TFSE_TEST_REAL_MV:?}
failure_marker=${TFSE_TEST_MV_FAILURE_MARKER:?}

[[ "$#" -gt 0 ]] || exit 2
destination=${!#}

if [[
    "$destination" == */terminator-full-select/current-install &&
    ! -e "$failure_marker"
]]; then
    printf 'state move rejected for rollback test\n' > "$failure_marker"
    exit 91
fi

exec "$real_mv" "$@"
