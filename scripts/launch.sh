# This file defines `launch`, which runs a command in a window of its own.
#
# `launch CMD ARGS` opens a new terminal window running CMD ARGS.  A shell
# with no window to open is an error, unless LAUNCH_INPLACE is set, because
# running the command where the shell stands looks the same as a window that
# opened elsewhere and is worse than saying so.
#
# Everything the command needs is copied at spawn time, and nothing is
# passed back afterwards.  The two windows share no channel: there is no
# protocol to reimplement when another terminal (tmux, foot, ...) is added,
# and there is no state to reason about when the new window outlives the
# one it came from.  `launch dic ...` therefore starts a conversation
# somewhere that stays put, and `geni ...` run in that window does its work
# there, with neither window knowing about the other.
#
# The window is given a background shifted away from the terminal's own, so
# a window this opened is visible at a glance and a command cannot mistake
# it for one the user opened.  The color is read from the terminal the
# command runs in and never from the parent's, which is what keeps the two
# windows independent.
#
# Sourcing this file defines the functions and does nothing else.

launch-usage() {
    cat <<'EOF'
usage: launch [flags] [--] CMD [ARGS...]

Run CMD ARGS in a window of its own, started with this shell's environment
and in this shell's directory.  A terminal `launch` cannot open a window in
is an error: running the command where the shell stands looks the same as a
window and is worse than saying so.  LAUNCH_INPLACE=1 asks for it anyway.

flags:
  --banner TEXT   print TEXT between ruled lines before CMD runs.
  --title TEXT    the new window's title.
  -h, --help      show this help.

The first `--` ends launch's flags; otherwise the first argument that is
not one starts the command, and everything from there on goes to CMD
unchanged.
EOF
}

