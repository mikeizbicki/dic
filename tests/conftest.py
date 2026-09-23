"""Fixtures for dic's integration tests: a HOME of its own, a real HTTP server
on 127.0.0.1, and streams whose isatty() a test controls.

Only the network is faked, so a test runs the code a user runs -- config file,
sqlite, request body, streamed events, the row -- and asserts on what came out.
"""
import collections, http.server, io, json, os, sys, threading

import pytest

# so `pytest tests/` works from a checkout, the way `pytest src/` does
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "src"))

from dic.store import config_dir


class Stream(io.StringIO):
    """A stream that says it is a terminal when a test says it is.

    dic asks a stream two questions: whether to colour it, and (for stderr)
    how much to say on it.  Both answers are policy, so both need a stream on
    each side of the branch.
    """

    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class Streams:
    """The two streams a dic() call writes: `out` is the answer, `err` is stderr."""

    def __init__(self, tty=False):
        self.out, self.err = Stream(tty), Stream(tty)


@pytest.fixture
def stream():
    """A fresh fake stream: `stream()` is a pipe, `stream(tty=True)` a terminal."""
    return Stream


@pytest.fixture
def streams():
    """The out/err pair to hand to dic(); set `.err.tty` to take the other branch."""
    return Streams()


@pytest.fixture
def env(tmp_path):
    """An environment pointing every path dic writes at a throwaway directory."""
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            "DIC_SESSION": "test"}


Request = collections.namedtuple("Request", "path headers body")


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answer every POST with the server's status and the server's events."""

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        self.server.received.append(
            Request(self.path, dict(self.headers), json.loads(body or b"{}")))
        if self.server.status == 200:
            payload = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n"
                               for event in self.server.events)
        else:
            payload = self.server.error.encode()
        self.send_response(self.server.status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass                    # the test's assertions are the log


@pytest.fixture
def sse(events):
    """A real HTTP server that replays canned SSE and keeps every request.

    `sse.events` is what it streams (build one with the `events` fixture),
    `sse.status` and `sse.error` are what it answers when a test wants a
    failure, and `sse.received` is one Request per call -- path, headers and
    parsed body -- so a test can assert what dic sent, not only what it said.
    """
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.api_base = f"http://127.0.0.1:{server.server_port}"
    server.status, server.error, server.events = 200, "overloaded", events()
    server.received = []
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def events():
    """(text, api_type) -> the SSE events that stream that text over that protocol."""

    def build(text="hello", api_type="openai-chat"):
        if api_type == "anthropic-messages":
            return [{"type": "message_start",
                     "message": {"usage": {"input_tokens": 1000,
                                           "output_tokens": 0}}},
                    {"type": "content_block_start",
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta",
                     "delta": {"type": "text_delta", "text": text}},
                    {"type": "content_block_stop"},
                    {"type": "message_delta", "usage": {"output_tokens": 500}}]
        return [{"choices": [{"delta": {"content": text}}]},
                {"choices": [], "usage": {"prompt_tokens": 1000,
                                          "completion_tokens": 500}}]
    return build


@pytest.fixture
def models(env, sse):
    """Write the test's models.json: `fake`, and `fake+anthropic` over it.

    The second states only its api_type, so it is also the proof that a model
    inherits its provider's api_base, key and price.
    """
    directory = config_dir(env)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "models.json"), "w") as f:
        json.dump({"fake": {"model_name": "fake-1",
                            "api_base": sse.api_base,
                            "api_key_name": "FAKE_API_KEY",
                            "cost_input": 3.0, "cost_output": 15.0},
                   "fake+anthropic": {"api_type": "anthropic-messages"}}, f)
    env["FAKE_API_KEY"] = "test-key"


@pytest.fixture
def model(models):
    """The id of the model a test uses unless it names another one."""
    return "fake"
