#!/usr/bin/env bats
#
# Tests for scripts/sandbox.sh.  bwrap is faked, so what is checked is the
# argument list sandbox() hands it -- the whole contract, since bwrap is what
# does the jailing -- and the syscall filter, which is compiled from the C
# beside the script and is the one part of the sandbox that is not an
# argument.  See tests/bin/bwrap for why a real jail is not made here.

setup() {
    source "$BATS_TEST_DIRNAME/../../scripts/sandbox.sh"

    export FAKE_BWRAP_LOG="$BATS_TEST_TMPDIR/bwrap-args"
    : > "$FAKE_BWRAP_LOG"

    repo="$BATS_TEST_TMPDIR/repo"
    mkdir -p "$repo"
    cd "$repo"
}

# Hand every bwrap invocation to the fake.  A real jail cannot be made from
# inside the jail `sandbox pytest` is: the filter the outer sandbox installs
# refuses unshare and mount, and those are what a jail is made of.
fake_bwrap() {
    export PATH="$BATS_TEST_DIRNAME/../bin:$PATH"
    export SANDBOX_SECCOMP_BLOB=/dev/null
}

# Whether bwrap was handed this exact word.
given() { grep -qxF -- "$1" "$FAKE_BWRAP_LOG"; }

@test "every kind of shared kernel object is unshared" {
    fake_bwrap
    run sandbox -- true
    [ "$status" -eq 0 ]
    given --die-with-parent
    given --new-session
    for flag in user ipc pid uts cgroup net; do
        given "--unshare-$flag"
    done
    given --cap-drop
    given ALL
}

@test "the environment is emptied and only the loader's own names go back" {
    fake_bwrap
    run sandbox -- true
    given --clearenv
    given --setenv
    given HOME
    given /tmp
    given PATH
    given "$PATH"
}

@test "the tree the shell stands in is the only writable path" {
    fake_bwrap
    run sandbox -- true
    given --bind
    given "$PWD"
}

@test "the credentials under /etc stay outside" {
    fake_bwrap
    run sandbox -- true
    ! given /etc/shadow
    ! given /etc/ssh
}

@test "a caller's arguments come after the filter" {
    fake_bwrap
    run sandbox --share-net -- true
    # the line numbers, with no brace of their own: the braces that open and
    # close a test are what tells the bats reader where its body ends
    seccomp=$(grep -nx -- --seccomp "$FAKE_BWRAP_LOG" | cut -d: -f1)
    share=$(grep -nx -- --share-net "$FAKE_BWRAP_LOG" | cut -d: -f1)
    [ -n "$seccomp" ]
    [ -n "$share" ]
    [ "$seccomp" -lt "$share" ]
}

@test "the syscall filter is compiled from the source beside the script" {
    unset SANDBOX_SECCOMP_BLOB
    export XDG_CACHE_HOME="$BATS_TEST_TMPDIR/cache"
    blob=$(sandbox-seccomp-blob)
    [ -s "$blob" ]
}
