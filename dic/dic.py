#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md.

This file is the entry point and the whole control flow: parse arguments,
pick a model out of the config cache, rebuild the conversation from sqlite, stream
one completion to stdout, append the new message to the tree.

    dic/dic.py          arguments, HTTP, main flow
    dic/config.py       model and provider config: json sources, sqlite cache
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/adaptors/*.py   one wire protocol each
    dic/models.json     packaged defaults, overlaid by the user's files
"""
import time                     # first, so that T0 measures dic's own startup
T0 = time.time_ns()             # cost -- imports, config, db -- as well as the API's

import argparse, http.client, json, os, re, sys, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:          # so `store` and `adaptors.*` resolve whether
    sys.path.insert(0, HERE)      # we are run as a script or as `-m dic.dic`

import config
from store import (BLUE, THINKING, CONFIG_DIR, RESET, STATS, db, die, history,
                   normalize, report, session_read, session_write, store_attachment,
                   turns_from_rows, ulid, use_color)

ADAPTER_DIR = os.path.join(CONFIG_DIR, "adapters")


def load_adaptor(api_type):
    """The module implementing api_type: PATH, auth, build, parse, finish.

    Built-ins are dic/adaptors/<api_type with '-' as '_'>.py.  Anything else
    must be ~/.config/fac/adapters/<api_type>.py exporting the same five
    names.  Only the selected adaptor is ever imported.
    """
    import importlib
    name = api_type.replace("-", "_")
    if os.path.exists(os.path.join(HERE, "adaptors", f"{name}.py")):
        return importlib.import_module(f"adaptors.{name}")
    path = os.path.join(ADAPTER_DIR, f"{api_type}.py")
    if not os.path.exists(path):
        die(f"unknown api_type: {api_type}")
    import importlib.util
    spec = importlib.util.spec_from_file_location("dic_adaptor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(api_base, path, headers, body, stamps):
    """POST body, stamp each phase into stamps, and yield events as they arrive.

    Plain http.client: importing a vendor SDK would cost more than the whole
    time-to-first-token this streams to save.  A failure sets stamps["error"]
    and yields nothing, so the caller can record the attempt before dying.
    """
    url = urllib.parse.urlparse(api_base)
    connection = (http.client.HTTPConnection if url.scheme == "http"
                  else http.client.HTTPSConnection)
    conn = connection(url.netloc)
    conn.connect()
    stamps["t_connect"] = time.time_ns()
    conn.request("POST", url.path.rstrip("/") + path, json.dumps(body), headers)
    stamps["t_request"] = time.time_ns()
    reply = conn.getresponse()
    stamps["t_headers"], stamps["status"] = time.time_ns(), reply.status
    if reply.status != 200:
        detail = reply.read().decode("utf-8", "replace").strip()
        stamps["error"] = f"{reply.reason}: {detail}"
        return
    for line in reply:
        if line.startswith(b"data:"):
            data = line[5:].strip()
            if data and data != b"[DONE]":
                yield json.loads(data)


def extract(text):
    """The body of the first fenced code block in text, or text unchanged.

    >>> extract("prose\\n```python\\nx = 1\\n```\\nmore")
    'x = 1\\n'
    >>> extract("no fence here")
    'no fence here'
    """
    match = re.search(r"```[^\n]*\n(.*?)```", text, re.S)
    return match.group(1) if match else text


def options(model, overrides):
    """The model's default options with KEY=VALUE overrides applied.

    Values are decoded as JSON when they parse, so numbers, booleans, lists
    and objects all reach the API with their proper types; anything else is
    passed through as a plain string.

    >>> options({"options": {"max_tokens": 10}}, ["max_tokens=20", "stop=x"])
    {'max_tokens': 20, 'stop': 'x'}
    >>> options({}, ['tools=[]'])
    {'tools': []}
    """
    opts = dict(model.get("options") or {})
    for override in overrides:
        if "=" not in override:
            die(f"bad option (expected key=value): {override}")
        key, value = override.split("=", 1)
        try:
            value = json.loads(value)
        except ValueError:
            pass
        opts[key.strip()] = value
    return opts


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
    """
    cost_in = (model.get("cost_input") or 0) * (tokens_in or 0) / 1e6
    cost_out = (model.get("cost_output") or 0) * (tokens_out or 0) / 1e6
    line = (f"cost: ${cost_in + cost_out:.4f}"
            f" (input: ${cost_in:.4f}, output: ${cost_out:.4f}) --mid={mid}")
    if verbosity < 2:
        return line

    def ms(start, end):
        return ((stamps.get(end) or 0) - (stamps.get(start) or 0)) / 1e6

    stream_ms = ms("t_first", "t_last")
    rate = (tokens_out or 0) / (stream_ms / 1000) if stream_ms else 0
    return line + (f" | overhead {ms('t_start', 't_request'):.0f}ms,"
                   f" ttft {ms('t_start', 't_first'):.0f}ms,"
                   f" {rate:.0f} tok/s, total {ms('t_start', 't_done'):.0f}ms")


def main():
    parser = argparse.ArgumentParser(prog="dic", description="talk to a chat model")
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("-m", "--model", default=os.environ.get("DIC_MODEL"))
    parser.add_argument("-s", "--system")
    parser.add_argument("-a", "--attachment", action="append", default=[])
    parser.add_argument("-o", "--option", action="append", default=[],
                        metavar="KEY=VALUE", help="override a model option")
    parser.add_argument("-x", "--extract", action="store_true")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true")
    parser.add_argument("-v", "--verbose", dest="v", action="count",
                        help="raise stderr verbosity; repeatable")
    parser.add_argument("-q", "--quiet", dest="v", action="store_const", const=0)
    parser.add_argument("--mid")
    parser.add_argument("--aliases", action="store_true",
                        help="print shell alias definitions and exit")
    parser.add_argument("--models", action="store_true",
                        help="list the configured model ids and exit")
    parser.add_argument("--stats", action="store_true",
                        help="print per-model runtime and usage statistics and exit")
    args = parser.parse_args()

    # a tty wants the cost line, a pipe wants nothing; -v/-q and $DIC_VERBOSITY
    # move off that default rather than replacing it
    verbosity = int(os.environ.get("DIC_VERBOSITY") or (1 if sys.stderr.isatty() else 0))
    if args.v is not None:
        verbosity = 0 if args.v == 0 else args.v + 1

    conn = db()
    config.sync(conn)     # a stat per source file; a parse only when one moved
    if args.aliases or args.models:
        sys.stdout.write(config.aliases(conn) if args.aliases
                         else "".join(f"{name}\n" for name in config.model_ids(conn)))
        sys.stdout.flush()
        os._exit(0)
    if args.stats:
        rows = conn.execute(STATS).fetchall()   # every number computed by sqlite
        for row in ([rows[0].keys()] if rows else []) + [list(r) for r in rows]:
            sys.stdout.write("\t".join("" if cell is None else str(cell)
                                       for cell in row) + "\n")
        sys.stdout.flush()
        os._exit(0)

    prompt = " ".join(args.prompt)
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        prompt = f"{prompt}\n\n{piped}" if (prompt and piped.strip()) else (prompt or piped)
    if not prompt.strip() and not args.attachment:
        die("no prompt")

    model = config.resolve(conn, args.model or config.default_id(conn))
    api_type = model.get("api_type", "openai-chat")
    adaptor = load_adaptor(api_type)
    key_name = model.get("api_key_name")
    if not key_name:
        die(f"{model['model_id']}: no api_key_name configured")
    api_key = os.environ.get(key_name)
    if not api_key:
        die(f"{key_name} is not set")

    prev_mid = args.mid or (session_read() if args.cont else None)

    turns, system = [], args.system
    if prev_mid:
        rows = history(conn, prev_mid)
        if not rows:
            die(f"no such mid: {prev_mid}")
        turns = turns_from_rows(conn, rows, api_type)
        if system is None:
            system = rows[-1]["system"]
    elif system is None:
        # fresh conversations only: continuing one must never let an exported
        # default rewrite the system prompt the thread was started with
        system = os.environ.get("DIC_SYSTEM") or model.get("system")

    attached = [store_attachment(conn, path) for path in args.attachment]
    turns.append({"role": "user",
                  "blocks": [dict(att) for att in attached]
                            + [{"type": "text", "text": prompt}]})
    turns = normalize(turns)

    body = adaptor.build(model, turns, system, options(model, args.option))
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(adaptor.auth(api_key))
    headers.update(model.get("headers") or {})

    report(verbosity, 3,
           f"POST {model['api_base']}{adaptor.PATH} {json.dumps(body)}")
    color = use_color(sys.stdout)
    stamps = {"t_start": T0, "status": None, "error": None}
    styles = {"": BLUE, "thinking": THINKING}
    acc, chunks, painted = {}, [], None
    for event in events(model["api_base"], adaptor.PATH, headers, body, stamps):
        text, kind = adaptor.parse(event, acc)
        if not text:
            continue
        stamps.setdefault("t_first", time.time_ns())
        if kind == "thinking":
            if use_color(sys.stderr):
                sys.stderr.write(THINKING + text + RESET)
            else:
                sys.stderr.write(text)
            sys.stderr.flush()
            continue
        chunks.append(text)
        if not args.extract:
            if color and painted != kind:
                sys.stdout.write((RESET if painted is not None else "")
                                 + styles[kind])
                painted = kind
            sys.stdout.write(text)
            sys.stdout.flush()
    stamps["t_last"] = time.time_ns()
    response = "".join(chunks)
    if not args.extract:
        if painted is not None:
            sys.stdout.write(RESET)
        if response and not response.endswith("\n"):
            sys.stdout.write("\n")

    tokens_in, tokens_out = acc.get("usage", (None, None))
    mid = ulid()
    stamps["t_done"] = time.time_ns()
    conn.execute(f"INSERT INTO messages VALUES ({','.join('?' * 21)})",
                 (mid, prompt, system, response, json.dumps(adaptor.finish(acc)),
                  prev_mid, json.dumps([att["aid"] for att in attached]),
                  model["model_id"], api_type, stamps["status"], stamps["error"],
                  tokens_in, tokens_out, acc.get("tokens_reasoning"),
                  stamps["t_start"], stamps.get("t_connect"), stamps.get("t_request"),
                  stamps.get("t_headers"), stamps.get("t_first"), stamps["t_last"],
                  stamps["t_done"]))
    conn.commit()
    conn.close()
    # a failed attempt is recorded for the error rate but the session pointer
    # is left alone, so the row is always a leaf and never replayed
    if stamps["status"] != 200:
        die(f"{stamps['status']} {stamps['error']}")
    session_write(mid)

    if args.extract:
        snippet = extract(response)
        sys.stdout.write(BLUE + snippet + RESET if color else snippet)
    sys.stdout.flush()

    report(verbosity, 1,
           summary(model, tokens_in, tokens_out, mid, stamps, verbosity))
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
