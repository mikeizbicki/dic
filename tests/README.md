# tests

Most of `dic` is pure functions, and those are tested where they live: a
docstring with a `>>>` in it is the test, and `pytest --doctest-modules` runs
them.  A new example goes next to the function it describes, not in here.

What is left is what a doctest cannot show: sqlite, a config file, a socket, a
thread, a terminal.  That is what this directory holds, and nearly every test
in it is one `dic()` call -- a prompt in, an answer out, a row written --
because that is the unit a user has and the unit a regression breaks.

The rules:

* **fake the network, and nothing else.**  `conftest.py` runs a real HTTP
  server on 127.0.0.1 answering canned SSE, so the config parse, the sqlite
  file, the request body, the stream and the row are all the real thing;
* **one test, one call, one reason to fail.**  A comment that says "and now
  also" means the test is two tests;
* **assert on what a user sees**: stdout, stderr, the row, the request that
  went out.  Never a local variable;
* **libraries are fine here.**  Nothing in this directory is on the latency
  path, so timings do not matter -- though the fixtures still need none:
  `http.server` and `threading` are stdlib.
* **start a process only to test the process.**  An in-process `dic()` call
  cannot see startup, because pytest has paid every import before the test
  runs; `test_startup.py` therefore runs a real `python -m dic`.  It is
  `@pytest.mark.slow`, so the default run leaves it out.
* **one fixture set per shape of protocol.**  `test_client.py`'s server answers
  every POST with SSE and is the right server for every protocol that is one
  request and one stream; `test_multimedia.py` answers GET and PUT too, because
  a job that is submitted, polled and downloaded is a different shape and not
  a different assertion.

If a test needs three sentences of setup to be believed, the setup is the bug.
