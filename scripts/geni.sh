# This file defines `geni`, the outermost of the coding agents.
#
# `committe` turns one request into one commit, and `itera` repeats that
# until the tests pass.  Both edit the tree the shell is standing in, so
# every failed round lands on the user.  `geni` gives them a tree of their
# own: a new branch off HEAD, checked out in a worktree outside the
# repository, with `itera` run inside it.  Only a green run is merged back,
# as a fast-forward onto the branch the shell started in, and only then is
# the worktree removed.  A run that goes badly leaves the user's own
# checkout untouched, with the failing tests and the commit that did not
# work in the worktree.
#
# The run happens where the shell stands.  Handing it to `launch` so that a
# run is something to leave and come back to, instead of a command that
# holds the shell, is the TODO at the end of this file.  It is deferred for
# now because a window that never opened and a window that opened elsewhere
# look the same to a caller, and a silent run in place behind that is worse
# than a refusal.
#
# The merge is a fast-forward and nothing else, so the tree that arrives is
# byte for byte the tree that passed the tests.  Nothing here resolves a
# conflict, because a conflict is a decision and the user is the one who
# makes it.
#
# Sourcing this file defines the functions and does nothing else.  `itera`
# must already be sourced: the work in the worktree is itera's.

geni-usage() {
    cat <<'EOF'
usage: geni [flags] [REQUEST...]

Run itera on a git worktree of its own, and merge the branch back if the
tests pass.

A new branch off HEAD is checked out in a worktree outside the repository
and REQUEST is passed to itera unchanged there.  The run happens in the
shell that called geni, so geni's status is the run's status.  On success
the branch is fast-forwarded onto the branch that was current and the
worktree is removed.  On failure the worktree is left as it is, with the
unfinished branch and the test output in it.

Because the work is on a branch of its own, the starting checkout is
untouched until the tests are green, and because a fast-forward is the only
merge this does, a green run never has to resolve anything.  A starting
branch that moved while itera ran cannot be fast-forwarded, and geni says
so instead of merging behind the user's back.

flags:
  -h, --help    show this help.

Every other argument is passed on to itera.
EOF
}

# Where the worktrees live.  Outside the repository on purpose: a tool the
# model runs cannot see one, `git status` is never polluted by one, and a
# crash leaves an ordinary directory that `git worktree list` still names.
# $GENI_WORKTREE_ROOT moves them, for a scratch disk.
geni-worktree-root() {
    printf '%s\n' "${GENI_WORKTREE_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/geni/wt}"
}

function geni() {
    # geni's own flags are parsed first; the first `--` ends them and
    # everything after is passed to itera.  A flag geni does not know passes
    # through too, so a flag itera gains works here unedited.
    local -a request=()
    while (( $# )); do
        case "$1" in
            -h|--help) geni-usage; return 0 ;;
            --)        shift; request+=("$@"); break ;;
            *)         request+=("$1"); shift ;;
        esac
    done

    if ! command -v itera >/dev/null 2>&1; then
        echo 'geni-error: itera not found' >&2
        return 1
    fi

    local root
    if ! root=$(git rev-parse --show-toplevel 2>/dev/null); then
        echo 'geni-error: not inside a git repository' >&2
        return 1
    fi

    # The branch starts from HEAD, and the worktree is a copy of nothing: an
    # uncommitted change in the starting tree is simply not in it, so it can
    # never be swept into a commit by accident.
    local base
    if ! base=$(git -C "$root" rev-parse --verify -q HEAD); then
        echo 'geni-error: the repository has no commits' >&2
        return 1
    fi

    # A name is a timestamp and a pid and a random number, because the two
    # things it names -- a branch and a directory -- have to be new, and
    # asking git whether they exist is a race.
    local id="$(date +%y%m%d-%H%M%S)-$RANDOM"
    local branch="geni/$id"
    local wt="$(geni-worktree-root)/${root##*/}-$id"

    mkdir -p "$(dirname "$wt")" || {
        echo "geni-error: cannot create $(dirname "$wt")" >&2
        return 1
    }
    if ! git -C "$root" worktree add -b "$branch" "$wt" "$base"; then
        echo "geni-error: could not add a worktree at $wt" >&2
        return 1
    fi

    # The run happens here.  TODO: hand it to `launch` so that it happens
    # in a window of its own instead; see the TODO at the end of this file.
    # It is not called yet because a window that never opened looks the
    # same as a window that opened elsewhere, and a run in place behind
    # that is worse than saying so.
    geni-run "$wt" "$branch" "$root" "${request[@]}"
}

