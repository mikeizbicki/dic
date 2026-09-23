"""The stderr policy a doctest cannot reach: a fake terminal, a repaint in
place, and the clock thread that runs while dic waits for its first token.

The rules -- which colour, how much verbosity -- are stated in tty.py's own
docstrings; these tests are that a stream and a live thread take the branch
those rules describe.
"""
import time

from dic import tty


def test_report_is_coloured_on_a_terminal_and_plain_on_a_pipe(stream):
    err = stream(tty=True)
    tty.report(0, 1, "cost: $0.01", err=err, env={})   # too quiet to say anything
    assert err.getvalue() == ""
    tty.report(1, 1, "cost: $0.01", err=err, env={})
    assert err.getvalue() == f"{tty.ORANGE}cost: $0.01\n{tty.RESET}"

    pipe = stream()
    tty.report(1, 1, "cost: $0.01", err=pipe, env={})
    assert pipe.getvalue() == "cost: $0.01\n"


def test_DIC_COLOR_overrides_what_the_stream_says(stream):
    forced = stream()                          # a pipe, so colour would be off...
    tty.report(1, 1, "hi", err=forced, env={"DIC_COLOR": "always"})
    assert forced.getvalue().startswith(tty.ORANGE)

    off = stream(tty=True)                     # a terminal, so colour would be on...
    tty.report(1, 1, "hi", err=off, env={"DIC_COLOR": "never"})
    assert off.getvalue() == "hi\n"


def test_NO_COLOR_uncolours_a_terminal(stream):
    err = stream(tty=True)
    tty.report(1, 1, "hi", err=err, env={"NO_COLOR": "1"})
    assert err.getvalue() == "hi\n"


def test_a_meter_writes_nothing_until_something_arrives(stream):
    err = stream()
    tty.pv_update({"name": "thinking"}, final=True, err=err, env={})
    assert err.getvalue() == ""       # a model that never reasoned shows no line


def test_a_meter_counts_bytes_repaints_in_place_and_closes_once(stream):
    err, meter = stream(), {"name": "response"}
    tty.pv_update(meter, "hello", err=err, env={})
    assert err.getvalue().startswith("\rresponse: 5.0  B 0:00:00 [")

    tty.pv_update(meter, "a", err=err, env={})    # within 0.1s: not painted again
    tty.pv_update(meter, "b", err=err, env={})
    assert "response: 7.0  B" not in err.getvalue()

    tty.pv_update(meter, final=True, err=err, env={})
    closed = err.getvalue()
    assert closed.rstrip().endswith("/s]") and closed.count("\n") == 1
    tty.pv_update(meter, "more", err=err, env={})
    assert err.getvalue() == closed               # a closed meter stays closed


def test_the_wait_clock_is_never_drawn_when_the_answer_is_quick(stream, monkeypatch):
    monkeypatch.setattr(tty, "WAIT_DELAY", 60)    # nothing here is slow
    err = stream()
    tty.pv_waiter(err=err, env={})()
    assert err.getvalue() == ""


def test_the_wait_clock_counts_in_tenths_and_closes_its_own_line(stream, monkeypatch):
    monkeypatch.setattr(tty, "WAIT_DELAY", 0)
    err = stream()
    stop = tty.pv_waiter(err=err, env={})
    time.sleep(0.3)                               # long enough for two or three ticks
    stop()
    text = err.getvalue()
    assert text.startswith("\rttft: 0:00:00.")    # repainted in place, not scrolled
    assert text.endswith("\n") and text.count("\n") == 1
