#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md."""
import argparse, base64, http.client, json, mimetypes, os, re, sqlite3, sys, time, urllib.parse

import yaml

CONFIG_DIR = os.path.expanduser("~/.config/fac")
DB_PATH = os.path.join(CONFIG_DIR, "dic.db")
MODELS_PATH = os.path.join(CONFIG_DIR, "models.yaml")
ADAPTER_DIR = os.path.join(CONFIG_DIR, "adapters")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    mid TEXT PRIMARY KEY,
    user TEXT,
    system TEXT,
    response TEXT,
    response_raw TEXT,
    prev_mid TEXT,
    time INTEGER,
    attachments TEXT,
    model_id TEXT,
    api_type TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER);
CREATE INDEX IF NOT EXISTS messages_prev_mid ON messages(prev_mid);
CREATE TABLE IF NOT EXISTS attachments (
    aid TEXT PRIMARY KEY,
    path TEXT,
    mime_type TEXT,
    data BLOB);
"""


def die(msg):
    sys.stderr.write("dic: %s\n" % msg)
    sys.exit(1)


B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid():
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(B32[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


# ---------------------------------------------------------------- storage

def db():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def history(conn, mid):
    """The ancestor chain of mid, oldest first."""
    cols = "mid,user,system,response,response_raw,prev_mid,api_type,attachments"
    rows = conn.execute(
        "WITH RECURSIVE chain(%s) AS ("
        "  SELECT %s FROM messages WHERE mid=?"
        "  UNION ALL"
        "  SELECT %s FROM messages m JOIN chain c ON m.mid=c.prev_mid)"
        " SELECT * FROM chain" % (cols, cols, ",".join("m." + c for c in cols.split(","))),
        (mid,)).fetchall()
    return list(reversed(rows))


def store_attachment(conn, path):
    with open(path, "rb") as f:
        data = f.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    aid = ulid()
    conn.execute("INSERT INTO attachments VALUES (?,?,?,?)", (aid, path, mime, data))
    return {"aid": aid, "type": "image", "mime_type": mime, "data": data}


# ---------------------------------------------------------------- session

def session_path():
    rt = os.environ.get("XDG_RUNTIME_DIR")
    d = os.path.join(rt, "fac", "dic") if rt else "/tmp/fac-%d/dic" % os.getuid()
    return os.path.join(d, os.environ.get("DIC_SESSION", "global"))


def session_read():
    try:
        with open(session_path()) as f:
            return f.read().strip()
    except OSError:
        die("no conversation in this session (DIC_SESSION=%s)"
            % os.environ.get("DIC_SESSION", "global"))


def session_write(mid):
    p = session_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        f.write(mid)
    os.replace(tmp, p)


# ---------------------------------------------------------------- models

def load_model(model_id):
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


# ---------------------------------------------------------------- adapters
#
# The intermediate representation of a conversation is a list of turns
#   {"role": "user"|"assistant", "blocks": [...], "raw": <provider json>}
# where a block is {"type": "text"|"image"|"tool_call"|"tool_result"|"thinking", ...}.
# "raw" is present only when the stored turn was produced by *this* api_type,
# in which case it is replayed verbatim; otherwise the blocks are converted.

def data_url(b):
    return "data:%s;base64,%s" % (b["mime_type"], base64.b64encode(b["data"]).decode())


def oai_auth(key):
    return {"Authorization": "Bearer " + key}


def oc_build(model, turns, system, params):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for t in turns:
        if "raw" in t:
            msgs.append(t["raw"])
            continue
        parts = []
        for b in t["blocks"]:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                parts.append({"type": "image_url", "image_url": {"url": data_url(b)}})
        if all(p["type"] == "text" for p in parts):
            parts = "\n\n".join(p["text"] for p in parts)
        msgs.append({"role": t["role"], "content": parts})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "stream_options": {"include_usage": True}}
    body.update(params)
    return body


def oc_parse(e, acc):
    u = e.get("usage")
    if u:
        acc["usage"] = (u.get("prompt_tokens"), u.get("completion_tokens"))
    out = ""
    for ch in e.get("choices") or []:
        out += (ch.get("delta") or {}).get("content") or ""
    if out:
        acc.setdefault("text", []).append(out)
    return out


def oc_finish(acc):
    return {"role": "assistant", "content": "".join(acc.get("text", []))}


def or_build(model, turns, system, params):
    inp = []
    for t in turns:
        if "raw" in t:
            inp.extend(t["raw"])
            continue
        content = []
        for b in t["blocks"]:
            if b["type"] == "text":
                content.append({"type": "input_text" if t["role"] == "user" else "output_text",
                                "text": b["text"]})
            elif b["type"] == "image":
                content.append({"type": "input_image", "image_url": data_url(b)})
        inp.append({"role": t["role"], "content": content})
    body = {"model": model["model_name"], "input": inp, "stream": True}
    if system:
        body["instructions"] = system
    body.update(params)
    return body


def or_parse(e, acc):
    ty = e.get("type")
    if ty == "response.output_text.delta":
        return e.get("delta") or ""
    if ty in ("response.completed", "response.incomplete", "response.failed"):
        r = e.get("response") or {}
        acc["raw"] = r.get("output") or []
        u = r.get("usage") or {}
        acc["usage"] = (u.get("input_tokens"), u.get("output_tokens"))
    return ""


def or_finish(acc):
    return acc.get("raw", [])


def am_auth(key):
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def am_build(model, turns, system, params):
    msgs = []
    for t in turns:
        if "raw" in t:
            msgs.append({"role": "assistant", "content": t["raw"]})
            continue
        content = []
        for b in t["blocks"]:
            if b["type"] == "text":
                content.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": b["mime_type"],
                    "data": base64.b64encode(b["data"]).decode()}})
        msgs.append({"role": t["role"], "content": content})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "max_tokens": 4096}
    if system:
        body["system"] = system
    body.update(params)
    return body


def am_parse(e, acc):
    ty = e.get("type")
    blocks = acc.setdefault("raw", [])
    if ty == "message_start":
        u = (e.get("message") or {}).get("usage") or {}
        acc["usage"] = (u.get("input_tokens"), u.get("output_tokens"))
    elif ty == "content_block_start":
        blocks.append(dict(e.get("content_block") or {}))
    elif ty == "content_block_delta" and blocks:
        b, d = blocks[-1], e.get("delta") or {}
        for k, dk in (("text", "text_delta"), ("thinking", "thinking_delta"),
                      ("signature", "signature_delta")):
            if d.get("type") == dk:
                b[k] = b.get(k, "") + d[k]
                return d[k] if k == "text" else ""
        if d.get("type") == "input_json_delta":
            b["_json"] = b.get("_json", "") + d["partial_json"]
    elif ty == "content_block_stop" and blocks:
        b = blocks[-1]
        if "_json" in b:
            b["input"] = json.loads(b.pop("_json") or "{}")
    elif ty == "message_delta":
        u = e.get("usage") or {}
        acc["usage"] = (acc.get("usage", (None, None))[0], u.get("output_tokens"))
    return ""


def am_finish(acc):
    return acc.get("raw", [])


ADAPTERS = {
    "openai-chat": dict(path="/chat/completions", auth=oai_auth,
                        build=oc_build, parse=oc_parse, finish=oc_finish),
    "openai-responses": dict(path="/responses", auth=oai_auth,
                             build=or_build, parse=or_parse, finish=or_finish),
    "anthropic-messages": dict(path="/messages", auth=am_auth,
                               build=am_build, parse=am_parse, finish=am_finish),
}


def adapter(api_type):
    if api_type in ADAPTERS:
        return ADAPTERS[api_type]
    f = os.path.join(ADAPTER_DIR, api_type + ".py")
    if not os.path.exists(f):
        die("unknown api_type: %s" % api_type)
    import importlib.util
    spec = importlib.util.spec_from_file_location("dic_adapter", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {k: getattr(mod, k) for k in ("path", "auth", "build", "parse", "finish")}


# ---------------------------------------------------------------- http

def sse(api_base, path, headers, body):
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


# ---------------------------------------------------------------- turns

def turns_from_rows(conn, rows, api_type):
    turns = []
    for r in rows:
        blocks = []
        for aid in json.loads(r["attachments"] or "[]"):
            a = conn.execute("SELECT mime_type,data FROM attachments WHERE aid=?", (aid,)).fetchone()
            if a:
                blocks.append({"type": "image", "mime_type": a["mime_type"], "data": a["data"]})
        blocks.append({"type": "text", "text": r["user"] or ""})
        turns.append({"role": "user", "blocks": blocks})
        t = {"role": "assistant", "blocks": [{"type": "text", "text": r["response"] or ""}]}
        if r["api_type"] == api_type and r["response_raw"]:
            t["raw"] = json.loads(r["response_raw"])
        turns.append(t)
    return turns


def normalize(turns):
    """Drop empty text blocks and merge consecutive same-role converted turns."""
    out = []
    for t in turns:
        if "raw" in t:
            out.append(t)
            continue
        t = dict(t, blocks=[b for b in t["blocks"] if b["type"] != "text" or b.get("text")])
        if not t["blocks"]:
            continue
        if out and out[-1]["role"] == t["role"] and "raw" not in out[-1]:
            out[-1]["blocks"] = out[-1]["blocks"] + t["blocks"]
        else:
            out.append(t)
    return out


# ---------------------------------------------------------------- main

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
    ad = adapter(api_type)
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

    body = ad["build"](model, turns, system, model.get("params") or {})
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(ad["auth"](key))
    headers.update(model.get("headers") or {})

    acc, out = {}, []
    for event in sse(model["api_base"], ad["path"], headers, body):
        text = ad["parse"](event, acc)
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
                 (mid, prompt, system, response, json.dumps(ad["finish"](acc)), prev_mid,
                  int(time.time()), json.dumps([a["aid"] for a in atts]),
                  model["model_id"], api_type, tin, tout))
    conn.commit()
    conn.close()
    session_write(mid)

    if args.extract:
        m = re.search(r"```[^\n]*\n(.*?)```", response, re.S)
        sys.stdout.write(m.group(1) if m else response)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
