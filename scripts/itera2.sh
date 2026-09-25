# This file defines the `itera` loop.
#
# `committe` turns one prompt into one commit, and the first commit is
# likely to have problems.  `itera` runs `committe` until the tests pass,
# feeding each round the failing test output, the commit that answered it
# and the tree it produced, because stale files are what makes a loop
# thrash.  A failure of the patch step is not a failure of the tests, so
# it is told apart and fed back as its own message.
#
# Unlike the older `itera.sh`, the loop does not run inside a sandbox.
# `dic` and `committe` are assumed to be safe, and only the test command
# is sandboxed, in a throwaway git worktree of the repository.  committe
# therefore commits for real, in the worktree, on a private branch; when
# the loop is over the rounds are squashed into the first round's commit
# and fast-forwarded into the caller's branch.  The caller's tree is not
# touched until that point, and a loop that fails leaves nothing behind.
#
# The public interface is the same as `itera.sh`: the same flags, the
# same arguments to committe, and the same `--test`/`--max` defaults.

# The sibling script, so the loop can source the very same `committe`
# the user has.
ITERA_COMMITTE_SH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/committe.sh"

function itera-session-dir() {
    # Where dic keeps its per-session pointers.  This mirrors dic's own
    # fallback when XDG_RUNTIME_DIR is unset, and has to keep mirroring
    # it: a wrong guess here continues a conversation nobody has.
    if [[ -n ${XDG_RUNTIME_DIR:-} ]]; then
        printf '%s/fac/dic\n' "$XDG_RUNTIME_DIR"
    else
        printf '/tmp/fac-%s/dic\n' "$UID"
    fi
}

function itera-fork-session() {
    # Print the mid the current session ends at, and copy that pointer
    # onto a fresh name.  The loop's first round therefore continues the
    # user's conversation -- it sends the whole history -- while advancing
    # only its own pointer, so the user's next `dic -c` still lands where
    # the user left it.
    local session="$1" dir src
    dir=$(itera-session-dir)
    src="$dir/${DIC_SESSION:-global}"
    if [[ ! -f "$src" ]]; then
        printf 'itera-error: no conversation to continue at %s\n' "$src" >&2
        printf 'itera-hint: run a dic call in this session first\n' >&2
        return 1
    fi
    mkdir -p -- "$dir" || return 1
    cp -- "$src" "$dir/$session" || return 1
    cat -- "$dir/$session"
}

function itera-usage() {
    cat <<'EOF'
usage: itera [flags] [REQUEST...]

Run committe in a loop until the tests pass.  The loop runs in a
throwaway git worktree of the current repository and the rounds leave
as one squashed commit fast-forwarded into the caller's branch.

flags:
  --test CMD    the test command (default: $ITERA_TEST, or ./test.sh).
  --max N       the round budget (default: $ITERA_MAX, or 8).
  --net MODE    the sandbox network policy: none (default) or host.
  -h, --help    show this help.

environment:
  ITERA_SETUP   a shell command to run in the worktree before the first
                round, for the ignored state a fresh worktree lacks
                (a venv, node_modules, a .env).  It must not modify
                tracked files, because committe expects a clean tree.

Every other argument is passed to committe as the request.  With -c or
--mid the request may be empty: the loop then continues the conversation
the user's session already points at.
EOF
}

function itera-teardown() {
    # Remove the worktree, its branch, and the stale registration the
    # worktree leaves in .git/worktrees if it was removed by hand.
    local wt=$1 branch=$2 keep=$3
    [[ -n $wt && -d $wt ]] && { git worktree remove --force -- "$wt" 2>/dev/null || rm -rf -- "$wt"; }
    git worktree prune 2>/dev/null
    [[ -z $keep ]] && git branch -D -- "$branch" 2>/dev/null
    return 0
}

