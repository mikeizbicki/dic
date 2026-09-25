#!/bin/bash
#
# sandbox.sh -- run a command in a namespaced, read-only jail.
#
# The command is never inspected.  A tool named `unsafe-*` runs exactly
# like any other tool; safety is a property of the environment, not of
# argv.
#
# The environment is:
#
#   * Every namespace that owns a shared kernel object is unshared:
#     user, mount, uts, ipc, pid, cgroup, and net.  Two sandboxes have
#     no shared object to rendezvous through: no abstract unix sockets
#     (no net ns), no SysV shm (no ipc ns), no shared /proc (no pid
#     ns), no shared mount table, no shared /dev/shm.
#
#   * The host filesystem is bound in read-only, then fresh tmpfs
#     mounts are stacked over /tmp, /var/tmp, /run, /dev, and
#     /dev/shm.  Writable state lives only in the private mount
#     namespace and is destroyed with it.  A host path is never
#     bind-mounted read-write; --rw copies the tree into a tmpfs.
#
#   * A fresh /proc and a minimal /dev, built from bind mounts of the
#     host's character devices.
#
# Every relaxation of the default policy prints a banner to stderr
# naming the wall that came down.

set -euo pipefail

# ----------------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------------

declare -a rw_dirs=()
declare -a ro_dirs=()
declare -a overlay_dirs=()
declare -a extra_env=()
declare -a pass_env=(PATH TERM LANG LC_ALL TZ)
declare -a cmd=()
net_mode=none
cwd=

sandbox-usage() {
    cat <<'EOF'
usage: sandbox [flags] [--] CMD [args...]

Run CMD inside a namespaced, read-only jail.  If CMD is omitted, $SHELL
is used.

flags:
  --rw PATH[:DST]    writable copy of PATH, mounted at DST
                     (default DST = PATH).  Never a bind mount; the
                     tree is copied into a fresh tmpfs.
  --ro PATH[:DST]    additional read-only bind of PATH at DST.
  --overlay PATH[:DST]
                     PATH is a read-only lower layer; every write lands
                     in a tmpfs upper layer that dies with the sandbox.
                     O(1) in the size of PATH, unlike --rw.
  --cwd DIR          working directory inside the sandbox.
                     Default: $PWD.
  --net=MODE         network policy: none (default) | live | host.
  --env K=V          set K=V in the sandbox environment.
  --pass-env K       forward K from the host environment (repeatable).
  -h, --help         show this help.
EOF
}

while (( $# )); do
    case "$1" in
        -h|--help)  sandbox-usage; exit 0 ;;
        --)         shift; cmd=("$@"); break ;;
        --rw)       rw_dirs+=("$2"); shift 2 ;;
        --ro)       ro_dirs+=("$2"); shift 2 ;;
        --overlay)  overlay_dirs+=("$2"); shift 2 ;;
        --cwd)      cwd="$2"; shift 2 ;;
        --env)      extra_env+=("$2"); shift 2 ;;
        --pass-env) pass_env+=("$2"); shift 2 ;;
        --net)      net_mode="$2"; shift 2 ;;
        --net=*)    net_mode="${1#--net=}"; shift ;;
        -*)         echo "sandbox-error: unknown flag: $1" >&2; exit 2 ;;
        *)          cmd=("$@"); break ;;
    esac
done

