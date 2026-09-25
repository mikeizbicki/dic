# This file defines the `itera` loop.
#
# `committe` turns one prompt into one commit, and the first commit is
# likely to have problems.  `itera` runs `committe` until the tests pass,
# feeding each round the failing test output, the commit that answered it
# and the tree it produced, because stale files are what makes a loop
# thrash.  A failure of the patch step is not a failure of the tests, so
# it is told apart and fed back as its own message.
#
# The test runs in a sandbox with a read-only view of the repository and
# no network: the test is the only thing in the loop that runs code the
# model wrote, and it must be assumed compromised.  Neither the read-only
# view nor the empty network is configurable.
#
# `committe` commits for real, one commit per round, in the caller's
# repository.  There is no worktree and no squash: the branch is the work.

# The sibling script, so the loop can find the very same `committe`
# the user has.
ITERA_COMMITTE_SH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/committe.sh"

function itera-usage() {
    cat <<'EOF'
usage: itera [flags] [REQUEST...]

Run committe in a loop until the tests pass.  Each round runs the test
command in a sandbox, then hands the failure to committe, which commits
for real.

The sandbox mounts the repository read-only and has no network.

flags:
  --test CMD    the test command (default: $ITERA_TEST, or ./test.sh).
  --max N       the round budget (default: $ITERA_MAX, or 8).
  -h, --help    show this help.

Every argument is passed on to committe as the request.
EOF
}

function itera() {
    local test_cmd="${ITERA_TEST:-./test.sh}"
    local max="${ITERA_MAX:-8}"
    local -a request=()

    while (( $# )); do
        case "$1" in
            -h|--help) itera-usage; return 0 ;;
            --test)    test_cmd="$2"; shift 2 ;;
            --max)     max="$2"; shift 2 ;;
            --)        shift; request=("$@"); break ;;
            *)         request=("$@"); break ;;
        esac
    done

    if [[ ! -f "$ITERA_COMMITTE_SH" ]]; then
        echo 'itera-error: committe.sh not found beside itera4.sh' >&2
        return 1
    fi
    if ! command -v sandbox >/dev/null 2>&1; then
        echo 'itera-error: sandbox not found' >&2
        return 1
    fi
    # committe checks that we are in a git repository with a clean tree,
    # so there is nothing to check here.

    local i=0 prev= prior= out tree
    while (( i < max )); do
        i=$((i + 1))
        printf 'itera: round %d/%d\n' "$i" "$max" >&2

        # Only the test command is sandboxed: it is the only thing in the
        # loop that runs code the model wrote.  The repository is mounted
        # read-only and the network is unshared, so the test can neither
        # change what committe sees nor call home.
        if out=$(sandbox --ro "$PWD:/w" --cwd /w -- bash -c "$test_cmd" 2>&1); then
            printf 'itera: green after %d round(s)\n' "$((i - 1))" >&2
            return 0
        fi

        # A round that changed nothing is a round that will repeat itself
        # forever.  Report it and stop rather than burn the budget.
        tree=$(git rev-parse 'HEAD^{tree}')
        if [[ "$tree" == "$prev" ]]; then
            printf 'itera: the tree did not change; stopping\n' >&2
            return 2
        fi
        prev="$tree"

        if {
            printf '%s' "$prior"
            printf 'The tests fail with:\n\n%s\n\n' "$out"
            printf 'The last commit was:\n\n'
            git --no-pager log -1 -p
            printf '\nThe tree is:\n\n'
            files-to-prompt .
        } | committe "${request[@]}" >&2; then
            prior=
        else
            printf 'itera: the patch did not apply\n' >&2
            prior='The previous round produced no commit: the patch did not apply.  The context lines did not match the files, so re-read the tree below and restate the patch against it.

'
        fi
    done

    printf 'itera: hit ITERA_MAX=%d\n' "$max" >&2
    return 1
}
