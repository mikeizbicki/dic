"""`dic()` itself: the program, minus the command line.

One call is one completion: resolve a model out of the config cache, rebuild
the conversation from sqlite, stream the answer to `out`, append the row to
the message tree, and return a `Reply` saying what was printed and how long
each phase took.  Nothing here reads `sys.argv`, and `sys.stdout` is only a
default, so this function is both the CLI's whole body and the library's
entry point.

    dic/dic.py          arguments, stdin, os._exit
    dic/client.py       dic(), Reply -- the control flow above
    dic/options.py      Flag: one declaration per knob, the CLI, the env vars
    dic/config.py       model and provider config: json sources, sqlite cache
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/tty.py          colour, errors, the cost line, the progress meters
    dic/adaptors/*.py   one wire protocol each
    dic/models.json     packaged defaults, overlaid by the user's files
"""
import http.client, json, os, re, sys, time, urllib.parse

from dic import config, output, price
from dic.options import flag, resolve
from dic.store import (INSERT, STATS, config_dir, db, history, normalize,
                       session_read, session_write, store_attachment,
                       turns_from_rows, ulid)
from dic.tty import (BLUE, RESET, THINKING, DicError, Line, pv_update,
                     report, summary, use_color)

HERE = os.path.dirname(os.path.abspath(__file__))


class Reply:
    """What one dic() call produced: the answer, the row, and the timings.

    `text` is what went to `out`, `raw` is what was stored in `response_raw`,
    and `timings` is the stamps dict in nanoseconds, so a caller can measure a
    phase or assert a ceiling without reading sqlite.  `mid` is the row's
    primary key: a later call passes it as `mid=` to continue the
    conversation, which is how `-c` and `--mid` are exercised in one process.
    `status` is None for a call that never reached the network, which is what
    --models, --aliases and --stats are.
    `usage` is the quantities the API reported, and only from those does the
    caller see what the call cost, because a price is read from the config as
    it is now and recorded on the row rather than returned.
    """
    __slots__ = ("text", "raw", "mid", "model_id", "api_type", "status",
                 "error", "usage", "paths", "mime", "timings")

    def __init__(self, text="", raw=None, mid=None, model_id=None,
                 api_type=None, status=None, error=None, usage=None,
                 paths=(), mime="text/plain", timings=None):
        self.text = text
        self.raw = raw
        self.mid = mid
        self.model_id = model_id
        self.api_type = api_type
        self.status = status
        self.error = error
        self.usage = usage or {}
        self.paths = list(paths)
        self.mime = mime
        self.timings = timings or {}


def load_adaptor(api_type, env):
    """The module implementing api_type: PATH, auth, build, parse, finish.

    Built-ins are dic.adaptors.<api_type with '-' as '_'>.  Anything else
    must be ~/.config/fac/adapters/<api_type>.py exporting the same five
    names and importing dic's own helpers by package name, e.g.
    `from dic.store import data_url`.  Only the selected adaptor is ever
    imported, and each one gets its own module name, so two of them cannot
    collide.
    """
    import importlib
    name = api_type.replace("-", "_")
    if os.path.exists(os.path.join(HERE, "adaptors", f"{name}.py")):
        return importlib.import_module(f"dic.adaptors.{name}")
    path = os.path.join(config_dir(env), "adapters", f"{api_type}.py")
    if not os.path.exists(path):
        raise DicError(f"unknown api_type: {api_type}")
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"dic_adaptor_{name}", path)
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


