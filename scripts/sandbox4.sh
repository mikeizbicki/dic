#!/bin/bash
#
# sandbox4.sh -- a thin wrapper around bwrap(1), sourced into the shell.
#
# Source this file and it defines one function, `sandbox`.  Every argument
# you give that function is an argument to bwrap(1):
#
#     source scripts/sandbox4.sh
#     sandbox -- make -j
#     sandbox --ro-bind /home/me/.gitconfig /home/me/.gitconfig -- git log
#     sandbox --bind /home/me/src /home/me/src -- make -C /home/me/src
#     sandbox --share-net -- cargo build
#
# bwrap applies its arguments from left to right and the last one wins, so
# the function lays down a strict default and you add back exactly the path
# you need.  The function has no flags of its own, so there is nothing here
# to learn that is not already bwrap(1).
#
# The default is a jail that sees almost nothing.  The point is that the
# files worth stealing -- ~/.ssh, ~/.aws, ~/.config/gh, a database socket
# -- are not in the jail's map at all, instead of sitting in it behind a
# permission bit: a file that was never mounted cannot be read, so it
# cannot be exfiltrated by a model that reads what a tool printed here.
#
# Sourcing this file defines the function and does nothing else.  It sets
# no shell option, exports no variable and runs no command.

sandbox() {
    # `local -a` gives this call its own array, so two calls cannot collide
    # and a shell running `set -u` is unaffected.  A bash array passes its
    # elements to bwrap as separate words, so a path with a space in it is
    # still one argument and not two.
    local -a args=(

        # Kill the sandbox when the shell that started it goes away.
        # Without this a sandbox outlives the terminal that launched it and
        # keeps running whatever it was told to do.
        --die-with-parent

        # Give the sandbox a session of its own.  Otherwise a program
        # inside it can inject keystrokes into the parent terminal with
        # TIOCSTI and make the shell outside the jail type a command.
        --new-session

        # Each --unshare-* gives the payload a private copy of one kind of
        # kernel object.  Because the two sides cannot see each other there
        # is no shared object left to meet at, so the only way in or out is
        # a path mounted on purpose, below.
        #
        # user: inside this namespace the payload can be "root" for its own
        # housekeeping, but that identity does not exist outside it, so it
        # cannot touch a host process or file that was not mounted for it.
        --unshare-user
        # ipc: no SysV shared memory, semaphore or message queue shared with
        # a host process; /dev/shm is a fresh tmpfs further down.
        --unshare-ipc
        # pid: the payload sees only itself, so it cannot list, signal or
        # inspect a process outside.  bwrap is PID 1 of this namespace, and
        # that is also what reaps the payload's orphans.
        --unshare-pid
        # uts: it may call sethostname and nobody outside notices.
        --unshare-uts
        # cgroup: it cannot see the host's cgroup tree.
        --unshare-cgroup
        # net: a network stack with no route off the machine.  A server
        # started here is reachable by nobody and nothing here can dial out.
        # `--share-net` is how you ask for the host network instead.
        --unshare-net

        # Start the payload with no capabilities.  This is belt and braces
        # after --unshare-user: even as root of its own namespace it cannot
        # mount, load a module or set the clock.
        --cap-drop ALL
    )

    # Bind in the loader and the tools.  On a merged-usr system /bin, /sbin
    # and /lib* are symlinks into /usr, so they are recreated as symlinks
    # rather than bound: binding through a symlink puts the same bytes in
    # the map twice, in two different roles.
    [[ -e /usr ]] && args+=(--ro-bind /usr /usr)
    for p in /bin /sbin /lib /lib32 /lib64 /libx32; do
        if [[ -L $p ]]; then
            args+=(--symlink "$(readlink "$p")" "$p")
        elif [[ -e $p ]]; then
            args+=(--ro-bind "$p" "$p")
        fi
    done

    # Just enough of /etc that names resolve, hosts are known and TLS
    # certificates verify.  This list is not a blanket bind of /etc on
    # purpose: /etc/ssh, /etc/shadow and whatever credential a package left
    # there stay outside.  A program that needs one of them is told to
    # mount it by name, which is a decision the caller can see.
    for p in /etc/alternatives /etc/bash.bashrc /etc/ca-certificates \
             /etc/ca-certificates.conf /etc/fonts /etc/gai.conf \
             /etc/group /etc/host.conf /etc/hosts /etc/inputrc \
             /etc/ld.so.cache /etc/ld.so.conf /etc/ld.so.conf.d \
             /etc/localtime /etc/mime.types /etc/nsswitch.conf \
             /etc/os-release /etc/passwd /etc/pki /etc/profile \
             /etc/profile.d /etc/protocols /etc/resolv.conf \
             /etc/services /etc/ssl /etc/terminfo /etc/timezone; do
        [[ -e $p ]] && args+=(--ro-bind "$p" "$p")
    done

    # A fresh, minimal /proc and /dev.  The host's /proc names every process
    # outside and the host's /dev carries every device node, so both are
    # replaced rather than bound.  bwrap fills them with what a program
    # needs to start.
    args+=(--proc /proc --dev /dev)

    # The writable places a program assumes exist.  Each one is an empty
    # filesystem mounted over whatever the host had there, so writes go to
    # memory that dies with the sandbox and never reach a disk.  /run is
    # where a daemon would put a socket, /dev/shm where two processes would
    # share memory, and /tmp where anything leaves a scratch file.
    args+=(--tmpfs /tmp --tmpfs /var/tmp --tmpfs /run --tmpfs /dev/shm)

    # The one path that is writable, and it is the directory the shell is
    # already in.  bwrap creates the missing parents inside the jail, so
    # /home/me/proj appears on its own and the rest of /home/me does not.
    args+=(--bind "$PWD" "$PWD")

    # Empty the environment, then put back only what a program needs to find
    # itself.  An exported API key is not a variable the payload can read
    # here; --setenv K=V puts one back when a build actually needs it.
    args+=(--clearenv --setenv HOME /tmp)
    for p in PATH TERM LANG LC_ALL TZ; do
        [[ -v $p ]] && args+=(--setenv "$p" "${!p}")
    done

    # The caller's arguments come last, so they are the exceptions to
    # everything above, and bwrap runs in a subshell: `exec` then replaces
    # that subshell and not the shell which sourced this file, which would
    # otherwise end the terminal session.
    ( exec bwrap "${args[@]}" "$@" )
}
