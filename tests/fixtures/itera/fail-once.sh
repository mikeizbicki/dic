#!/bin/bash
# Green for itera's pre-flight, red for the first round, green after that.
#
# itera runs the test once before the loop and once after every round, so the
# calls line up as: 1 is the pre-flight, 2 is after round 1, 3 is after round
# 2.  Failing on the second is a round that changed nothing the tests could
# see; the third is the round that fixed it.
set -u

n=$(cat "$ITERA_STATE" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s\n' "$n" > "$ITERA_STATE"

if (( n == 2 )); then
    echo "the second call is the red one" >&2
    exit 1
fi
exit 0
