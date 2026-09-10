"""Model configuration: json sources, a sqlite cache, one recursive query.

Configuration is plain json -- an object mapping an id to that entry's own
keys -- because `json` is C and `yaml` is a fifty millisecond import, which is
more than the time-to-first-token this program exists to protect.

An id is a `+`-separated chain of names, weakest first: `groq+qwen` is the
entry `qwen` layered onto the entry `groq`, and `thinking-high+groq+qwen`
layers a mixin under both.  Entries that exist only to be layered on
(providers, mixins) set `"abstract": true` and cannot be named with -m; that
is the only difference between a "provider" and a "model", so both files have
one schema and one namespace.

Resolution merges the chain with sqlite's json_patch (RFC 7386: objects merge
recursively, a null deletes), so a model states only what it changes about its
provider.  The parsed entries live in the `config` table of dic.db and are
reparsed only when a source file's mtime or size changes, so the hot path is
one stat per file and one query.
"""
import json, os

from store import CONFIG_DIR, die

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_PATH = os.path.join(HERE, "models.json")
PROVIDERS_PATH = os.path.join(CONFIG_DIR, "providers.json")
MODELS_PATH = os.path.join(CONFIG_DIR, "models.json")

# The ancestor chain of an id, folded root-first.  The first CTE is the same
# walk as store.history(): the same query answers "who is my conversational
# past" and "who is my configuration past".  The second linearizes it, the
# third folds it; depth is capped so a hand-written cycle cannot hang dic.
RESOLVE = """
WITH RECURSIVE anc(id, parent, keys, depth) AS (
    SELECT id, parent, keys, 0 FROM config WHERE id = ?
  UNION ALL
    SELECT c.id, c.parent, c.keys, anc.depth + 1
      FROM config c JOIN anc ON c.id = anc.parent WHERE anc.depth < 32),
ord(rn, keys) AS (
    SELECT row_number() OVER (ORDER BY depth DESC), keys FROM anc),
fold(rn, acc) AS (
    SELECT 1, keys FROM ord WHERE rn = 1
  UNION ALL
    SELECT o.rn, json_patch(f.acc, o.keys) FROM fold f JOIN ord o ON o.rn = f.rn + 1)
SELECT acc FROM fold ORDER BY rn DESC LIMIT 1
"""


def parent(model_id):
    """Everything left of the last '+', or None at the root of a chain.

    >>> parent("thinking-high+groq+qwen"), parent("groq")
    ('thinking-high+groq', None)
    """
    return model_id.rsplit("+", 1)[0] if "+" in model_id else None


def patch(a, b):
    """RFC 7386 merge of b into a, matching sqlite's json_patch exactly.

    Used only when two source files define the same id; the resolution of a
    chain is done by sqlite, with these same semantics.

    >>> patch({"options": {"max_tokens": 10, "top_p": 1}},
    ...       {"options": {"max_tokens": 20}})
    {'options': {'max_tokens': 20, 'top_p': 1}}
    >>> patch({"system": "x"}, {"system": None})
    {}
    """
    out = dict(a)
    for k, v in b.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = patch(out[k], v)
        else:
            out[k] = v
    return out


def sources():
    """The configuration files that exist, weakest first.

    The packaged defaults, then the user's providers and models, then
    $DIC_MODELS: later files are merged over earlier ones rather than
    replacing them, so a user file may change one key of one model.
    """
    paths = (DEFAULTS_PATH, PROVIDERS_PATH, MODELS_PATH, os.environ.get("DIC_MODELS"))
    return [p for p in paths if p and os.path.exists(p)]


def entries(path):
    """One source file as {id: keys}; ids beginning with '#' are comments."""
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        die("cannot read %s: %s" % (path, e))
    except ValueError as e:
        die("malformed json in %s: %s" % (path, e))
    if not isinstance(data, dict):
        die("%s: expected an object mapping model_id to its keys" % path)
    out = {}
    for k, v in data.items():
        if k.startswith("#"):
            continue
        if not isinstance(v, dict):
            die("%s: %s: expected an object" % (path, k))
        out[k] = v
    return out


