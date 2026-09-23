"""Everything dic prints that is not the answer: colour, errors, the cost
line, and the pv-style progress meters.

One module, because the two halves of the meters would otherwise live in two
files: store.py held the formatting (pv_bytes, pv_clock, pv_line) while
dic.py held the painting that consumes it.  Nothing here reads sqlite or the
network, so importing it costs one file of pure stdlib.

See SPEC.md, "Color", "Progress meters" and "Verbosity".
"""
import os, sys, time

BLUE = "\033[38;5;39m"       # model output
ORANGE = "\033[38;5;208m"    # the cost summary
RED = "\033[31m"             # errors
THINKING = "\033[38;5;245m"             # reasoning: faded gray on the usual background
RESET = "\033[0m"


def use_color(stream, env=None):
    """Whether to emit ANSI colour on stream.

    $DIC_COLOR (never|auto|always) wins, then $NO_COLOR, then isatty, so a
    pipe gets clean text without the caller having to ask and a pager can ask
    for colour anyway.  dic never prints uncoloured text to a terminal: every
    stream has a meaning (blue output, orange cost, red error).
    """
    env = os.environ if env is None else env
    mode = env.get("DIC_COLOR", "auto")
    if mode in ("never", "always"):
        return mode == "always"
    return stream.isatty() and not env.get("NO_COLOR")


class DicError(Exception):
    """A failure dic reports: what die() prints, raised instead of exiting.

    Library code raises it, so that a failed call cannot kill the caller's
    process, and the entry point hands it to die(), so that what a failure
    looks like on the terminal and which status it exits with are each said
    exactly once, at the top, where the caller and its streams are known.
    """


def die(msg, err=None, env=None):
    """Report an error in red on stderr and exit nonzero: the CLI's exit.

    dic() raises DicError instead of calling this, and the entry point
    catches that error and passes it here, so a library call never ends in
    sys.exit and the CLI never ends without one.
    """
    err = sys.stderr if err is None else err
    line = f"dic: {msg}\n"
    err.write(RED + line + RESET if use_color(err, env) else line)
    sys.exit(1)


def report(verbosity, level, msg, err=None, env=None):
    """Write msg to stderr in orange when verbosity has reached level.

    All of dic's stderr goes through here, so colour policy and verbosity
    policy are each stated exactly once.

    >>> report(0, 1, "not printed")
    """
    if verbosity < level:
        return
    err = sys.stderr if err is None else err
    line = f"{msg}\n"
    err.write(ORANGE + line + RESET if use_color(err, env) else line)
    err.flush()