# Everything geni does once the worktree exists: the loop, the merge back,
# and the banner that says how it went.  It is a function of its own
# because a window starts a new shell, and a new shell can call a function
# but cannot resume the middle of one.
function geni-run() {
    local wt=$1 branch=$2 root=$3; shift 3

    # Remember where the shell was, because success has to put it back.
    # Failure is the one case it is not put back: the worktree is the thing
    # to look at then.
    local start=$PWD
    cd "$wt" || return 1

    printf 'geni: %s on %s\n' "$wt" "$branch" >&2

    # The worktree is clean by construction, so committe's own check that
    # the tree it is about to edit is clean is never forced past here.
    if ! itera "$@"; then
        echo 'geni-error: itera failed; staying in the worktree' >&2
        echo "geni-hint: when the tests pass: git -C '$root' merge --ff-only '$branch'" >&2
        echo "geni-hint: then: git -C '$root' worktree remove '$wt'" >&2
        return 1
    fi

    # What the fast-forward goes onto is read now and not earlier, because
    # the branch the shell started in is not necessarily the one it is in
    # when the run ends.
    local target
    target=$(git -C "$root" symbolic-ref --short -q HEAD \
             || git -C "$root" rev-parse --short HEAD)

    if ! git -C "$root" merge --ff-only "$branch" >&2; then
        echo "geni-error: $target has moved since $branch was cut" >&2
        echo "geni-hint: rebase it: git -C '$wt' rebase '$target'" >&2
        echo "geni-hint: then: git -C '$root' merge --ff-only '$branch'" >&2
        return 1
    fi

    # Leave the worktree before removing it: a shell whose directory has
    # been deleted is a nuisance for everything typed after it.
    cd "$start" || cd "$root" || return 1

    if ! git -C "$root" worktree remove "$wt"; then
        echo "geni-warning: the worktree is still there: $wt" >&2
        echo "geni-hint: git -C '$root' worktree remove --force '$wt'" >&2
    fi
    git -C "$root" branch -d "$branch" >/dev/null 2>&1 \
        || echo "geni-hint: the branch is still there: $branch" >&2

    # Say so and stop.  TODO: when the run happens in a window of its own,
    # this is the last line and the window should close itself instead of
    # waiting for the user, the way `--hold` in launch leaves it now.  Until
    # then the shell that called geni is the one to tell.
    echo 'geni succeeded'
    return 0
}

# TODO: run all of the above in a window of its own, so that a geni run is
# something to leave and come back to instead of a command that holds the
# shell.  Nothing above assumes the foreground -- the shell stays in the
# worktree, the branch is the return value, the merge is one command -- so
# what is missing is only the opening of the window.  For kitty that is
#
#     kitty @ launch --type=os-window --keep-focus --title="geni: $id" \
#         -- bash -lc "cd '$wt' && geni ..."
#
# The window is opened by `launch`, so the terminal-specific part lives
# there and not here.  One hazard belongs here anyway.  Opening a window
# needs `allow_remote_control socket-only` in kitty.conf, which makes the
# control socket a unix socket owned by the user and not a port anything on
# the network can reach.  KITTY_LISTEN_ON names that socket, and every
# process the terminal starts inherits it: a `dic --tools ...` that shells
# out would find it and could `kitty @ send-text` a command into a window
# the user is typing in.  dic has to unset it for its own children before
# it runs a tool.  `launch` copies it on purpose, because a window that
# cannot open a window of its own could not run `launch`.
#
# TODO: run the other agents in the same worktree, after itera and before
# the merge is decided, so that one request is one branch and one review.
