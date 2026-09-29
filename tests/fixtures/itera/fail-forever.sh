#!/bin/bash
# Green for itera's pre-flight, then red for every round: itera has to stop
# on its own and not run rounds forever.
set -u

n=$(cat "$ITERA_STATE" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s\n' "$n" > "$ITERA_STATE"

if (( n == 1 )); then
    exit 0                       # the tree was green before anyone touched it
fi
for (( i = 0; i < n; i++ )); do echo "still red" >&2; done
exit 1
