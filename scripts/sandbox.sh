#!/bin/bash
#
# sandbox.sh -- run a command in a namespaced, read-only jail.
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
#     bind-mounted read-write.
#
#   * A fresh /proc and a minimal /dev.
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
usage: sandbox2 [flags] [--] CMD [args...]

Run CMD inside a namespaced, read-only jail.  If CMD is omitted, $SHELL
is used.

flags:
  --rw PATH[:DST]    writable view of PATH, mounted at DST
                     (default DST = PATH).  Never a bind mount; writes
                     go to a tmpfs upper layer that dies with the
                     sandbox.
  --ro PATH[:DST]    additional read-only bind of PATH at DST.
  --overlay PATH[:DST]
                     alias for --rw.
  --cwd DIR          working directory inside the sandbox.
                     Default: $PWD.
  --net=MODE         network policy: none (default) | host.
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
    none|host) ;;
    live) echo "sandbox-error: --net=live is not implemented" >&2; exit 2 ;;
    *) echo "sandbox-error: unknown --net mode: $net_mode" >&2; exit 2 ;;
esac

if ! command -v bwrap >/dev/null 2>&1; then
    echo "sandbox-error: bubblewrap(1) not found" >&2
    exit 1
fi

# ----------------------------------------------------------------------------
# banner: say out loud which walls came down
# ----------------------------------------------------------------------------

if [[ "$net_mode" != none ]]; then
    echo "sandbox: network=$net_mode" >&2
fi
for spec in "${rw_dirs[@]}" "${overlay_dirs[@]}"; do
    echo "sandbox: writable view of $spec (upper layer is tmpfs)" >&2
done
for spec in "${ro_dirs[@]}"; do
    echo "sandbox: extra read-only bind of $spec" >&2
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

cwd=${cwd:-$PWD}

sandbox-dst() {
    local spec="$1" dst
    dst="${spec#*:}"
    if [[ "$dst" == "$spec" ]]; then dst="$spec"; fi
    if [[ "$dst" != /* ]]; then dst="/$dst"; fi
    printf '%s' "$dst"
}

# ----------------------------------------------------------------------------
# the jail
# ----------------------------------------------------------------------------
#
# bwrap applies options left to right, so the ordering below is the
# policy: the host first, read-only; then the fresh writable stacks
# that shadow its shared paths; then the caller's own escapes.

declare -a bargs=(
    --die-with-parent
    --new-session
    --unshare-user
    --unshare-ipc
    --unshare-pid
    --unshare-uts
    --unshare-cgroup
)
if [[ "$net_mode" == none ]]; then
    bargs+=(--unshare-net)
else
    bargs+=(--share-net)
fi

bargs+=(
    --ro-bind / /
    --proc /proc
    --dev /dev
    --tmpfs /dev/shm
    --tmpfs /tmp
    --tmpfs /var/tmp
    --tmpfs /run
)

for spec in "${ro_dirs[@]}"; do
    src=$(realpath -- "${spec%%:*}")
    bargs+=(--ro-bind "$src" "$(sandbox-dst "$spec")")
done

for spec in "${rw_dirs[@]}" "${overlay_dirs[@]}"; do
    src=$(realpath -- "${spec%%:*}")
    bargs+=(--overlay "$src" "$(sandbox-dst "$spec")")
done

bargs+=(--clearenv --setenv HOME /tmp)
for kv in "${env_args[@]}"; do
    bargs+=(--setenv "${kv%%=*}" "${kv#*=}")
done

bargs+=(--chdir "$cwd" --)
bargs+=("${cmd[@]}")

exec bwrap "${bargs[@]}"
