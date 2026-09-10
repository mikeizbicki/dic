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
import argparse, http.client, json, os, re, sys, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:          # so `store` and `adaptors.*` resolve whether
    sys.path.insert(0, HERE)      # we are run as a script or as `-m dic.dic`

import config
from store import (BLUE, CONFIG_DIR, ORANGE, RESET, colored, db, die, history,
                   normalize, session_read, session_write, store_attachment,
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


def sse(api_base, path, headers, body):
    """POST body and yield each parsed server-sent event as it arrives.

    Plain http.client: importing a vendor SDK would cost more than the whole
    time-to-first-token this streams to save.
    """
    u = urllib.parse.urlparse(api_base)
    cls = http.client.HTTPSConnection if u.scheme != "http" else http.client.HTTPConnection
    conn = cls(u.netloc)
    conn.request("POST", u.path.rstrip("/") + path, json.dumps(body), headers)
    r = conn.getresponse()
    if r.status != 200:
        die("%d %s: %s" % (r.status, r.reason, r.read().decode("utf-8", "replace").strip()))
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


def summary(model, tin, tout, mid):
    """The one-line cost and mid report written to stderr.

    Costs are per million tokens, as configured; a model with no configured
    price simply reports zero.

    >>> summary({"cost_input": 3.0, "cost_output": 15.0}, 1000, 500, "01ABC")
    'cost: $0.0105 (input: $0.0030, output: $0.0075) --mid=01ABC'
    """
    ci = (model.get("cost_input") or 0) * (tin or 0) / 1e6
    co = (model.get("cost_output") or 0) * (tout or 0) / 1e6
    return "cost: $%.4f (input: $%.4f, output: $%.4f) --mid=%s" % (ci + co, ci, co, mid)


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
    p.add_argument("--mid")
    p.add_argument("--aliases", action="store_true",
                   help="print shell alias definitions and exit")
    p.add_argument("--models", action="store_true",
                   help="list the configured model ids and exit")
    args = p.parse_args()

    conn = db()
    config.sync(conn)     # a stat per source file; a parse only when one moved
    if args.aliases or args.models:
        sys.stdout.write(config.aliases(conn) if args.aliases
                         else "".join(i + "\n" for i in config.ids(conn)))
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

    tty_out, tty_err = colored(sys.stdout), colored(sys.stderr)
    acc, out, painted = {}, [], False
    for event in sse(model["api_base"], ad.PATH, headers, body):
        text = ad.parse(event, acc)
        if text:
            out.append(text)
            if not args.extract:
                if tty_out and not painted:
                    sys.stdout.write(BLUE)
                    painted = True
                sys.stdout.write(text)
                sys.stdout.flush()
    response = "".join(out)
    if not args.extract:
        if painted:
            sys.stdout.write(RESET)
        if response and not response.endswith("\n"):
            sys.stdout.write("\n")

    tin, tout = acc.get("usage", (None, None))
    mid = ulid()
    conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, prompt, system, response, json.dumps(ad.finish(acc)), prev_mid,
                  int(time.time()), json.dumps([a["aid"] for a in atts]),
                  model["model_id"], api_type, tin, tout))
    conn.commit()
    conn.close()
    session_write(mid)

    if args.extract:
        body_out = extract(response)
        sys.stdout.write(BLUE + body_out + RESET if tty_out else body_out)
    sys.stdout.flush()

    if sys.stderr.isatty():
        s = summary(model, tin, tout, mid)
        sys.stderr.write((ORANGE + s + RESET if tty_err else s) + "\n")
        sys.stderr.flush()
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