def pv_bytes(n):
    """A byte count as pv prints it: the number, then the unit in three columns.

    >>> pv_bytes(58), pv_bytes(2048)
    ('58.0  B', '2.0KiB')
    """
    value, unit = float(n), "B"
    for bigger in ("KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f}{unit:>3}"


def pv_clock(seconds, tenths=False):
    """H:MM:SS, as pv -t prints it; optionally to a tenth of a second.

    A tenth is visible movement on a clock nobody is timing anything with,
    which is the point of the one that runs while dic waits.

    >>> pv_clock(64.7), pv_clock(64.7, tenths=True)
    ('0:01:04', '0:01:04.7')
    """
    whole = int(seconds)
    out = f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"
    return out + f".{int((seconds - whole) * 10)}" if tenths else out


def pv_line(name, nbytes, seconds):
    """One `pv -N name -btr` status line.

    Used for the reasoning stream, which is progress and not content: the
    meter says how much a model thought without scrolling its answer away.

    >>> pv_line("thinking", 58, 4.0)
    'thinking: 58.0  B 0:00:04 [14.5  B/s]'
    """
    rate = nbytes / seconds if seconds else 0
    return (f"{name}: {pv_bytes(nbytes)} {pv_clock(seconds)}"
            f" [{pv_bytes(rate)}/s]")


def pv_paint(state, line, err=None, env=None):
    """Rewrite one status line in place on stderr, in the reasoning gray.

    The line is padded to the widest one written so far, since a line that
    shrinks would otherwise leave the tail of the previous one behind.
    """
    err = sys.stderr if err is None else err
    state["width"] = max(state.get("width", 0), len(line))
    line = line.ljust(state["width"])
    err.write("\r" + (THINKING + line + RESET if use_color(err, env) else line))
    err.flush()


WAIT_DELAY = 0.5     # a first token faster than this needs no reassurance


def pv_waiter(err=None, env=None):
    """Tick a bare clock until the first token arrives; return its stopper.

    A slow first token is indistinguishable from a hung program, so after
    WAIT_DELAY seconds the line reads `ttft: 0:00:03.4` and keeps counting
    in tenths, which is slow enough to read and fast enough to look alive.
    This needs a thread because the main thread is blocked in a socket read
    until exactly the moment the clock should stop; the stopper joins it, so
    only one of the two ever writes to stderr, and then closes the line with
    the final time -- if the clock was ever shown at all.
    """
    import threading
    err = sys.stderr if err is None else err
    state, start, stop = {}, time.time_ns(), threading.Event()

    def seconds():
        return (time.time_ns() - start) / 1e9

    def tick():
        while not stop.wait(0.1):
            if seconds() >= WAIT_DELAY:
                pv_paint(state, f"ttft: {pv_clock(seconds(), tenths=True)}", err, env)

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()

    def stopper():
        stop.set()
        thread.join()
        if state.get("width"):
            pv_paint(state, f"ttft: {pv_clock(seconds(), tenths=True)}", err, env)
            err.write("\n")
            err.flush()
    return stopper


def pv_update(state, text=None, final=False, err=None, env=None):
    """Repaint one `pv -N <state["name"]> -btr` meter on stderr.

    text adds its bytes to the meter, creating it on the first call; final
    closes the line so the finished meter stays visible above whatever comes
    next.  A stream that never arrives therefore writes nothing at all, and
    repaints are capped at ten a second so a fast stream is not spent on
    escape codes.
    """
    if state.get("done"):
        return
    err = sys.stderr if err is None else err
    now = time.time_ns()
    if text is not None:
        state["bytes"] = state.get("bytes", 0) + len(text.encode())
        state.setdefault("t0", now)
    if "t0" not in state:
        return
    if not final and now - state.get("t_paint", 0) < 10 ** 8:
        return
    state["t_paint"] = now
    pv_paint(state, pv_line(state["name"], state["bytes"],
                            (now - state["t0"]) / 1e9), err, env)
    if final:
        err.write("\n")
        state["done"] = True
        err.flush()


def summary(model, tokens_in, tokens_out, mid, stamps, verbosity):
    """The one-line cost and mid report written to stderr.

    Costs are per million tokens, as configured; a model with no configured
    price simply reports zero.  At -v the same line carries the timings,
    which is what a human wants while an ad-hoc `dic --stats` is aggregate.

    >>> stamps = {"t_start": 0, "t_request": 10**7, "t_first": 2 * 10**8,
    ...           "t_last": 10**9, "t_done": 11 * 10**8}
    >>> summary({"cost_input": 3.0, "cost_output": 15.0}, 1000, 500, "01ABC",
    ...         stamps, 1)
    'cost: $0.0105 (input: $0.0030, output: $0.0075) --mid=01ABC'
    >>> summary({}, 0, 800, "01ABC", stamps, 2).split(" | ")[1]
    'overhead 10ms, ttft 200ms, 1000 tok/s, total 1100ms'
    >>> summary({}, None, None, None, stamps, 1)
    'cost: $0.0000 (input: $0.0000, output: $0.0000)'
    """
    cost_in = (model.get("cost_input") or 0) * (tokens_in or 0) / 1e6
    cost_out = (model.get("cost_output") or 0) * (tokens_out or 0) / 1e6
    line = (f"cost: ${cost_in + cost_out:.4f}"
            f" (input: ${cost_in:.4f}, output: ${cost_out:.4f})")
    if mid:                     # a cancelled call has no row, and so no mid
        line += f" --mid={mid}"
    if verbosity < 2:
        return line

    def ms(start, end):
        return ((stamps.get(end) or 0) - (stamps.get(start) or 0)) / 1e6

    stream_ms = ms("t_first", "t_last")
    rate = (tokens_out or 0) / (stream_ms / 1000) if stream_ms else 0
    return line + (f" | overhead {ms('t_start', 't_request'):.0f}ms,"
                   f" ttft {ms('t_start', 't_first'):.0f}ms,"
                   f" {rate:.0f} tok/s, total {ms('t_start', 't_done'):.0f}ms")
