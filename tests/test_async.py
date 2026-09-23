"""The async bridge: only what the facade adds, and only what it must not lose.

`generate_async` is one thread around `dic()`, so `tests/test_client.py`
tests the call and this file tests the bridge: that concurrent calls are
really in flight at once, that a failure crosses the thread boundary as
itself, that a cancelled Task leaves the worker running to the end of the
call, that the worker is dic's own and not the event loop's, and that none
of it costs the sync path an import.

No pytest-asyncio: nothing here needs a loop fixture, so `asyncio.run` in a
plain test function is the whole of the plumbing.
"""
import asyncio, concurrent.futures, http.server, io, json, os
import subprocess, sys, threading

import pytest

import dic.client
from dic import config
from dic.client import generate_async
from dic.store import db
from dic.tty import DicError

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


class Fake:
    """A chat-completions endpoint that can be held open and made to fail.

    `gate` runs with the request body before the response headers are
    written, which is how a test parks a call inside the network and looks
    at what another thread does meanwhile; `bodies` is every request that
    arrived.  A `status` other than 200 answers with an error, which is how
    a call fails without the network failing.
    """

    def __init__(self):
        self.gate = None
        self.bodies = []
        self.status = 200
        self.release = threading.Event()   # the fixture's way out of a gate
        self.lock = threading.Lock()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def api_base(self):
        """The URL a model entry carries: host and port, and no path."""
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self):
        """Let every parked handler go, then stop serving."""
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def handler(self):
        """The request handler class this instance serves with."""
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass  # pytest's capture is not for access logs

            def do_POST(self):
                body = json.loads(
                    self.rfile.read(int(self.headers["content-length"])))
                with fake.lock:
                    fake.bodies.append(body)
                if fake.gate:
                    fake.gate(body)
                if fake.status != 200:
                    self.fail_the_call()
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n')
                self.wfile.write(
                    b'data: {"choices": [], "usage": {"prompt_tokens": 3,'
                    b' "completion_tokens": 1}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def fail_the_call(self):
                detail = json.dumps(
                    {"error": {"message": "the model is down"}}).encode()
                self.send_response(fake.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(detail)))
                self.end_headers()
                self.wfile.write(detail)

        return Handler


@pytest.fixture
def fake():
    """A fake model, stopped however the test left it."""
    server = Fake()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def home(tmp_path, fake):
    """The env and the config file that point dic at the fake model.

    dic is handed `env` and `models_file` rather than reading the process's,
    so no test touches the developer's own dic.db; and both directories dic
    writes to -- the config and the session pointer -- land in tmp_path.
    """
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"fake": {"model_name": "fake",
                                         "api_base": fake.api_base,
                                         "api_key_name": "FAKE_KEY"}}))
    return {"env": {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path),
                    "FAKE_KEY": "k"},
            "models_file": str(path)}


def knobs(home, **over):
    """dic()'s keyword arguments for one call, with its own out and err."""
    args = dict(model="fake", models_file=home["models_file"],
                env=home["env"], out=io.StringIO(), err=io.StringIO())
    args.update(over)
    return args


def warm(home):
    """Write the config cache, before N threads race to write it.

    Not something dic needs: it is what the first call would have done
    anyway.  A test about concurrency should not also be a test about who
    won a lock.
    """
    conn = db(home["env"])
    config.sync(conn, home["env"], home["models_file"])
    conn.close()


def rows(home):
    """Every message row in this HOME's database, oldest first."""
    conn = db(home["env"])
    try:
        return conn.execute(
            "SELECT status, response FROM messages ORDER BY mid").fetchall()
    finally:
        conn.close()


