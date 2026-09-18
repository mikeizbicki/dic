"""Persistence: the sqlite message tree, attachment blobs, session pointers,
the parsed configuration cache that config.py fills, and colour policy.

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
    attachments TEXT,
    model_id TEXT,
    api_type TEXT,
    status INTEGER,              -- HTTP status, NULL if we never got one
    error TEXT,                  -- the server's body when status <> 200
    tokens_input INTEGER,
    tokens_output INTEGER,
    tokens_reasoning INTEGER,    -- billed but unprinted; tok/s needs it
    t_start INTEGER,             -- ns, before any import but `time`
    t_connect INTEGER,           -- TCP+TLS up
    t_request INTEGER,           -- request body written: end of our overhead
    t_headers INTEGER,           -- response headers in: queue + prefill start
    t_first INTEGER,             -- first token printed
    t_last INTEGER,              -- stream closed
    t_done INTEGER);             -- row written, just before exit
CREATE INDEX IF NOT EXISTS messages_prev_mid ON messages(prev_mid);
-- Every derived number is a subtraction of two stored instants, so the view
-- is a view: nothing is materialized, nothing can go stale, and no python
-- computes a statistic.  A provider is the head of the model_id chain.
CREATE VIEW IF NOT EXISTS stats AS SELECT
    mid, model_id, api_type, status,
    substr(model_id, 1, instr(model_id || '+', '+') - 1) AS provider,
    t_start / 1000000000 AS time,
    (t_connect - t_start)  / 1e6 AS ms_connect,
    (t_request - t_start)  / 1e6 AS ms_overhead,
    (t_headers - t_request)/ 1e6 AS ms_wait,
    (t_first   - t_start)  / 1e6 AS ms_ttft,
    (t_last    - t_first)  / 1e6 AS ms_stream,
    (t_done    - t_last)   / 1e6 AS ms_teardown,
    (t_done    - t_start)  / 1e6 AS ms_total,
    tokens_input, tokens_output, tokens_reasoning,
    1e9 * (tokens_output + coalesce(tokens_reasoning, 0))
        / nullif(t_last - t_first, 0) AS tok_per_sec
  FROM messages;
CREATE TABLE IF NOT EXISTS attachments (
    aid TEXT PRIMARY KEY,
    path TEXT,
    mime_type TEXT,
    data BLOB);
CREATE TABLE IF NOT EXISTS config (
    id TEXT PRIMARY KEY,
    parent TEXT,
    keys TEXT,
    abstract INTEGER,
    alias TEXT,
    pos INTEGER,
    source TEXT);
CREATE INDEX IF NOT EXISTS config_parent ON config(parent);
CREATE TABLE IF NOT EXISTS config_meta (
    path TEXT PRIMARY KEY,
    mtime INTEGER,
    size INTEGER);
"""

# What `dic --stats` prints: usage frequency and runtime performance are the
# same aggregate over the same rows, so they are one query.  Averages ignore
# failed calls, which are counted separately.
STATS = """
SELECT model_id, count(*) AS n, sum(status <> 200) AS errors,
       round(avg(ms_overhead) FILTER (WHERE status = 200), 1) AS overhead,
       round(avg(ms_wait)     FILTER (WHERE status = 200), 1) AS wait,
       round(avg(ms_ttft)     FILTER (WHERE status = 200), 1) AS ttft,
       round(max(ms_ttft)     FILTER (WHERE status = 200), 1) AS ttft_max,
       round(avg(tok_per_sec) FILTER (WHERE status = 200), 1) AS tok_s,
       round(avg(ms_total)    FILTER (WHERE status = 200), 1) AS total,
       sum(tokens_input) AS tin, sum(tokens_output) AS tout
  FROM stats GROUP BY model_id ORDER BY n DESC
"""

BLUE = "\033[38;5;39m"       # model output
ORANGE = "\033[38;5;208m"    # the cost summary
RED = "\033[31m"             # errors
THINKING = "\033[38;5;245m"             # reasoning: faded gray on the usual background
RESET = "\033[0m"


def use_color(stream):
    """Whether to emit ANSI colour on stream.

    $DIC_COLOR (never|auto|always) wins, then $NO_COLOR, then isatty, so a
    pipe gets clean text without the caller having to ask and a pager can ask
    for colour anyway.  dic never prints uncoloured text to a terminal: every
    stream has a meaning (blue output, orange cost, red error).
    """
    mode = os.environ.get("DIC_COLOR", "auto")
    if mode in ("never", "always"):
        return mode == "always"
    return stream.isatty() and not os.environ.get("NO_COLOR")


def die(msg):
    """Report an error in red on stderr and exit nonzero."""
    line = f"dic: {msg}\n"
    sys.stderr.write(RED + line + RESET if use_color(sys.stderr) else line)
    sys.exit(1)


