#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md.

This file is the entry point and the whole control flow: parse arguments,
pick a model out of models.yaml, rebuild the conversation from sqlite, stream
one completion to stdout, append the new message to the tree.

    dic/dic.py          arguments, model config, HTTP, main flow
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/adaptors/*.py   one wire protocol each
"""
import argparse, http.client, json, os, re, sys, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:          # so `store` and `adaptors.*` resolve whether
    sys.path.insert(0, HERE)      # we are run as a script or as `-m dic.dic`

import yaml

from store import (CONFIG_DIR, db, die, history, normalize, session_read,
                   session_write, store_attachment, turns_from_rows, ulid)

MODELS_PATH = os.path.join(CONFIG_DIR, "models.yaml")
ADAPTER_DIR = os.path.join(CONFIG_DIR, "adapters")


def load_model(model_id):
    """The models.yaml entry named model_id, or the first entry if it is None.

    Nothing here is validated beyond the lookup itself: unknown keys are the
    extensibility mechanism and belong to the API, not to dic.
    """
    try:
        with open(MODELS_PATH) as f:
            models = yaml.safe_load(f) or []
    except OSError:
        die("no models configured: %s" % MODELS_PATH)
    if not models:
        die("no models configured: %s" % MODELS_PATH)
    if not model_id:
        return models[0]
    for m in models:
        if m.get("model_id") == model_id:
            return m
    die("unknown model: %s" % model_id)


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


def main():
    p = argparse.ArgumentParser(prog="dic", description="talk to a chat model")
    p.add_argument("prompt", nargs="*")
    p.add_argument("-m", "--model")
    p.add_argument("-s", "--system")
    p.add_argument("-a", "--attachment", action="append", default=[])
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

    atts = [store_attachment(conn, a) for a in args.attachment]
    turns.append({"role": "user",
                  "blocks": [dict(a) for a in atts] + [{"type": "text", "text": prompt}]})
    turns = normalize(turns)

    body = ad.build(model, turns, system, model.get("params") or {})
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(ad.auth(key))
    headers.update(model.get("headers") or {})

    acc, out = {}, []
    for event in sse(model["api_base"], ad.PATH, headers, body):
        text = ad.parse(event, acc)
        if text:
            out.append(text)
            if not args.extract:
                sys.stdout.write(text)
                sys.stdout.flush()
    response = "".join(out)
    if not args.extract and response and not response.endswith("\n"):
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
        sys.stdout.write(extract(response))
    sys.stdout.flush()
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
