# This file defines the `itera` loop.
#
# `committe` turns one prompt into one commit, and the first commit is
# likely to have problems.  `itera` runs `committe` until the tests pass,
# feeding each round the failing test output, the commit that answered it
# and the tree it produced, because stale files are what makes a loop
# thrash.  A failure of the patch step is not a failure of the tests, so
# it is told apart and fed back as its own message.
#
# The loop runs inside `sandbox` on an overlay of the repository: the tree
# the user sees is the read-only lower layer, and every write -- the
# tests', git's, the model's -- lands in a tmpfs that dies with the loop.
# The host repository is not touched until the loop is over, and the
# rounds leave as one squashed commit applied with `git am --3way`.

# The sibling script, so the shell inside the sandbox can source the very
# same `committe` the user has.
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

Run committe in a loop until the tests pass.  The loop runs in a sandbox
on a writable overlay of the current repository, and the rounds leave as
one squashed commit applied with git am --3way.

flags:
  --test CMD    the test command (default: $ITERA_TEST, or ./test.sh).
  --max N       the round budget (default: $ITERA_MAX, or 8).
  -h, --help    show this help.

Every other argument is passed to committe as the request.  With -c or
--mid the request may be empty: the loop then continues the conversation
the user's session already points at.
EOF
}

function itera-inner-script() {
    # The script that runs inside the sandbox.  It is a global function so
    # that it can be read, and run under `bash -x`, while debugging a loop.
    cat <<'ITERA_INNER'
set -euo pipefail

# ---- the conversation ------------------------------------------------------
#
# dic's `-c` reads the pointer this writes.  Seeding it from the mid the
# user's session ends at makes the loop's first round a continuation of
# the user's conversation and every later round a continuation of the
# loop's own, so the loop can be priced and audited by itself.
mkdir -p -- "$XDG_RUNTIME_DIR/fac/dic"
printf '%s\n' "$ITERA_MID" > "$XDG_RUNTIME_DIR/fac/dic/$DIC_SESSION"

# ---- git -------------------------------------------------------------------
#
# The sandbox starts with an empty environment, so git has no identity
# and no global configuration.  None of it should leak in in any case:
# the loop's commits belong to `itera`, not to the user.
export GIT_COMMITTER_NAME=itera GIT_COMMITTER_EMAIL=itera@agent
export GIT_AUTHOR_NAME=itera GIT_AUTHOR_EMAIL=itera@agent
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

. "$ITERA_COMMITTE_SH"

base=$(git rev-parse HEAD)
i=0
prev=
rc=0
prior=

while (( i < ITERA_MAX )); do
    i=$((i + 1))
    printf 'itera: round %d/%d\n' "$i" "$ITERA_MAX" >&2

    # The test command is one string, the way it was written on the
    # command line, so it is eval'd rather than split.
    if out=$(eval "$ITERA_TEST" 2>&1); then
        printf 'itera: green after %d round(s)\n' "$((i - 1))" >&2
        break
    fi

    # A round that changed nothing is a round that will repeat itself
    # forever.  Report it and stop rather than burn the budget.
    tree=$(git rev-parse 'HEAD^{tree}')
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
        git --no-pager log -1 -p
        printf '\nThe tree is:\n\n'
        files-to-prompt .
    } | committe "$@" >&2; then
        prior=
    else
        printf 'itera: the patch did not apply\n' >&2
        prior='The previous round produced no commit: the patch did not apply.  The context lines did not match the files, so re-read the tree below and restate the patch against it.

'
    fi
done

if (( i >= ITERA_MAX )); then
    printf 'itera: hit ITERA_MAX=%d\n' "$ITERA_MAX" >&2
    rc=1
fi

# ---- squash and export -----------------------------------------------------
#
# N rounds are one change and one commit.  Keep the first round's message,
# the one written against a clean tree, and let its author own the
# squashed commit.
first=$(git rev-list --reverse "$base"..HEAD | head -n1 || true)
if [[ -n "$first" ]]; then
    git reset --soft "$base"
    git commit --quiet -C "$first"
    git format-patch --stdout -1
fi

exit "$rc"
ITERA_INNER
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

    if ! command -v sandbox >/dev/null 2>&1; then
        echo 'itera-error: sandbox not found' >&2
        return 1
    fi
    if [[ ! -f "$ITERA_COMMITTE_SH" ]]; then
        echo 'itera-error: committe.sh not found beside itera.sh' >&2
        return 1
    fi
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo 'itera-error: not inside a git repository' >&2
        return 1
    fi
    if ! git diff --quiet --cached; then
        echo 'itera-error: staging area is non-empty' >&2
        return 1
    fi
    if ! git diff --quiet; then
        echo 'itera-error: working tree has uncommitted changes' >&2
        return 1
    fi

    local conf="${XDG_CONFIG_HOME:-$HOME/.config}/fac"
    if [[ ! -d "$conf" ]]; then
        printf 'itera-error: no dic configuration at %s\n' "$conf" >&2
        return 1
    fi

    # Continue the user's conversation, on a pointer of itera's own.
    local session="itera-$$" mid
    mid=$(itera-fork-session "$session") || return $?
    if [[ -z "$mid" ]]; then
        echo 'itera-error: the session pointer is empty' >&2
        return 1
    fi

    # dic needs its keys inside; everything else it needs is invented
    # there.  Forward every _API_KEY the shell has exported.
    local -a keys=()
    local k
    while IFS= read -r k; do
        [[ "$k" == *_API_KEY ]] || continue
        keys+=(--pass-env "$k")
    done < <(compgen -e)

    # The repository and dic's state directory are both overlays, so the
    # bytes the host holds are the read-only lower layer of each and
    # nothing the loop writes can reach them.  dic's new messages land in
    # an upper layer that dies with the sandbox, which is what keeps the
    # loop's conversation out of the user's history.
    local patch rc
    patch=$(mktemp) || return 1

    sandbox \
        --overlay "$PWD:/w" \
        --overlay "$conf:/home/itera/.config/fac" \
        --ro "$(dirname -- "$ITERA_COMMITTE_SH"):/etc/itera" \
        --cwd /w \
        --net=live \
        --env 'HOME=/home/itera' \
        --env 'XDG_RUNTIME_DIR=/tmp/itera-run' \
        --env "DIC_SESSION=$session" \
        --env "ITERA_MID=$mid" \
        --env "ITERA_MAX=$max" \
        --env "ITERA_TEST=$test_cmd" \
        --env 'ITERA_COMMITTE_SH=/etc/itera/committe.sh' \
        "${keys[@]}" \
        -- bash -c "$(itera-inner-script)" itera -- "${request[@]}" \
        > "$patch"
    rc=$?

    if [[ -s "$patch" ]] && ! git am --3way < "$patch"; then
        printf 'itera-error: git am failed\n' >&2
        printf 'itera-hint: the raw patch is at %s\n' "$patch" >&2
        return 1
    fi
    rm -f -- "$patch"
    return "$rc"
}