def report(verbosity, level, msg):
    """Write msg to stderr in orange when verbosity has reached level.

    All of dic's stderr goes through here, so colour policy and verbosity
    policy are each stated exactly once.

    >>> report(0, 1, "not printed")
    """
    if verbosity < level:
        return
    line = f"{msg}\n"
    sys.stderr.write(ORANGE + line + RESET if use_color(sys.stderr) else line)
    sys.stderr.flush()


def pv_bytes(n):
    """A byte count as pv prints it: the number, then the unit in three columns.

    >>> pv_bytes(58), pv_bytes(2048)
    ('58.0  B', '2.0KiB')
    """
    value, unit = float(n), "B"
    for bigger in ("KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f}{unit:>3}"


def pv_clock(seconds):
    """H:MM:SS, as pv -t prints it.

    >>> pv_clock(64.7)
    '0:01:04'
    """
    whole = int(seconds)
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def pv_line(name, nbytes, seconds):
    """One `pv -N name -btr` status line.

    Used for the reasoning stream, which is progress and not content: the
    meter says how much a model thought without scrolling its answer away.

    >>> pv_line("thinking", 58, 4.0)
    'thinking: 58.0  B 0:00:04 [14.5  B/s]'
    """
    rate = nbytes / seconds if seconds else 0
    return (f"{name}: {pv_bytes(nbytes)} {pv_clock(seconds)}"
            f" [{pv_bytes(rate)}/s]")


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


def data_url(block):
    """An image block as an RFC 2397 data: URL, the form both OpenAI APIs take.

    >>> data_url({"type": "image", "mime_type": "image/png", "data": b"hi"})
    'data:image/png;base64,aGk='
    """
    encoded = base64.b64encode(block["data"]).decode()
    return f"data:{block['mime_type']};base64,{encoded}"


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
    columns = "mid,user,system,response,response_raw,prev_mid,api_type,attachments"
    qualified = ",".join(f"m.{c}" for c in columns.split(","))
    rows = conn.execute(
        f"WITH RECURSIVE chain({columns}) AS ("
        f"  SELECT {columns} FROM messages WHERE mid=?"
        "  UNION ALL"
        f"  SELECT {qualified} FROM messages m JOIN chain c ON m.mid=c.prev_mid)"
        " SELECT * FROM chain",
        (mid,)).fetchall()
    return list(reversed(rows))


def store_attachment(conn, path):
    """Save a file's original bytes and return it as an image block.

    Originals, never a provider's encoding: the same attachment may have to be
    re-encoded for a different provider later in the conversation.
    """
    with open(path, "rb") as f:
        data = f.read()
    mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    aid = ulid()
    conn.execute("INSERT INTO attachments VALUES (?,?,?,?)",
                 (aid, path, mime_type, data))
    return {"aid": aid, "type": "image", "mime_type": mime_type, "data": data}


def turns_from_rows(conn, rows, api_type):
    """History rows to IR turns, keeping "raw" where the api_type still matches."""
    turns = []
    for row in rows:
        blocks = []
        for aid in json.loads(row["attachments"] or "[]"):
            att = conn.execute("SELECT mime_type,data FROM attachments WHERE aid=?",
                               (aid,)).fetchone()
            if att:
                blocks.append({"type": "image", "mime_type": att["mime_type"],
                               "data": att["data"]})
        blocks.append({"type": "text", "text": row["user"] or ""})
        turns.append({"role": "user", "blocks": blocks})
        reply = {"role": "assistant",
                 "blocks": [{"type": "text", "text": row["response"] or ""}]}
        if row["api_type"] == api_type and row["response_raw"]:
            reply["raw"] = json.loads(row["response_raw"])
        turns.append(reply)
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
    for turn in turns:
        if "raw" in turn:
            out.append(turn)
            continue
        blocks = [b for b in turn["blocks"] if b["type"] != "text" or b.get("text")]
        if not blocks:
            continue
        if out and out[-1]["role"] == turn["role"] and "raw" not in out[-1]:
            out[-1]["blocks"] = out[-1]["blocks"] + blocks
        else:
            out.append(dict(turn, blocks=blocks))
    return out


# ---------------------------------------------------------------- session

def session_path():
    """The tmpfs file holding this shell session's last mid.

    One file per DIC_SESSION value, wiped on logout: no sessions table, no
    garbage collection, no locking, and the mtime is "last used" for free.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = (os.path.join(runtime, "fac", "dic") if runtime
            else f"/tmp/fac-{os.getuid()}/dic")
    return os.path.join(base, os.environ.get("DIC_SESSION", "global"))


def session_read():
    """The mid that -c continues; a missing pointer is fatal, never a new chat."""
    try:
        with open(session_path()) as f:
            return f.read().strip()
    except OSError:
        session = os.environ.get("DIC_SESSION", "global")
        die(f"no conversation in this session (DIC_SESSION={session})")


def session_write(mid):
    """Point this session at mid, atomically."""
    path = session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(mid)
    os.replace(tmp, path)
