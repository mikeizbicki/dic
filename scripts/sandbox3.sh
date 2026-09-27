#!/bin/bash
#
# sandbox3.sh -- a thin wrapper around bwrap(1).
#
# Every argument this script is given is a bwrap argument.  It runs
#
#     bwrap <the default arguments below> "$@"
#
# and bwrap applies its arguments from left to right, so the last one wins:
# the default is a jail that sees almost nothing, and each argument the
# caller adds is an exception to it.  There is no flag of our own.
#
# The default jail sees only what a program needs to start:
#
#   * /usr, and the files in /etc that describe the filesystem, read-only.
#   * fresh /proc, /dev, /tmp, /var/tmp, /run, /dev/shm.
#   * $PWD, read-write, which is the only writable path.
#
# Everything else is not bound at all.  /home, /root, /srv, /mnt, /opt,
# /boot, /var and /sys are missing from the map, so an ssh key is absent
# rather than hidden behind a permission bit.
#
# To see more of the host, name it:
#
#     sandbox3.sh --ro-bind /home/me/.gitconfig /home/me/.gitconfig \
#                 -- git log
#     sandbox3.sh --bind /home/me/src /home/me/src -- make -C /home/me/src
#     sandbox3.sh --share-net -- cargo build
#     sandbox3.sh --ro-bind / / -- less /etc/hostname
#     sandbox3.sh --ro-bind "$PWD" "$PWD" -- make check
#
# The last two are the big ones: `--ro-bind / /` exposes the whole host
# read-only, and rebinding $PWD read-only takes the write back.  Without a
# command, bwrap runs a shell.

set -euo pipefail

args=(
    --die-with-parent
    --new-session
    --unshare-user
    --unshare-ipc
    --unshare-pid
    --unshare-uts
    --unshare-cgroup
    --unshare-net
    --cap-drop ALL
)

# The loader and the tools.  /bin, /sbin, /lib* are symlinks into /usr on a
# merged-usr system and are remade as symlinks here.
[[ -e /usr ]] && args+=(--ro-bind /usr /usr)
for p in /bin /sbin /lib /lib32 /lib64 /libx32; do
    if [[ -L $p ]]; then
        args+=(--symlink "$(readlink "$p")" "$p")
    elif [[ -e $p ]]; then
        args+=(--ro-bind "$p" "$p")
    fi
done

# Just enough /etc that names, hosts and certificates resolve; a credential
# in /etc is not bound.
for p in /etc/alternatives /etc/bash.bashrc /etc/ca-certificates \
         /etc/ca-certificates.conf /etc/fonts /etc/gai.conf /etc/group \
         /etc/host.conf /etc/hosts /etc/inputrc /etc/ld.so.cache \
         /etc/ld.so.conf /etc/ld.so.conf.d /etc/localtime /etc/mime.types \
         /etc/nsswitch.conf /etc/os-release /etc/passwd /etc/pki \
         /etc/profile /etc/profile.d /etc/protocols /etc/resolv.conf \
         /etc/services /etc/ssl /etc/terminfo /etc/timezone; do
    [[ -e $p ]] && args+=(--ro-bind "$p" "$p")
done

args+=(--proc /proc --dev /dev)
args+=(--tmpfs /tmp --tmpfs /var/tmp --tmpfs /run --tmpfs /dev/shm)
args+=(--bind "$PWD" "$PWD")

# The environment is emptied, so an exported API key is not a variable the
# payload can read.  --setenv puts one back.
args+=(--clearenv --setenv HOME /tmp)
for p in PATH TERM LANG LC_ALL TZ; do
    [[ -v $p ]] && args+=(--setenv "$p" "${!p}")
done

exec bwrap "${args[@]}" "$@"