def code_block(text):
    """The body of the first fenced code block in text, or text unchanged.

    >>> code_block("prose\\n```python\\nx = 1\\n```\\nmore")
    'x = 1\\n'
    >>> code_block("no fence here")
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
            raise DicError(f"bad option (expected key=value): {override}")
        key, value = override.split("=", 1)
        try:
            value = json.loads(value)
        except ValueError:
            pass
        opts[key.strip()] = value
    return opts


def dic(prompt,
        model:        flag(short="-m", help="which model") = None,
        system:       flag(short="-s", help="system prompt") = None,
        attachment:   flag(short="-a", action="append", metavar="FILE",
                           help="attach a file") = (),
        option:       flag(short="-o", action="append", metavar="KEY=VALUE",
                           help="override a model option") = (),
        extract:      flag(short="-x", action="bool",
                           help="print only the first fenced code block") = False,
        path:         flag(long="--path", metavar="FILE",
                           help="write the answer to FILE, atomically") = None,
        force:        flag(short="-f", action="bool",
                           help="overwrite the file named by --path") = False,
        mime_type:    flag(long="--mime-type", metavar="TYPE",
                           help="the mime type of the answer") = None,
        cont:         flag(short="-c", long="--continue", action="bool",
                           help="continue this session's last conversation") = False,
        mid:          flag(long="--mid", help="continue from a message id") = None,
        pv_thinking:  flag(action="yes/no",
                           help="meter the reasoning instead of printing it") = None,
        pv_response:  flag(action="yes/no",
                           help="meter the answer on stderr as well") = None,
        verbosity:    flag(short="-v", action="count", env=False,
                           help="raise stderr verbosity; repeatable"
                                " (DIC_VERBOSITY sets the base)") = None,
        quiet:        flag(short="-q", action="bool",
                           help="print nothing to stderr but errors") = False,
        aliases:      flag(action="bool",
                           help="print shell alias definitions and exit") = False,
        models:       flag(action="bool",
                           help="list the configured model ids and exit") = False,
        models_file:  flag(help="a json file of model entries to overlay") = None,
        stats:        flag(action="bool",
                           help="print per-model statistics and exit") = False,
        t_start=None, env=None, out=None, err=None) -> Reply:
    """Talk to one model once, and record the turn.

    Every knob carries a Flag, so `argument > $DIC_<NAME> > model config` is
    resolved here, once, by options.resolve: the command line and the library
    cannot disagree about precedence.  A continuing conversation then supplies
    the model it last used, so only an explicit -m can switch providers inside
    a thread, exactly as only -s can rewrite its system prompt.
    `t_start` is the process's first
    instant when the CLI calls in and defaults to now, and `env`, `out` and
    `err` default to the process's, so a library call supplies none of them.

    What the call's usage cost is rated here, once, when the reply has ended:
    a price table is a config file, and the next invocation may have edited it,
    so the itemization is stored with the row instead of being recomputed by
    whoever reads it.  A call is never repriced.

    Returns a Reply, writing the answer to `out` and colour, the cost line
    and the meters to `err`.  A failure raises DicError, after the attempt
    has been recorded, so a failed call is countable too, and a ^C comes
    back as KeyboardInterrupt: neither exits the process, because a
    library's caller is not dic's to kill.
    """
    t_start = time.time_ns() if t_start is None else t_start
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    asked = model                   # -m, before $DIC_MODEL fills it in
    knobs = resolve(locals(), env)

    conn = db(env)
    config.sync(conn, env, knobs["models_file"])   # a stat per file; a parse only when one moved
    if knobs["aliases"] or knobs["models"]:
        text = (config.aliases(conn) if knobs["aliases"]
                else "".join(f"{name}\n" for name in config.model_ids(conn)))
        out.write(text)
        out.flush()
        return Reply(text=text)
    if knobs["stats"]:
        rows = conn.execute(STATS).fetchall()   # every number computed by sqlite
        text = "".join("\t".join("" if cell is None else str(cell) for cell in row)
                       + "\n"
                       for row in ([rows[0].keys()] if rows else [])
                       + [list(r) for r in rows])
        out.write(text)
        out.flush()
        return Reply(text=text)

    # stderr is graded: $DIC_VERBOSITY sets the grade, otherwise 1 on a
    # terminal and 0 on a pipe; -q drops it to 0 and each -v raises it by one
    verbosity = int(env.get("DIC_VERBOSITY") or (1 if err.isatty() else 0))
    if knobs["quiet"]:
        verbosity = 0
    elif knobs["verbosity"]:
        verbosity += knobs["verbosity"]

    if not prompt.strip() and not knobs["attachment"]:
        raise DicError("no prompt")

    prev_mid = knobs["mid"] or (session_read(env) if knobs["cont"] else None)
    if knobs["cont"] and not prev_mid:
        # -c with no pointer is an error: never a new conversation, and never
        # somebody else's
        raise DicError("no conversation in this session"
                       f" (DIC_SESSION={env.get('DIC_SESSION', 'global')})")
    rows = history(conn, prev_mid) if prev_mid else []
    if prev_mid and not rows:
        raise DicError(f"no such mid: {prev_mid}")

    # -m > the model this conversation last used > $DIC_MODEL > the default, so
    # -c carries on with the same provider unless the caller names another one
    knobs["model"] = (asked or (rows[-1]["model_id"] if rows else None)
                      or knobs["model"])
    model = config.resolve(conn, knobs["model"] or config.default_id(conn, env))
    api_type = model.get("api_type", "openai-chat")
    adaptor = load_adaptor(api_type, env)
    key_name = model.get("api_key_name")
    if not key_name:
        raise DicError(f"{model['model_id']}: no api_key_name configured")
    api_key = env.get(key_name)
    if not api_key:
        raise DicError(f"{key_name} is not set")

    # what the answer will be, settled before the call so that a missing or
    # taken --path fails before the network instead of after a whole video
    mime = knobs["mime_type"] or model.get("output") or "text/plain"
    if mime.startswith("text/"):
        output.check_free(knobs["path"], knobs["force"])
    else:
        if not knobs["path"]:
            raise DicError(f"{mime}: a non-text answer needs --path FILE")
        output.check_free(output.numbered(knobs["path"], 0), knobs["force"])
    sink = output.Sink(path=knobs["path"], force=knobs["force"],
                       out=out, err=err, env=env, verbosity=verbosity)

    turns, system = [], knobs["system"]
    if rows:
        turns = turns_from_rows(conn, rows, api_type)
        if system is None:
            system = rows[-1]["system"]
    elif system is None:
        # fresh conversations only: continuing one must never let an exported
        # default rewrite the system prompt the thread was started with
        system = env.get("DIC_SYSTEM") or model.get("system")

    attached = [store_attachment(conn, path) for path in knobs["attachment"]]
    turns.append({"role": "user",
                  "blocks": [dict(att) for att in attached]
                            + [{"type": "text", "text": prompt}]})
    turns = normalize(turns)

    opts = options(model, knobs["option"])
    prepare = getattr(adaptor, "prepare", None)
    if prepare is not None:            # fal reads URLs, so files go up first
        turns = prepare(model, api_key, turns, opts)
    body = adaptor.build(model, turns, system, opts)
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(adaptor.auth(api_key))
    headers.update(model.get("headers") or {})

    report(verbosity, 3,
           f"POST {model['api_base']}{adaptor.PATH} {json.dumps(body, default=str)}",
           err=err, env=env)
    stamps = {"t_start": t_start, "status": None, "error": None}
    acc, chunks, painted = {}, [], None
    # reasoning is progress, not content, so it is metered by default; the
    # answer is metered only where stderr is the only thing on the screen
    pv_thinking = True if knobs["pv_thinking"] is None else knobs["pv_thinking"]
    pv_response = (not out.isatty() if knobs["pv_response"] is None
                   else knobs["pv_response"])
    pv = {"name": "thinking"} if pv_thinking else None
    pv_resp = {"name": "response"} if pv_response else None
    line = Line(err, env)
    if pv is not None or pv_resp is not None:
        line.wait()             # a bare ttft clock until something arrives

    def close_meters():
        """Stop the wait clock and close each meter, so nothing repaints after."""
        # an error, a cancel, or a reply with no text; also ends a poll status
        line.first_token()
        if pv is not None:      # a reply that was nothing but reasoning
            pv_update(pv, final=True, err=err, env=env)
        if pv_resp is not None:
            pv_update(pv_resp, final=True, err=err, env=env)

    def billing():
        """The usage the API reported and what the price table makes of it.

        One place, reached from both the reply and the cancelled path, so a
        ^C's cost line and the reply's are the same numbers.  The itemization
        comes back with the total because it is what gets stored: the rule
        that priced each count is not recoverable from the config, which the
        next invocation may already have edited.
        """
        usage = acc.get("usage") or {}
        facts = price.facts(usage, acc.get("tier") or opts.get("service_tier")
                            or "default")
        total, items = price.rate(model, usage, facts)
        return usage, total, items

    def insert(mid, response, raw, outputs, usage, cost, items, status, error):
        """Append one turn to the tree: the reply, the failure, or the abort."""
        conn.execute(INSERT,
                     (mid, prompt, system, response, json.dumps(raw),
                      prev_mid, json.dumps([att["aid"] for att in attached]),
                      json.dumps(outputs), model["model_id"], api_type,
                      status, error, json.dumps(usage) if usage else None,
                      cost, json.dumps(items), price.price_hash(model),
                      stamps["t_start"], stamps.get("t_connect"),
                      stamps.get("t_request"), stamps.get("t_headers"),
                      stamps.get("t_first"), stamps["t_last"],
                      stamps["t_done"]))

    paint = use_color(out, env) and knobs["path"] is None

    try:
        call = getattr(adaptor, "call", None)
        stream = (call(model, api_key, body, line, stamps)
                  if call is not None else
                  events(model["api_base"], adaptor.PATH, headers, body, stamps))
        for event in stream:
            chunk, kind = adaptor.parse(event, acc)
            if not chunk:
                continue
            if kind != "blob":
                sink.end_blob()         # a blob ends when anything else arrives
            stamps.setdefault("t_first", time.time_ns())
            line.first_token()          # the wait is over, whatever arrived
            if kind == "thinking":
                if pv is not None:
                    pv_update(pv, chunk, err=err, env=env)
                elif use_color(err, env):
                    err.write(THINKING + chunk + RESET)
                else:
                    err.write(chunk)
                if pv is None:
                    err.flush()
                continue
            if kind == "blob":          # bytes: --path was checked above
                sink.write_blob(chunk)
                if pv_resp is not None:
                    pv_update(pv_resp, chunk, err=err, env=env)
                continue
            if pv is not None:      # the answer starts on a line of its own
                pv_update(pv, final=True, err=err, env=env)
            chunks.append(chunk)
            if pv_resp is not None:
                pv_update(pv_resp, chunk, err=err, env=env)
            if not knobs["extract"]:
                if paint and painted != kind:
                    sink.write((RESET if painted is not None else "") + BLUE)
                    painted = kind
                sink.write(chunk)
    except KeyboardInterrupt:
        # A ^C is the caller's, not dic's: close what this call opened and let
        # it propagate, because only the entry point knows that for the CLI a
        # cancelled call is a nonzero exit.  The bytes already received are not
        # thrown away: whatever was written stays in a .part, that name is on
        # the row as this turn's output, and the error says what is on disk.
        stamps["t_last"] = stamps["t_done"] = time.time_ns()
        close_meters()
        if not knobs["extract"] and knobs["path"] is None:
            if painted is not None:         # do not leave stdout painted blue
                out.write(RESET)
            if chunks and not chunks[-1].endswith("\n"):
                out.write("\n")
            out.flush()
        pending = sink.pending()
        sink.abandon()
        usage, cost, items = billing()
        insert(ulid(), "", adaptor.finish(acc),
               [{"path": pending, "mime_type": mime}] if pending else [],
               usage, cost, items, -1, "cancelled")
        conn.commit()
        report(verbosity, 1,
               summary(usage, items, None, stamps, verbosity),
               err=err, env=env)
        if pending:
            report(verbosity, 1, f"partial output left at {pending}",
                   err=err, env=env)
        raise
    except DicError as e:
        # a call() adaptor reports a failure by raising, so the transport that
        # would have written the row never ran and the line it leaves may be
        # mid-repaint: close the line, and record the attempt with the same -1
        # a stream that failed after a 200 gets, so a job that never answered
        # is still countable and its row is never continued by -c
        stamps["t_last"] = stamps["t_done"] = time.time_ns()
        close_meters()
        pending = sink.pending()
        sink.abandon()
        usage, cost, items = billing()
        insert(ulid(), "".join(chunks), adaptor.finish(acc),
               [{"path": pending, "mime_type": mime}] if pending else [],
               usage, cost, items, stamps.get("status") or -1, str(e))
        conn.commit()
        raise
    stamps["t_last"] = time.time_ns()
    close_meters()
    sink.end_blob()
    if acc.get("error"):
        # a stream that failed after a 200: -1 keeps the row out of the
        # averages, inside the error count, and off the session pointer,
        # exactly like a call that never reached the network
        stamps["status"], stamps["error"] = -1, acc["error"]
    elif stamps["status"] is None:
        # a call() adaptor replaces the transport and never sees a wire
        # status; a stream that ran to its end is a 200
        stamps["status"] = 200
    response = "".join(chunks)
    text = response
    if knobs["extract"]:
        text = code_block(response)
        sink.write(BLUE + text + RESET if paint else text)
    else:
        if painted is not None:
            sink.write(RESET)
        if response and not response.endswith("\n"):
            sink.write("\n")
            text += "\n"
    outputs = [{"path": p, "mime_type": mime} for p in sink.close()]

    usage, cost, items = billing()
    mid = ulid()
    stamps["t_done"] = time.time_ns()
    insert(mid, response, adaptor.finish(acc), outputs, usage, cost, items,
           stamps["status"], stamps["error"])
    conn.commit()
    conn.close()
    # a failed attempt is recorded for the error rate but the session pointer
    # is left alone, so the row is always a leaf and never replayed
    if stamps["status"] != 200:
        raise DicError(f"{stamps['status']} {stamps['error']}")
    session_write(mid, env)

    out.flush()

    if acc.get("stop") in ("length", "max_tokens", "max_output_tokens"):
        # a 200 that stopped because it ran out of room: the answer above is
        # cut off, and nothing else about the call says so
        report(verbosity, 1, f"truncated: {acc['stop']}", err=err, env=env)
    report(verbosity, 1,
           summary(usage, items, mid, stamps, verbosity),
           err=err, env=env)
    return Reply(text=text, raw=adaptor.finish(acc), mid=mid,
                 model_id=model["model_id"], api_type=api_type,
                 status=stamps["status"], error=stamps["error"],
                 usage=usage, paths=[o["path"] for o in outputs], mime=mime,
                 timings=stamps)
