# This file defines `geni`, the outermost of the coding agents.
#
# `committe` turns one request into one commit, and `itera` repeats that
# until the tests pass.  Both edit the tree the shell is standing in, so
# every failed round lands on the user.  `geni` gives them a tree of their
# own: a new branch off HEAD, checked out in a worktree outside the
# repository, with `itera` run inside it.  Only a green run is merged back,
# as a fast-forward onto the branch the shell started in, and only then is
# the worktree removed.  A run that goes badly leaves the user's own
# checkout untouched and the shell inside the worktree, where the failing
# tests and the commit that did not work are.
#
# The merge is a fast-forward and nothing else, so the tree that arrives is
# byte for byte the tree that passed the tests.  Nothing here resolves a
# conflict, because a conflict is a decision and the user is the one who
# makes it.
#
# Sourcing this file defines the function and does nothing else.  `itera`
# must already be sourced: the work in the worktree is its.

geni-usage() {
    cat <<'EOF'
usage: geni [flags] [REQUEST...]

Run itera on a git worktree of its own, and merge the branch back if the
tests pass.

A new branch off HEAD is checked out in a worktree outside the repository,
the shell moves there, and REQUEST is passed to itera unchanged.  On
success the branch is fast-forwarded onto the branch the shell started in,
the worktree is removed, and the shell returns to where it was.  On failure
the shell stays in the worktree, with the unfinished branch and the test
output in front of it.

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

    # Remember where the shell was, because success has to put it back.
    # Failure is the one case it is not put back: the worktree is the thing
    # to look at then.
    local start=$PWD
    cd "$wt" || return 1

    printf 'geni: %s on %s\n' "$wt" "$branch" >&2

    # The worktree is clean by construction, so committe's own check that
    # the tree it is about to edit is clean is never forced past here.
    if ! itera "${request[@]}"; then
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
# which needs `allow_remote_control socket-only` in kitty.conf: the control
# socket is then a unix socket owned by the user, and not a port anything
# on the network can reach.  KITTY_LISTEN_ON names that socket, and it is
# inherited by every process the terminal starts, which is the whole
# hazard: a `dic --tools ...` that shells out would find it and could
# `kitty @ send-text` a command into a window the user is typing in.  The
# window opened here must therefore be started with that variable unset,
# and dic must unset it for its own children before it runs a tool.
#
# A window that closes the moment geni returns takes the failing run with
# it, so the command the window runs ends in an interactive shell, which
# is where a failed run wants the user anyway.  A shell that cannot open
# a window -- no kitty, no socket, a terminal kitty did not start -- falls
# back to running geni in the foreground, which is what this file does.
#
# TODO: run the other agents in the same worktree, after itera and before
# the merge is decided, so that one request is one branch and one review.