# A banner: the text between two rules as wide as the window.
launch-banner() {
    local text=$1 width=${COLUMNS:-80} rule
    printf -v rule '%*s' "$width" ''
    rule=${rule// /=}
    printf '%s\n%s\n%s\n' "$rule" "$text" "$rule"
}

# The terminal's own background, as #rrggbb, asked of it with the OSC 11
# query.  Only the terminal being talked to answers, and only its own color
# comes back, so no other window is consulted and no other window's color
# is read.  Failing is ordinary: a terminal that does not answer, or one
# that is not a terminal, leaves the color alone.
launch-read-bg() {
    local saved reply
    [[ -c /dev/tty ]] || return 1
    saved=$(stty -g </dev/tty 2>/dev/null) || return 1
    stty raw -echo </dev/tty 2>/dev/null || return 1
    printf '\033]11;?\033\\' >/dev/tty
    IFS= read -r -t 0.5 -d '\' reply </dev/tty
    stty "$saved" </dev/tty 2>/dev/null
    [[ $reply =~ 11\;rgb:([0-9a-fA-F]{2})[0-9a-fA-F]*/([0-9a-fA-F]{2})[0-9a-fA-F]*/([0-9a-fA-F]{2})[0-9a-fA-F]* ]] \
        || return 1
    printf '#%s%s%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
}

# Shift a #rrggbb color away from its own luminance, so a light theme gets
# a shade darker than white and a dark one a shade lighter than black.  It
# is a shift and not a scale, because a scale leaves white at white: the
# point is a background that is plainly this window's own.
launch-tint() {
    local hex=${1#\#} r g b lum step c
    r=$((16#${hex:0:2})); g=$((16#${hex:2:2})); b=$((16#${hex:4:2}))
    lum=$(( (2126*r + 7152*g + 722*b) / 10000 ))
    (( lum > 128 )) && step=-18 || step=18
    c=$((r + step)); (( c < 0 )) && c=0; (( c > 255 )) && c=255; r=$c
    c=$((g + step)); (( c < 0 )) && c=0; (( c > 255 )) && c=255; g=$c
    c=$((b + step)); (( c < 0 )) && c=0; (( c > 255 )) && c=255; b=$c
    printf '\033]11;#%02x%02x%02x\033\\' "$r" "$g" "$b"
}

# Everything the new window does to look like a window `launch` opened: its
# title and its background, and nothing the command can see.  A title is an
# escape the window writes to itself, so there is no terminal-specific
# option to get wrong.  It writes to /dev/tty, so a command whose output is
# redirected still gets a decorated window.
launch-decorate() {
    local title=$1 bg
    [[ -n $title ]] && printf '\033]2;%s\033\\' "$title" >/dev/tty
    if bg=$(launch-read-bg); then
        launch-tint "$bg" >/dev/tty
    fi
}

# The terminal this shell runs under, as $TERM names it.  $TERM is the
# variable every program has: a login sets it and every emulator keeps it,
# so the emulator names itself here without a socket having to exist,
# without remote control having to be on, and without this file asking the
# emulator anything.  A missing binary is enough to know a window cannot
# open, so the emulator is looked up before it is believed.
#
# NOTE: $TERM is the emulator and not the instance.  Two kitty windows in
# one kitty process, and two kitty processes on one machine, both report
# xterm-kitty, so this says which program to ask for a window and not which
# one to ask.  The socket this replaces said which one, and the price of
# dropping it is a window that may land in a different instance than the
# shell that asked for it.
#
# NOTE: $TERM crosses ssh, a window does not.  A remote shell reports the
# remote terminal but draws on no display of its own, so nothing it runs
# can open a local window, and the socket was no more reachable from there.
# tmux is handled apart for the same reason and not as an exception: tmux
# is client and server already, so a window in it is an object inside the
# session and not a window on the local display.
#
# TODO: the emulator and the instance are told apart by asking the
# emulator, which is what remote control is for.  Use the socket when one
# is there and fall back to $TERM when it is not, instead of choosing one.
# TODO: wezterm, foot, alacritty and a plain X terminal each have a way to
# be asked for a window and are left out until one is in front of someone.
launch-backend() {
    local cmd
    case ${TERM:-} in
        xterm-kitty*) cmd=kitty ;;
        tmux*)        cmd=tmux ;;
        *)            return ;;
    esac
    command -v "$cmd" >/dev/null 2>&1 || return
    printf '%s' "$cmd"
}

# Every exported variable as NAME=VALUE, NUL-separated so that a value
# holding a newline survives the trip.
launch-env() {
    local name
    for name in $(compgen -e); do
        printf '%s=%s\0' "$name" "${!name}"
    done
}

function launch() {
    local banner= title=
    local -a cmd=()
    while (( $# )); do
        case $1 in
            -h|--help) launch-usage; return 0 ;;
            --banner)  banner=$2; shift 2 ;;
            --title)   title=$2; shift 2 ;;
            --)        shift; cmd=("$@"); break ;;
            *)         cmd=("$@"); break ;;
        esac
    done
    if (( ${#cmd[@]} == 0 )); then
        launch-usage >&2
        return 2
    fi

    local backend
    backend=$(launch-backend)
    if [[ -z $backend ]]; then
        # No window to open: $TERM names a terminal this does not know how
        # to ask, or the binary that would ask it is not installed.
        # Running the command here looks exactly like a window that opened
        # elsewhere and is worse than failing, so it is refused unless
        # LAUNCH_INPLACE asks for it.
        if [[ -z ${LAUNCH_INPLACE:-} ]]; then
            echo 'launch-error: no window to open' >&2
            echo 'launch-hint: LAUNCH_INPLACE=1 to run where the shell stands' >&2
            return 1
        fi
        "${cmd[@]}"
        return
    fi

    # What the new window runs: the decoration, the banner, and then the
    # command.  It is one line for one shell, so every word is quoted for
    # that shell; the command is a word like any other and needs no special
    # case.
    #
    # The helpers are written into the line as definitions and not called
    # by name, because the shell that reads the line is a fresh one that
    # has not sourced this file: it knows the interactive rc and nothing
    # else, so a bare `launch-decorate` is a command it does not have.
    # Sending the definitions along makes the line stand alone however the
    # new shell was started.
    local line
    line=$(declare -f launch-decorate launch-read-bg launch-tint launch-banner)
    line+=$'\n'"launch-decorate $(printf %q "$title")"
    [[ -n $banner ]] && line+=$'\n'"launch-banner $(printf %q "$banner")"

    # `exec` so that the window runs the command itself and not the command
    # inside a second shell.  Without it the line ends in a shell, the
    # shell is interactive because the outer one was started with -i, and
    # `exit` at the prompt returns to the outer shell instead of ending the
    # window -- and an interactive shell on a tty that its predecessor is
    # still attached to prints "no job control in background".
    line+=$'\n''exec'
    local word
    for word in "${cmd[@]}"; do
        line+=" $(printf %q "$word")"
    done

    # This shell's environment, put back in the new one.  A terminal
    # launches its children with the environment of the process that
    # started it -- for tmux, the server's -- and not the caller's, so
    # leaving this out is how a venv, a proxy setting or an API key
    # quietly goes missing.  kitty is run from this shell and inherits its
    # environment, so there is nothing to put back for it; only tmux, whose
    # window travels over a socket to a server that was started once, has
    # to be told.
    local -a env_args=() kv
    while IFS= read -r -d '' kv; do
        case $backend in
            tmux)  env_args+=(-e "$kv") ;;
        esac
    done < <(launch-env)

    case $backend in
        kitty)
            # The binary and not the socket.  --detach forks from this
            # shell, so the window inherits its directory, its environment
            # and its file descriptors with nothing passed and nothing
            # listening; --hold so the window survives the command.
            # NOTE: with no socket there is no instance to join, so this is
            # a kitty of its own and not a window in the one the shell is
            # in; see the note on launch-backend.
            # TODO: drop --hold and let the window close itself once the
            # run is over; it is held open for now because a window that
            # stays is the only place to read a run while this is new.
            kitty --detach --hold --title "$title" \
                bash -c "$line"
            ;;
        tmux)
            # -c for the directory and -e for the environment, because a
            # tmux server has both of its own and neither is this shell's.
            # -P -F names the window that was made, so that it can be told
            # to stay after the command ends, which is what --hold is for
            # kitty.
            local id
            id=$(tmux new-window -P -F '#{window_id}' -c "$PWD" "${env_args[@]}" \
                     -- bash -c "$line") || return
            tmux set-option -w -t "$id" remain-on-exit on
            ;;
    esac
}
