#!/bin/sh
# Verify that a candidate food/drink name matches the user's raw input, using
# the Jev (TypeSafe System One) decision model.
#
# usage: jev_verify.sh "<raw input>" "<candidate name>" [threshold]
#
# Prints one JSON line, e.g.
#   {"score":0.98,"accept":true,"threshold":0.88,"model":"jev-1.13.0"}
# accept:null means Jev was unreachable -> treat as UNVERIFIED, not rejected.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec node "$DIR/jev_verify.mjs" "$@"