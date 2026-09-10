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
from store import (BLUE, CONFIG_DIR, RESET, STATS, colored, db, die, history,
                   normalize, report, session_read, session_write, store_attachment,
                   turns_from_rows, ulid)

ADAPTER_DIR = os.path.join(CONFIG_DIR, "adapters")


def adaptor(api_type):
    """The module implementing api_type: PATH, auth, build, parse, finish.

    Built-ins are dic/adaptors/<api_type with '-' as '_'>.py.  Anything else
    must be ~/.config/fac/adapters/<api_type>.py exporting the same five
    names.  Only the selected adaptor is ever imported.
    """
    import importlib
    name = api_type.replace("-", "_")
    if os.path.exists(os.path.join(HERE, "adaptors", name + ".py")):
        return importlib.import_module("adaptors." + name)
    f = os.path.join(ADAPTER_DIR, api_type + ".py")
    if not os.path.exists(f):
        die("unknown api_type: %s" % api_type)
    import importlib.util
    spec = importlib.util.spec_from_file_location("dic_adaptor", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sse(api_base, path, headers, body, t):
    """POST body, stamp each phase into t, and yield events as they arrive.

    Plain http.client: importing a vendor SDK would cost more than the whole
    time-to-first-token this streams to save.  A failure sets t["error"] and
    yields nothing, so the caller can record the attempt before dying.
    """
    u = urllib.parse.urlparse(api_base)
    cls = http.client.HTTPSConnection if u.scheme != "http" else http.client.HTTPConnection
    conn = cls(u.netloc)
    conn.connect()
    t["t_connect"] = time.time_ns()
    conn.request("POST", u.path.rstrip("/") + path, json.dumps(body), headers)
    t["t_request"] = time.time_ns()
    r = conn.getresponse()
    t["t_headers"], t["status"] = time.time_ns(), r.status
    if r.status != 200:
        t["error"] = "%s: %s" % (r.reason, r.read().decode("utf-8", "replace").strip())
        return
    for line in r:
        if line.startswith(b"data:"):
            d = line[5:].strip()
            if d and d != b"[DONE]":
                yield json.loads(d)


def extract(text):
    """The body of the first fenced code block in text, or text unchanged.

    >>> extract("prose\\n```python\\nx = 1\\n```\\nmore")
    'x = 1\\n'
    >>> extract("no fence here")
    'no fence here'
    """
    m = re.search(r"```[^\n]*\n(.*?)```", text, re.S)
    return m.group(1) if m else text


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
    for o in overrides:
        if "=" not in o:
            die("bad option (expected key=value): %s" % o)
        k, v = o.split("=", 1)
        try:
            v = json.loads(v)
        except ValueError:
            pass
        opts[k.strip()] = v
    return opts


def summary(model, tin, tout, mid, t, verbosity):
    """The one-line cost and mid report written to stderr.

    Costs are per million tokens, as configured; a model with no configured
    price simply reports zero.  At -v the same line carries the timings,
    which is what a human wants while an ad-hoc `dic --stats` is aggregate.

    >>> t = {"t_start": 0, "t_request": 10**7, "t_first": 2 * 10**8,
    ...      "t_last": 10**9, "t_done": 11 * 10**8}
    >>> summary({"cost_input": 3.0, "cost_output": 15.0}, 1000, 500, "01ABC", t, 1)
    'cost: $0.0105 (input: $0.0030, output: $0.0075) --mid=01ABC'
    >>> summary({}, 0, 800, "01ABC", t, 2).split(" | ")[1]
    'overhead 10ms, ttft 200ms, 1000 tok/s, total 1100ms'
    """
    ci = (model.get("cost_input") or 0) * (tin or 0) / 1e6
    co = (model.get("cost_output") or 0) * (tout or 0) / 1e6
    s = "cost: $%.4f (input: $%.4f, output: $%.4f) --mid=%s" % (ci + co, ci, co, mid)
    if verbosity < 2:
        return s
    ms = lambda a, b: ((t.get(b) or 0) - (t.get(a) or 0)) / 1e6
    stream = ms("t_first", "t_last")
    return s + " | overhead %.0fms, ttft %.0fms, %.0f tok/s, total %.0fms" % (
        ms("t_start", "t_request"), ms("t_start", "t_first"),
        (tout or 0) / (stream / 1000) if stream else 0, ms("t_start", "t_done"))


def main():
    p = argparse.ArgumentParser(prog="dic", description="talk to a chat model")
    p.add_argument("prompt", nargs="*")
    p.add_argument("-m", "--model", default=os.environ.get("DIC_MODEL"))
    p.add_argument("-s", "--system")
    p.add_argument("-a", "--attachment", action="append", default=[])
    p.add_argument("-o", "--option", action="append", default=[],
                   metavar="KEY=VALUE", help="override a model option")
    p.add_argument("-x", "--extract", action="store_true")
    p.add_argument("-c", "--continue", dest="cont", action="store_true")
    p.add_argument("-v", "--verbose", dest="v", action="count",
                   help="raise stderr verbosity; repeatable")
    p.add_argument("-q", "--quiet", dest="v", action="store_const", const=0)
    p.add_argument("--mid")
    p.add_argument("--aliases", action="store_true",
                   help="print shell alias definitions and exit")
    p.add_argument("--models", action="store_true",
                   help="list the configured model ids and exit")
    p.add_argument("--stats", action="store_true",
                   help="print per-model runtime and usage statistics and exit")
    args = p.parse_args()

    # a tty wants the cost line, a pipe wants nothing; -v/-q and $DIC_VERBOSITY
    # move off that default rather than replacing it
    v = int(os.environ.get("DIC_VERBOSITY") or (1 if sys.stderr.isatty() else 0))
    v = v if args.v is None else (0 if args.v == 0 else args.v + 1)

    conn = db()
    config.sync(conn)     # a stat per source file; a parse only when one moved
    if args.aliases or args.models:
        sys.stdout.write(config.aliases(conn) if args.aliases
                         else "".join(i + "\n" for i in config.ids(conn)))
        sys.stdout.flush()
        os._exit(0)
    if args.stats:
        rows = conn.execute(STATS).fetchall()   # every number computed by sqlite
        for r in ([rows[0].keys()] if rows else []) + [list(r) for r in rows]:
            sys.stdout.write("\t".join("" if c is None else str(c) for c in r) + "\n")
        sys.stdout.flush()
        os._exit(0)

    prompt = " ".join(args.prompt)
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        prompt = (prompt + "\n\n" + piped) if (prompt and piped.strip()) else (prompt or piped)
    if not prompt.strip() and not args.attachment:
        die("no prompt")

    model = config.resolve(conn, args.model or config.default_id(conn))
    api_type = model.get("api_type", "openai-chat")
    ad = adaptor(api_type)
    key_name = model.get("api_key_name")
    if not key_name:
        die("%s: no api_key_name configured" % model["model_id"])
    key = os.environ.get(key_name)
    if not key:
        die("%s is not set" % key_name)

    prev_mid = args.mid or (session_read() if args.cont else None)

    turns, system = [], args.system
    if prev_mid:
        rows = history(conn, prev_mid)
        if not rows:
            die("no such mid: %s" % prev_mid)
        turns = turns_from_rows(conn, rows, api_type)
        if system is None:
            system = rows[-1]["system"]
    elif system is None:
        # fresh conversations only: continuing one must never let an exported
        # default rewrite the system prompt the thread was started with
        system = os.environ.get("DIC_SYSTEM") or model.get("system")

    atts = [store_attachment(conn, a) for a in args.attachment]
    turns.append({"role": "user",
                  "blocks": [dict(a) for a in atts] + [{"type": "text", "text": prompt}]})
    turns = normalize(turns)

    body = ad.build(model, turns, system, options(model, args.option))
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(ad.auth(key))
    headers.update(model.get("headers") or {})

    report(v, 3, "POST %s%s %s" % (model["api_base"], ad.PATH, json.dumps(body)))
    tty_out = colored(sys.stdout)
    t = {"t_start": T0, "status": None, "error": None}
    acc, out, painted = {}, [], False
    for event in sse(model["api_base"], ad.PATH, headers, body, t):
        text = ad.parse(event, acc)
        if text:
            t.setdefault("t_first", time.time_ns())
            out.append(text)
            if not args.extract:
                if tty_out and not painted:
                    sys.stdout.write(BLUE)
                    painted = True
                sys.stdout.write(text)
                sys.stdout.flush()
    t["t_last"] = time.time_ns()
    response = "".join(out)
    if not args.extract:
        if painted:
            sys.stdout.write(RESET)
        if response and not response.endswith("\n"):
            sys.stdout.write("\n")

    tin, tout = acc.get("usage", (None, None))
    mid = ulid()
    t["t_done"] = time.time_ns()
    conn.execute("INSERT INTO messages VALUES (%s)" % ",".join("?" * 21),
                 (mid, prompt, system, response, json.dumps(ad.finish(acc)), prev_mid,
                  json.dumps([a["aid"] for a in atts]), model["model_id"], api_type,
                  t["status"], t["error"], tin, tout, acc.get("tokens_reasoning"),
                  t["t_start"], t.get("t_connect"), t.get("t_request"),
                  t.get("t_headers"), t.get("t_first"), t["t_last"], t["t_done"]))
    conn.commit()
    conn.close()
    # a failed attempt is recorded for the error rate but the session pointer
    # is left alone, so the row is always a leaf and never replayed
    if t["status"] != 200:
        die("%s %s" % (t["status"], t["error"]))
    session_write(mid)

    if args.extract:
        body_out = extract(response)
        sys.stdout.write(BLUE + body_out + RESET if tty_out else body_out)
    sys.stdout.flush()

    report(v, 1, summary(model, tin, tout, mid, t, v))
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