def test_concurrent_calls_are_in_flight_at_once(home, fake):
    """The whole point: four awaited calls are four requests in flight.

    Every request waits at a rendezvous for the other three, so a facade
    that serialized its calls -- one lock, one call at a time -- would time
    the barrier out instead of passing it.
    """
    warm(home)
    rendezvous = threading.Barrier(4, timeout=10)
    late = []

    def gate(body):
        try:
            rendezvous.wait()
        except threading.BrokenBarrierError:
            late.append(body)          # the other calls never turned up

    fake.gate = gate

    async def main():
        calls = [generate_async("hi", **knobs(home)) for _ in range(4)]
        return await asyncio.gather(*calls)

    replies = asyncio.run(main())
    assert not late, "the calls did not overlap"
    assert [reply.text for reply in replies] == ["hi\n"] * 4
    # one row each, from four threads and four connections: sqlite would
    # have raised "SQLite objects created in a thread can only be used in
    # that same thread" if a connection had been shared between them
    assert [row["status"] for row in rows(home)] == [200] * 4
    assert [row["response"] for row in rows(home)] == ["hi"] * 4


def test_a_failure_arrives_as_a_dicerror(home, fake):
    """dic() raising in the worker is the DicError that arrives here."""
    fake.status = 500
    with pytest.raises(DicError) as caught:
        asyncio.run(generate_async("hi", **knobs(home)))
    assert "500" in str(caught.value)
    assert [(row["status"], row["response"]) for row in rows(home)] == [
        (500, "")]


def test_a_base_exception_is_never_swallowed(home, monkeypatch):
    """A ^C inside the worker is the caller's ^C, not a Future's lost error.

    The transport is replaced with one that raises KeyboardInterrupt, which
    is what http.client does when the user interrupts it.  Discarding it, or
    turning it into anything else, is what a thread-backed facade must not
    do.
    """
    warm(home)

    def boom(*args, **kwargs):
        raise KeyboardInterrupt
        yield                       # a generator, like the real events()

    monkeypatch.setattr(dic.client, "events", boom)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(generate_async("hi", **knobs(home)))


def test_a_cancelled_task_leaves_the_call_running(home, fake):
    """cancel() stops the caller; the thread cannot be stopped, so it ends.

    A half-recorded row would be worse than a thread that outlives its
    caller by one call, so the row lands whole after the Task is already
    cancelled -- and the answer is already in `out`, because nothing can
    take back bytes a worker has printed.
    """
    warm(home)
    fake.gate = lambda body: fake.release.wait(timeout=10)
    args = knobs(home)

    async def main():
        task = asyncio.create_task(generate_async("hi", **args))
        for _ in range(500):            # until the request is in flight
            if fake.bodies:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the call never reached the server")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        fake.release.set()              # let the abandoned worker finish
        for _ in range(1000):           # and give it the time to
            if rows(home):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the cancelled call never recorded its row")

    asyncio.run(main())
    assert args["out"].getvalue() == "hi\n"
    assert [(row["status"], row["response"]) for row in rows(home)] == [
        (200, "hi")]


def test_a_saturated_loop_executor_does_not_block_dic(home):
    """dic's pool is dic's: a full loop executor is not dic's problem.

    asyncio's default executor is shared with every run_in_executor(None,
    ...) in the interpreter, so a bridge that used it would queue dic's
    calls behind whoever else is running.  Here its one worker is busy
    forever, and the call must still come back.
    """
    warm(home)
    release = threading.Event()

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=1))
        busy = loop.run_in_executor(None, release.wait)
        try:
            return await asyncio.wait_for(generate_async("hi", **knobs(home)),
                                          timeout=5)
        finally:
            release.set()
            await busy

    assert asyncio.run(main()).text == "hi\n"


def test_the_sync_path_imports_nothing_of_this():
    """A fresh interpreter importing dic.client must not import asyncio.

    In this process the assertion would be worthless -- this module imports
    asyncio at the top, and pytest collects every test module before it runs
    one -- so the interpreter is asked instead, and it is asked about the
    import list the CLI pays for on every invocation.
    """
    code = ("import sys, dic.client\n"
            "assert 'asyncio' not in sys.modules, sys.modules['asyncio']\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