if (( ${#cmd[@]} == 0 )); then
    cmd=("${SHELL:-/bin/bash}")
fi

case "$net_mode" in
    none|live|host) ;;
    *) echo "sandbox-error: unknown --net mode: $net_mode" >&2; exit 2 ;;
esac

if ! command -v unshare >/dev/null 2>&1; then
    echo "sandbox-error: unshare(1) not found" >&2
    exit 1
fi

# ----------------------------------------------------------------------------
# banner: say out loud which walls came down
# ----------------------------------------------------------------------------

if [[ "$net_mode" != none ]]; then
    echo "sandbox: network=$net_mode" >&2
fi
for spec in "${rw_dirs[@]}"; do
    echo "sandbox: writable copy of $spec" >&2
done
for spec in "${ro_dirs[@]}"; do
    echo "sandbox: extra read-only bind of $spec" >&2
done
for spec in "${overlay_dirs[@]}"; do
    echo "sandbox: writable overlay of $spec (upper layer is tmpfs)" >&2
done

# ----------------------------------------------------------------------------
# environment
# ----------------------------------------------------------------------------

declare -a env_args=()
for k in "${pass_env[@]}"; do
    if [[ -v "$k" ]]; then
        env_args+=("$k=${!k}")
    fi
done
for kv in "${extra_env[@]}"; do
    env_args+=("$kv")
done

# ----------------------------------------------------------------------------
# staging directory
# ----------------------------------------------------------------------------

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/sandbox.XXXXXXXXXX")
trap 'rm -rf -- "$STAGE"' EXIT
cwd=${cwd:-$PWD}

# ----------------------------------------------------------------------------
# build the inner script
# ----------------------------------------------------------------------------

inner=$(
    printf 'STAGE=%q\n' "$STAGE"
    printf 'CWD=%q\n'   "$cwd"
    printf 'RW_DIRS=(';  printf '%q ' "${rw_dirs[@]}";  printf ')\n'
    printf 'RO_DIRS=(';  printf '%q ' "${ro_dirs[@]}";  printf ')\n'
    printf 'OVERLAY_DIRS=('; printf '%q ' "${overlay_dirs[@]}"; printf ')\n'
    printf 'ENV_ARGS=('; printf '%q ' "${env_args[@]}"; printf ')\n'
    cat <<'INNER_SCRIPT'
# ---- fresh root ------------------------------------------------------------

mkdir -p "$STAGE/root"
mount -t tmpfs -o mode=0755 sandbox-root "$STAGE/root"

# ---- host filesystem, read-only --------------------------------------------

mount --rbind / "$STAGE/root"
mount --make-rslave "$STAGE/root"

# Pre-create every mountpoint while the tree is still writable.
mkdir -p "$STAGE/root/tmp" "$STAGE/root/var/tmp" "$STAGE/root/run"
mkdir -p "$STAGE/root/proc" "$STAGE/root/dev" "$STAGE/root/.oldroot"

sandbox-dst() {
    local spec="$1" dst
    dst="${spec#*:}"
    if [[ "$dst" == "$spec" ]]; then dst="$spec"; fi
    if [[ "$dst" != /* ]]; then dst="/$dst"; fi
    printf '%s' "$dst"
}

for spec in "${RO_DIRS[@]}" "${RW_DIRS[@]}" "${OVERLAY_DIRS[@]}"; do
    mkdir -p "$STAGE/root$(sandbox-dst "$spec")"
done

# Remount the top of the tree read-only, and every submount with it.
mount -o remount,bind,ro "$STAGE/root"
mount --make-rslave "$STAGE/root"

if command -v findmnt >/dev/null 2>&1; then
    while IFS= read -r mnt; do
        if [[ "$mnt" == / ]]; then
            continue
        fi
        mount -o remount,bind,ro "$STAGE/root$mnt" 2>/dev/null || true
    done < <(findmnt -R -n -o TARGET --target /)
fi

# ---- fresh tmpfs over the shared user-space paths --------------------------

mount -t tmpfs -o mode=1777 sandbox-tmp    "$STAGE/root/tmp"
mount -t tmpfs -o mode=1777 sandbox-vartmp "$STAGE/root/var/tmp"
mount -t tmpfs -o mode=0755 sandbox-run    "$STAGE/root/run"

# ---- fresh /dev ------------------------------------------------------------

mount -t tmpfs -o mode=0755 sandbox-dev "$STAGE/root/dev"
mkdir -p "$STAGE/root/dev/pts" "$STAGE/root/dev/shm"
chmod 1777 "$STAGE/root/dev/shm"
mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts "$STAGE/root/dev/pts"
ln -sf pts/ptmx "$STAGE/root/dev/ptmx"
for n in null zero full random urandom tty; do
    if [[ -e "/dev/$n" ]]; then
        touch "$STAGE/root/dev/$n"
        mount --bind "/dev/$n" "$STAGE/root/dev/$n"
    fi
done

# ---- fresh /proc -----------------------------------------------------------

mount -t proc -o nosuid,nodev,noexec proc "$STAGE/root/proc"

# ---- user read-only binds --------------------------------------------------

for spec in "${RO_DIRS[@]}"; do
    src="${spec%%:*}"
    dst=$(sandbox-dst "$spec")
    src=$(realpath -- "$src")
    mount --bind "$src" "$STAGE/root$dst"
    mount -o remount,bind,ro "$STAGE/root$dst"
done

# ---- user read-write copies (never a bind mount) ---------------------------

for spec in "${RW_DIRS[@]}"; do
    src="${spec%%:*}"
    dst=$(sandbox-dst "$spec")
    src=$(realpath -- "$src")
    mount -t tmpfs sandbox-rw "$STAGE/root$dst"
    cp -a --reflink=auto -- "$src/." "$STAGE/root$dst/"
done

# ---- user writable overlays ------------------------------------------------
#
# PATH is the lower layer and is never written; every write lands in a
# tmpfs upper layer inside the namespace and is destroyed with it.  This
# is the cheap answer for a tree too large to copy: the mount costs the
# same whether the lower layer holds one file or a million.
#
# Overlayfs in a user namespace cannot use the trusted. xattr namespace,
# so it must be told to use user. instead; that is what userxattr means.
# The upper and work layers live on the fresh /var/tmp tmpfs because they
# must share a filesystem, and because a workdir that outlived the
# sandbox would be garbage nobody collected.

_n=0
for spec in "${OVERLAY_DIRS[@]}"; do
    src="${spec%%:*}"
    dst=$(sandbox-dst "$spec")
    src=$(realpath -- "$src")
    _n=$((_n + 1))
    upper="$STAGE/root/var/tmp/sandbox-upper-$_n"
    work="$STAGE/root/var/tmp/sandbox-work-$_n"
    mkdir -p "$upper" "$work"
    if ! mount -t overlay overlay \
        -o "lowerdir=$src,upperdir=$upper,workdir=$work,userxattr" \
        "$STAGE/root$dst"; then
        echo "sandbox-error: overlay of $spec failed" >&2
        echo "sandbox-hint: overlayfs in a user namespace needs kernel >= 5.11" >&2
        exit 1
    fi
done

# ---- pivot into the new root -----------------------------------------------

cd "$STAGE/root"
pivot_root . .oldroot
cd /
umount -l /.oldroot 2>/dev/null || true
rmdir /.oldroot 2>/dev/null || true

# ---- environment and exec --------------------------------------------------

mkdir -p /tmp/sandbox-home
cd "$CWD"
exec env -i HOME=/tmp/sandbox-home "${ENV_ARGS[@]}" "$@"
INNER_SCRIPT
)

# ----------------------------------------------------------------------------
# namespaces and run
# ----------------------------------------------------------------------------

declare -a ns_opts=(--user --map-root-user --mount --uts --ipc --pid --fork --cgroup)
if [[ "$net_mode" == none ]]; then
    ns_opts+=(--net)
fi

unshare "${ns_opts[@]}" bash -c "$inner" sandbox "${cmd[@]}"
