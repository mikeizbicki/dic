"""Persistence: the sqlite message tree, attachment blobs, session pointers.

Everything in dic that touches the disk lives here.  The rest of the program
sees only the provider-neutral intermediate representation of a conversation:
a list of turns

    {"role": "user"|"assistant", "blocks": [block, ...], "raw": <provider json>}

where a block is {"type": "text"|"image"|"tool_call"|"tool_result"|"thinking",
...}.  "raw" is present only when the stored turn was produced by the api_type
we are about to call again, in which case the adaptor replays it verbatim and
full fidelity (signatures, reasoning, cache prefix) is preserved; otherwise
the adaptor converts the blocks and provider-opaque ones are dropped whole.
"""
import base64, json, mimetypes, os, sqlite3, sys, time

CONFIG_DIR = os.path.expanduser("~/.config/fac")
DB_PATH = os.path.join(CONFIG_DIR, "dic.db")

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
    """Report an error on stderr and exit nonzero."""
    sys.stderr.write("dic: %s\n" % msg)
    sys.exit(1)


B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid():
    """A ULID: 48 bits of millisecond time then 80 random bits, Crockford base32.

    Message ids therefore sort lexicographically by creation time.

    >>> u = ulid()
    >>> len(u), set(u) <= set(B32)
    (26, True)
    """
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(B32[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def data_url(b):
    """An image block as an RFC 2397 data: URL, the form both OpenAI APIs take.

    >>> data_url({"type": "image", "mime_type": "image/png", "data": b"hi"})
    'data:image/png;base64,aGk='
    """
    return "data:%s;base64,%s" % (b["mime_type"], base64.b64encode(b["data"]).decode())


# ---------------------------------------------------------------- sqlite

def db():
    """Open ~/.config/fac/dic.db, creating the append-only schema if needed."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def history(conn, mid):
    """The ancestor chain of mid, oldest first (one recursive query, one trip)."""
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
    """Save a file's original bytes and return it as an image block.

    Originals, never a provider's encoding: the same attachment may have to be
    re-encoded for a different provider later in the conversation.
    """
    with open(path, "rb") as f:
        data = f.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    aid = ulid()
    conn.execute("INSERT INTO attachments VALUES (?,?,?,?)", (aid, path, mime, data))
    return {"aid": aid, "type": "image", "mime_type": mime, "data": data}


def turns_from_rows(conn, rows, api_type):
    """History rows to IR turns, keeping "raw" where the api_type still matches."""
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
    """Drop empty text blocks and merge consecutive same-role converted turns.

    Turns replayed verbatim are never merged or rewritten.

    >>> normalize([{"role": "user", "blocks": [{"type": "text", "text": ""}]},
    ...            {"role": "user", "blocks": [{"type": "text", "text": "a"}]},
    ...            {"role": "user", "blocks": [{"type": "text", "text": "b"}]}])
    [{'role': 'user', 'blocks': [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}]}]
    >>> normalize([{"role": "assistant", "blocks": [], "raw": ["opaque"]}])
    [{'role': 'assistant', 'blocks': [], 'raw': ['opaque']}]
    """
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


# ---------------------------------------------------------------- session

def session_path():
    """The tmpfs file holding this shell session's last mid.

    One file per DIC_SESSION value, wiped on logout: no sessions table, no
    garbage collection, no locking, and the mtime is "last used" for free.
    """
    rt = os.environ.get("XDG_RUNTIME_DIR")
    d = os.path.join(rt, "fac", "dic") if rt else "/tmp/fac-%d/dic" % os.getuid()
    return os.path.join(d, os.environ.get("DIC_SESSION", "global"))


def session_read():
    """The mid that -c continues; a missing pointer is fatal, never a new chat."""
    try:
        with open(session_path()) as f:
            return f.read().strip()
    except OSError:
        die("no conversation in this session (DIC_SESSION=%s)"
            % os.environ.get("DIC_SESSION", "global"))


def session_write(mid):
    """Point this session at mid, atomically."""
    p = session_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        f.write(mid)
    os.replace(tmp, p)
