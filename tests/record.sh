#!/bin/bash
#
# Record one transcript of the real dic for the shell tests to replay.
#
# usage: tests/record.sh <case> [dic-args...]
#
# Writes tests/fixtures/transcripts/<case>/dic.stdout, what dic printed, and
# dic.exit, the status it ended with.  Run this by hand when a fixture needs
# refreshing or a new one is wanted; the tests only ever read a transcript,
# so a run of them costs nothing.  The prompt is built the way committe
# builds it, so the arguments to record are just the request:
#
#     tests/record.sh add-file -m groq+qwen 'add a file primes.py'

set -euo pipefail

if (( $# < 1 )); then
    sed -n '2,/^$/p' "$0" >&2
    exit 64
fi

here=$(cd -- "$(dirname -- "$0")" && pwd)
case_name=$1; shift
case_dir=$here/fixtures/transcripts/$case_name

# Refuse to record through the fake, which would put its own error message
# in the transcript and look like a successful call.
dic_path=$(command -v dic || true)
case ${dic_path:-} in
    "$here/bin/"*)
        echo "record: tests/bin/dic is first on PATH; run with the real dic" >&2
        exit 1 ;;
    "")
        echo "record: dic is not on PATH" >&2
        exit 1 ;;
esac

source "$here/../scripts/committe.sh"

mkdir -p "$case_dir"

set +e
dic -s "$(committe-prompt)" "$@" > "$case_dir/dic.stdout"
status=$?
set -e
printf '%s\n' "$status" > "$case_dir/dic.exit"

echo "record: wrote $case_dir (dic exited $status)" >&2
