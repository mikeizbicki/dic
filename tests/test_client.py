"""Integration tests: a real config file, a real sqlite file, a real socket.

Each test is one dic() call -- prompt in, answer out, one row written -- because
that is the unit a user has, and a failure should name the part of the call
that broke.  A pure function is doctested where it lives, not retested here.
"""
import json, os

import pytest

from dic.client import dic
from dic.store import db, session_path
from dic.tty import DicError


def say(streams, env, prompt, **knobs):
    """One dic() call through the fake streams and the test environment."""
    return dic(prompt, env=env, out=streams.out, err=streams.err, **knobs)


def tree(env):
    """The message tree, oldest first."""
    conn = db(env)
    rows = conn.execute("SELECT * FROM messages ORDER BY t_start").fetchall()
    conn.close()
    return rows


def test_one_call_prints_the_answer_and_appends_one_row(streams, env, sse, model):
    reply = say(streams, env, "hi", model=model)

    assert streams.out.getvalue() == "hello\n"
    assert reply.text == "hello\n"
    assert (reply.status, reply.model_id) == (200, "fake")

    row = tree(env)[0]
    assert row["mid"] == reply.mid
    assert (row["user"], row["response"], row["status"]) == ("hi", "hello", 200)
    assert row["t_start"] <= row["t_request"] <= row["t_first"] <= row["t_last"]
    assert os.path.exists(session_path(env))     # something for -c to point at


def test_the_request_names_the_model_and_carries_the_prompt(streams, env, sse, model):
    say(streams, env, "hi", model=model)

    request = sse.received[-1]
    assert request.path == "/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.body["model"] == "fake-1"
    assert request.body["messages"] == [{"role": "user", "content": "hi"}]


def test_the_cost_line_reports_the_tokens_the_api_counted(streams, env, sse, model):
    streams.err.tty = True                       # a terminal gets the cost line
    reply = say(streams, env, "hi", model=model)

    assert ("cost: $0.0105 (input: $0.0030, output: $0.0075)"
            f" --mid={reply.mid}") in streams.err.getvalue()
    row = tree(env)[0]
    assert (row["tokens_input"], row["tokens_output"]) == (1000, 500)


def test_continue_replays_the_conversation(streams, env, sse, model, events):
    first = say(streams, env, "one", model=model)
    sse.events = events("second")
    say(streams, env, "two", model=model, cont=True)

    assert sse.received[-1].body["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "two"}]
    assert streams.out.getvalue() == "hello\nsecond\n"
    assert tree(env)[1]["prev_mid"] == first.mid


def test_mid_wins_over_continue(streams, env, sse, model, events):
    first = say(streams, env, "one", model=model)
    say(streams, env, "two", model=model)        # a second, unrelated thread
    sse.events = events("three")
    say(streams, env, "three", model=model, cont=True, mid=first.mid)

    assert [m["content"] for m in sse.received[-1].body["messages"]] == [
        "one", "hello", "three"]


def test_a_failed_call_is_stored_and_leaves_the_pointer_alone(streams, env, sse, model):
    sse.status = 503
    with pytest.raises(DicError, match="503"):
        say(streams, env, "hi", model=model)

    row = tree(env)[0]
    assert row["status"] == 503 and "overloaded" in row["error"]
    assert row["response"] == ""                 # nothing to replay next turn
    assert not os.path.exists(session_path(env))


def test_continue_without_a_session_pointer_is_an_error(streams, env, model):
    with pytest.raises(DicError, match="no conversation"):
        say(streams, env, "hi", model=model, cont=True)


def test_extract_prints_the_code_block_but_stores_the_whole_answer(streams, env, sse,
                                                                   model, events):
    sse.events = events("prose\n```python\nx = 1\n```\ntail")
    reply = say(streams, env, "hi", model=model, extract=True)

    assert streams.out.getvalue() == "x = 1\n"
    assert reply.text == "x = 1\n"
    assert tree(env)[0]["response"] == "prose\n```python\nx = 1\n```\ntail"


def test_an_attachment_is_stored_verbatim_and_sent_as_a_data_url(streams, env, sse,
                                                                 model, tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    say(streams, env, "what is this", model=model, attachment=[str(image)])

    parts = sse.received[-1].body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1] == {"type": "text", "text": "what is this"}

    aid = json.loads(tree(env)[0]["attachments"])[0]
    conn = db(env)
    data = conn.execute("SELECT data FROM attachments WHERE aid=?",
                        (aid,)).fetchone()[0]
    conn.close()
    assert data == b"\x89PNG\r\n\x1a\n"


def test_a_turn_is_replayed_verbatim_to_the_protocol_that_produced_it(streams, env,
                                                                      sse, models,
                                                                      events):
    sse.events = events("first", "anthropic-messages")
    first = say(streams, env, "one", model="fake+anthropic")
    say(streams, env, "two", model="fake+anthropic", mid=first.mid)

    assert sse.received[-1].path == "/messages"
    assert sse.received[-1].body["messages"][1] == {
        "role": "assistant", "content": [{"type": "text", "text": "first"}]}


def test_a_turn_is_converted_for_a_call_that_speaks_another_protocol(streams, env,
                                                                     sse, model,
                                                                     events):
    sse.events = events("first", "anthropic-messages")
    first = say(streams, env, "one", model="fake+anthropic")
    say(streams, env, "two", model=model, mid=first.mid)

    assert sse.received[-1].body["messages"][1] == {
        "role": "assistant", "content": "first"}
    assert tree(env)[1]["api_type"] == "openai-chat"