function itera() {
    local test_cmd="${ITERA_TEST:-./test.sh}"
    local max="${ITERA_MAX:-8}"
    local net="${ITERA_NET:-none}"
    local -a request=()

    while (( $# )); do
        case "$1" in
            -h|--help) itera-usage; return 0 ;;
            --test)    test_cmd="$2"; shift 2 ;;
            --max)     max="$2"; shift 2 ;;
            --net)     net="$2"; shift 2 ;;
            --)        shift; request=("$@"); break ;;
            *)         request=("$@"); break ;;
        esac
    done

    if [[ ! -f "$ITERA_COMMITTE_SH" ]]; then
        echo 'itera-error: committe.sh not found beside itera2.sh' >&2
        return 1
    fi
    if ! command -v sandbox >/dev/null 2>&1; then
        echo 'itera-error: sandbox not found' >&2
        return 1
    fi
    # committe checks that the tree is clean and that we are in a git
    # repository, so there is nothing to check here.

    # The subshell keeps the EXIT trap from outliving this call, so a
    # later `return` in the caller's shell does not try to tear down a
    # worktree that is already gone.
    (
    local base branch wt session i=0 prev= prior= out tree first keep= rc=0
    trap 'itera-teardown "$wt" "$branch" "$keep"' EXIT

    base=$(git rev-parse HEAD) || exit 1
    branch="itera/$$"
    wt=$(mktemp -d "${TMPDIR:-/tmp}/itera.XXXXXXXX") || exit 1
    git worktree add --quiet -b "$branch" "$wt" "$base" || exit 1
    if [[ -n ${ITERA_SETUP:-} ]]; then
        ( cd -- "$wt" && eval "$ITERA_SETUP" ) || exit 1
    fi

    # Continue the user's conversation, on a pointer of itera's own.
    session="itera-$$"
    itera-fork-session "$session" >/dev/null || exit 1

    while (( i < max )); do
        i=$((i + 1))
        printf 'itera: round %d/%d\n' "$i" "$max" >&2

        # Only the test command is sandboxed: it is the only thing in the
        # loop that runs code the model wrote.  The worktree is mounted
        # writable so the tests can leave artifacts where they expect to.
        if out=$(sandbox --rw "$wt:/w" --cwd /w --net "$net" \
                     -- bash -c "$test_cmd" 2>&1); then
            printf 'itera: green after %d round(s)\n' "$((i - 1))" >&2
            break
        fi

        # A round that changed nothing is a round that will repeat itself
        # forever.  Report it and stop rather than burn the budget.
        tree=$(git -C "$wt" rev-parse 'HEAD^{tree}')
        if [[ "$tree" == "$prev" ]]; then
            printf 'itera: the tree did not change; stopping\n' >&2
            rc=2
            break
        fi
        prev="$tree"

        if {
            printf '%s' "$prior"
            printf 'The tests fail with:\n\n%s\n\n' "$out"
            printf 'The last commit was:\n\n'
            git -C "$wt" --no-pager log -1 -p
            printf '\nThe tree is:\n\n'
            ( cd -- "$wt" && files-to-prompt . )
        } | ( cd -- "$wt" && DIC_SESSION="$session" committe "${request[@]}" ) >&2; then
            prior=
        else
            printf 'itera: the patch did not apply\n' >&2
            prior='The previous round produced no commit: the patch did not apply.  The context lines did not match the files, so re-read the tree below and restate the patch against it.

'
        fi
    done

    if (( i >= max )); then
        printf 'itera: hit ITERA_MAX=%d\n' "$max" >&2
        rc=1
    fi

    # ---- squash and merge ---------------------------------------------------
    #
    # N rounds are one change and one commit.  Keep the first round's
    # message, the one written against a clean tree, and let its author
    # own the squashed commit.  A fast-forward cannot conflict, so the
    # caller's branch either advances by this commit or does not move;
    # if it moved we keep the branch so nothing is lost.
    first=$(git -C "$wt" rev-list --reverse "$base".."$branch" | head -n1)
    if [[ -n "$first" ]]; then
        git -C "$wt" reset --soft "$base" || exit 1
        git -C "$wt" commit --quiet -C "$first" || exit 1
        if git merge --ff-only --quiet "$branch"; then
            printf 'itera: merged %s\n' "$(git rev-parse --short HEAD)" >&2
        else
            keep=1
            rc=1
            printf 'itera-error: HEAD moved; the rounds are on %s\n' "$branch" >&2
            printf 'itera-hint: git merge --ff-only %s\n' "$branch" >&2
        fi
    fi

    exit "$rc"
    )
}