def sync(conn):
    """Reparse the sources into the config cache, but only if one changed.

    A stat is ~20us and a parse is milliseconds, so the common invocation
    pays four stats and nothing else.
    """
    stamps = []
    for p in sources():
        st = os.stat(p)
        stamps.append((p, st.st_mtime_ns, st.st_size))
    have = [tuple(r) for r in conn.execute(
        "SELECT path, mtime, size FROM config_meta ORDER BY path")]
    if have == sorted(stamps):
        return

    merged, source = {}, {}
    for p, _, _ in stamps:
        for k, v in entries(p).items():
            merged[k], source[k] = patch(merged.get(k, {}), v), p

    rows = []
    for pos, (k, v) in enumerate(merged.items()):
        v = dict(v)
        abstract = 1 if v.pop("abstract", False) else 0
        alias = v.pop("alias", None)
        rows.append((k, parent(k), json.dumps(v), abstract, alias, pos, source[k]))
    with conn:
        conn.execute("DELETE FROM config")
        conn.execute("DELETE FROM config_meta")
        conn.executemany("INSERT INTO config VALUES (?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO config_meta VALUES (?,?,?)", stamps)


def defined(conn, model_id):
    """Whether model_id is a row in the config table."""
    return conn.execute("SELECT 1 FROM config WHERE id=?", (model_id,)).fetchone() is not None


def canonical(conn, name):
    """name itself, or the id of the entry whose `alias` is name.

    So `dic -m qwen` and the shell alias `qwen` name the same model without
    either being written down twice.
    """
    if defined(conn, name):
        return name
    r = conn.execute("SELECT id FROM config WHERE alias=?", (name,)).fetchone()
    return r["id"] if r else name


def materialize(conn, model_id):
    """Give an ad-hoc chain like `thinking-high+groq+qwen` real rows.

    Every prefix becomes a row whose own keys are those of the longest defined
    suffix of that prefix, so a combination nobody wrote down still means what
    it reads as: the mixin, then the provider, then the model.  Once inserted
    it is an ordinary entry and resolves by the ordinary query.
    """
    if defined(conn, model_id):
        return
    segs = model_id.split("+")
    if len(segs) == 1 or not defined(conn, segs[0]):
        die("unknown model: %s (try `dic --models`)" % model_id)
    new = []
    for i in range(1, len(segs) + 1):
        pid = "+".join(segs[:i])
        if defined(conn, pid):
            continue
        keys = "{}"
        for j in range(1, i):
            r = conn.execute("SELECT keys FROM config WHERE id=?",
                             ("+".join(segs[j:i]),)).fetchone()
            if r:
                keys = r["keys"]
                break
        new.append((pid, parent(pid), keys, 0, None, None, "ad-hoc"))
    if new:
        with conn:
            conn.executemany("INSERT OR IGNORE INTO config VALUES (?,?,?,?,?,?,?)", new)


def merged(conn, model_id):
    """The folded keys of model_id's chain, or {} if there is no such chain."""
    r = conn.execute(RESOLVE, (model_id,)).fetchone()
    return json.loads(r[0]) if r and r[0] else {}


def resolve(conn, model_id):
    """The fully merged configuration of model_id, with model_id itself added.

    The resolved id is what gets stored in the messages table, so a later
    --mid can say which entry produced a turn even after the files change.
    """
    model_id = canonical(conn, model_id)
    materialize(conn, model_id)
    r = conn.execute("SELECT abstract FROM config WHERE id=?", (model_id,)).fetchone()
    if r and r["abstract"]:
        die("%s is abstract (a provider or mixin), not a model" % model_id)
    cfg = merged(conn, model_id)
    if not cfg.get("model_name"):
        die("%s: no model_name configured (try `dic --models`)" % model_id)
    cfg["model_id"] = model_id
    return cfg


def ids(conn):
    """Every id that -m accepts, in configuration order."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM config WHERE abstract=0 AND pos IS NOT NULL ORDER BY pos")]


def default_id(conn):
    """The model used when neither -m nor $DIC_MODEL says otherwise.

    The first configured entry whose api_key_name is actually exported, so an
    install holding one provider's key needs no configuration at all; failing
    that the first entry, which then fails naming the key to export.
    """
    first = None
    for i in ids(conn):
        first = first or i
        if os.environ.get(merged(conn, i).get("api_key_name") or ""):
            return i
    if not first:
        die("no models configured: write %s" % MODELS_PATH)
    return first


def aliases(conn):
    """Shell alias definitions for every entry that names one.

    `dic.sh` evals this, so the shell name and the model name come from the
    same table and cannot drift apart.
    """
    return "".join("alias %s='dic -m %s'\n" % (r["alias"], r["id"])
                   for r in conn.execute(
                       "SELECT id, alias FROM config"
                       " WHERE alias IS NOT NULL AND abstract=0 ORDER BY pos"))
