#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md.

This file is the entry point and the whole control flow: parse arguments,
pick a model out of models.yaml, rebuild the conversation from sqlite, stream
one completion to stdout, append the new message to the tree.

    dic/dic.py          arguments, model config, HTTP, main flow
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/adaptors/*.py   one wire protocol each
    dic/models.yaml     packaged defaults, overlaid by the user's file
"""
import argparse, http.client, json, os, re, sys, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:          # so `store` and `adaptors.*` resolve whether
    sys.path.insert(0, HERE)      # we are run as a script or as `-m dic.dic`

import yaml

from store import (CONFIG_DIR, db, die, history, normalize, session_read,
                   session_write, store_attachment, turns_from_rows, ulid)

MODELS_PATH = os.path.join(CONFIG_DIR, "models.yaml")
DEFAULTS_PATH = os.path.join(HERE, "models.yaml")
ADAPTER_DIR = os.path.join(CONFIG_DIR, "adapters")

BLUE = "\033[38;5;39m"       # model output, when stdout is a terminal
ORANGE = "\033[38;5;208m"    # the cost summary, when stderr is a terminal
RESET = "\033[0m"


def load_models():
    """Every configured model, highest priority first.

    $DIC_MODELS, then the user's models.yaml, then the defaults shipped in
    the package.  The lists are concatenated rather than one shadowing the
    other, so a user file adds and reorders without having to restate what
    dic already knows; lookups take the first match, so a user entry reusing
    a packaged model_id wins.
    """
    models = []
    for path in (os.environ.get("DIC_MODELS"), MODELS_PATH, DEFAULTS_PATH):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                entries = yaml.safe_load(f)
        except OSError as e:
            die("cannot read %s: %s" % (path, e))
        except yaml.YAMLError as e:
            die("malformed yaml in %s: %s" % (path, e))
        if entries is None:
            continue
        if not isinstance(entries, list):
            die("%s: expected a list of models" % path)
        models.extend(entries)
    return models


def load_model(model_id):
    """The entry named model_id, or the best default when it is None.

    model_id comes from -m or, failing that, $DIC_MODEL; either way an
    unknown name is an error rather than a silent fallback, so a stale
    export cannot quietly send the prompt somewhere else.

    "Best" means the first entry whose api_key_name is actually set in the
    environment, so an install that only has one provider's key configured
    picks that provider without -m; failing that, simply the first entry,
    which then fails with the missing-key message naming what to export.

    Nothing here is validated beyond the lookup itself: unknown keys are the
    extensibility mechanism and belong to the API, not to dic.
    """
    models = load_models()
    if not models:
        die("no models configured: write %s (see models.yaml.example)"
            % MODELS_PATH)
    if model_id:
        for m in models:
            if m.get("model_id") == model_id:
                return m
        die("unknown model: %s (configured: %s)"
            % (model_id, ", ".join(str(m.get("model_id")) for m in models)))
    for m in models:
        if os.environ.get(m.get("api_key_name") or ""):
            return m
    return models[0]


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
    args = p.parse_args()

    prompt = " ".join(args.prompt)
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        prompt = (prompt + "\n\n" + piped) if (prompt and piped.strip()) else (prompt or piped)
    if not prompt.strip() and not args.attachment:
        die("no prompt")

    model = load_model(args.model)
    api_type = model.get("api_type", "openai-chat")
    ad = adaptor(api_type)
    key = os.environ.get(model["api_key_name"])
    if not key:
        die("%s is not set" % model["api_key_name"])

    conn = db()
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

    tty_out, tty_err = sys.stdout.isatty(), sys.stderr.isatty()
    acc, out, colored = {}, [], False
    for event in sse(model["api_base"], ad.PATH, headers, body):
        text = ad.parse(event, acc)
        if text:
            out.append(text)
            if not args.extract:
                if tty_out and not colored:
                    sys.stdout.write(BLUE)
                    colored = True
                sys.stdout.write(text)
                sys.stdout.flush()
    response = "".join(out)
    if not args.extract:
        if colored:
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

    if tty_err:
        sys.stderr.write(ORANGE + summary(model, tin, tout, mid) + RESET + "\n")
        sys.stderr.flush()
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
