# dic SPEC

`dic` is a minimalist CLI tool for working with chat LLMs models.

"Dic" is Latin for the command "speak".
The idea is that working with AIs is like working with demons and magic,
and Latin is the traditional language for controlling demons and casting spells.

`dic` is similar to simonw's `llm` tool but with an emphasis on speed and Unix-style composability.

## Code Priorities

`dic` is designed for expert CLI users, and so it prioritizes speed.
The system must have minimal latency (i.e. minimizing time-to-first-token displayed),
and the program must terminate "instantly" after receiving the last token.
Some low-level methods of achieving this speed include:
1. importing as few libraries as possible,
1. having as little boilerplate code as possible,
1. streaming the output to stdout.

In particular, `dic` must never import a vendor SDK (`openai`, `anthropic`, etc.);
these packages cost hundreds of milliseconds at import time,
which is more than the time-to-first-token of most APIs.
All API access is plain HTTP against the documented JSON endpoints.

## Code

Program architecture should be simple and not overengineered.
Intro data structures students should find the architecture to be the "obvious" way they would have done things,
with as few as possible files/classes/functions.
Code should be succinct and low token but human friendly.
Include useful variable names and doctests,
but do not go overboard on verbosity.

## Options

`dic` supports the following options.
Whenever possible, names and semantics remain the same as simonw's `llm`.

| short form | long form      | meaning |
| ---------- | -------------- | ------- |
| `-m`       | `--model`      | which model to use |
| `-s`       | `--system`     | system prompt |
| `-a`       | `--attachment` | attach the file |
| `-x`       | `--extract`    | extracts first fenced code block |
| `-c`       | `--continue`   | continue the previous conversation in this session |
|            | `--mid`        | continue the conversation from the given message id |
|            | `--aliases`    | print shell alias definitions for `dic.sh` to eval |
|            | `--models`     | list the configured model ids |
|            | `--stats`      | print per-model runtime and usage statistics |
| `-v`       | `--verbose`    | raise stderr verbosity; repeatable |
| `-q`       | `--quiet`      | print nothing to stderr but errors |

### Defaults

Two environment variables supply defaults for the two flags a user tends to want
set the same way every time:

| variable     | default for |
| ------------ | ----------- |
| `DIC_MODEL`  | `-m`        |
| `DIC_SYSTEM` | `-s`        |
| `DIC_VERBOSITY` | `-v`/`-q` |

These are environment variables rather than a second config file because a config
file would add a stat and a parse to the latency path for something a shell startup
file already does, and because the environment is inherited by subshells and
overridable for a single command: `DIC_MODEL=gpt dic ...`.

If `DIC_MODEL` names a model that is not configured, this is an error;
a stale export must never silently fall back to some other model.

`DIC_SYSTEM` applies only when starting a *new* conversation.
With `-c` or `--mid` the system prompt is inherited from the conversation,
so that an exported default cannot rewrite the system prompt of a running thread
and leave the `system` column no longer describing it.
The full precedence is `-s`, then the inherited prompt, then `DIC_SYSTEM`,
then the model's own `system` key.

### Color

Every stream `dic` writes has a meaning and therefore a color:
blue for model output, orange for the cost summary, red for errors.
There is deliberately no uncolored terminal output.
Color is emitted when `$DIC_COLOR` is `always`,
suppressed when it is `never`,
and otherwise used only when the stream is a terminal and `$NO_COLOR` is unset,
so a pipe gets clean text without the caller having to ask.

### Verbosity

stderr is graded, and the grade is one integer resolved once at startup:
`$DIC_VERBOSITY` if set, otherwise `1` when stderr is a terminal and `0` when it
is not, then moved by `-q` (to 0) or `-v` (each repetition one higher).

| level | stderr |
| ----- | ------ |
| 0 | errors only |
| 1 | the cost and `--mid` line |
| 2 | and the timings of this call: overhead, ttft, tok/s, total |
| 3 | and the request body and URL before it is sent |

All of it goes through one `report(verbosity, level, msg)` in `store.py`.

**TODO:**
1. Working with tools is currently not implemented, but planned for the future.
    The database and internal message representation are designed so that this can be added
    without breaking existing conversations.

2. Many providers allow prompt caching to reduce cost of input tokens.
    It's not clear to me the best way to structure this from the cli or in the various config files.

3. Eventually we should be able to generate images/audio/video/etc using other API endpoints.
    The existing adaptors framework will need to be lightly adjusted to support these additional output types and a way for specifying file output or mimetype will be needed,
    but the biggest problem will be finding a good way to support pricing (which can have very different structures for different providers) and for handling non-sync APIs.
    For example video files often take several minutes to generate, and fal.ai uses a polling strategy in its API to check on status.

4. There is no cross-provider standard for listing models or their prices.
    `GET /v1/models` is OpenAI-shaped and served by Groq, Together, vLLM and OpenRouter,
    but only OpenRouter reports `pricing`, and Anthropic reports none.
    A future `dic --sync` should hit each provider's list endpoint and *generate* the
    entries under a provider id, never fetching prices on the latency path.

5. Eventually this system should be usable as a library and support async requests to allow many API calls to happen concurrently.
    We want these async requests to simultaneously not complicate the code too much and not slow down the CLI interface where time to first token is critical.

6. The `stats` view has no notion of a percentile, only averages and maxima,
    because sqlite has no `percentile()` without an extension.
    A median is expressible with a window function over the view and should replace
    `avg` once the query is worth the length.

**OUT OF SCOPE:**

1. The `llm` command provides mechanisms for dynamically building the prompt (for example using "fragments" or "prompt templates").
    `dic` will never implement these features;
    they should instead live in separate programs that can be used to generate interesting prompts,
    and then have those prompts passed into `dic`.

1. The `llm` command provides mechanisms for working with non-chat models (e.g. embedding models).
    These non-chat models should be provided separate stand alone programs.

1. `llm` uses a plugin per provider, which means that a newly released model cannot be used
    until its plugin has been updated, and that plugin load times dominate startup.
    `dic` will never have a per-provider plugin system.
    Adapters are per *wire protocol* (of which there are only a handful) and model names are opaque
    passthrough strings, so a model released today works today.

## Conversation History

A major change between `dic` and `llm` is how conversation history is stored.
In `llm`, conversations are always "linear" and always "global",
but in `dic` conversations can have non-linear tree structures and are local to the current shell session.

The way this works is that all messages sent with `dic` have a "message id" or "mid" which serves as the primary key in a sqlite table "messages" located at `~/.config/fac/dic.db`.
The messages table has the following columns:
- `mid`: a ULID
- `user`: the user prompt
- `system`: the system prompt
- `response`: the plain text of the API response, as streamed to stdout
- `response_raw`: the provider's own JSON content blocks for the assistant turn, stored verbatim
- `prev_mid`: (default NULL) previous `mid` if the conversation is multi-turn; importantly, two messages can share the same `prev_mid`, so the structure forms a tree and not a linked list
- `attachments`: a list of indexes into the attachments table
- `model_id`: the `model_id` used to generate the response
- `api_type`: the wire protocol used to generate the response (see "Model configuration")
- `status`, `error`: the HTTP status of the call and the server's message when it was not 200
- `tokens_input`, `tokens_output`: token counts reported by the API, if any
- `tokens_reasoning`: tokens billed but never printed, if the API reports them;
  without this a thinking model's tokens/second is wrong
- `t_start`, `t_connect`, `t_request`, `t_headers`, `t_first`, `t_last`, `t_done`:
  nanoseconds since the epoch at each phase boundary of the call (see "Timing")

There is no `time` column: it is `t_start / 1000000000`, and a value that is a
function of another value is not stored.

There must be an index on `prev_mid`, since reconstructing a conversation walks the tree upwards.

`dic` accepts a `--mid` flag which allows continuing the conversation from any previous message,
and so the `messages` table must contain all information needed to ensure that the conversation can be reconstructed in the future.
This is why both `response` and `response_raw` are stored:
`response` is what a human wants to read, but `response_raw` is what must be sent back to the API,
and it may contain blocks (reasoning blocks, signatures, tool calls) that `dic` does not itself understand.
`dic` must be able to replay these blocks without understanding them.

## Timing and statistics

`dic` stores *instants*, never durations: every metric anyone has asked for is the
difference of two of them, and subtraction is sqlite's job.

| column | taken |
| ------ | ----- |
| `t_start`   | the first line of `dic.py`, before every import but `time` |
| `t_connect` | TCP and TLS established |
| `t_request` | the request body has been written |
| `t_headers` | the response headers have arrived |
| `t_first`   | the first event carrying text |
| `t_last`    | the stream closed |
| `t_done`    | the row is written, immediately before exit |

So "time to first API call" — `dic`'s own overhead, including the interpreter, the
config query, the history query and whichever adaptor was imported — is
`t_request - t_start`, and it will show the cost of a tool loop or a slow adaptor
when those exist.  Time to first token is `t_first - t_start`, generation rate is
`tokens_output / (t_last - t_first)`, and end of response is `t_last - t_start`.

A failed call is stored too, with its `status` and `error` and an empty `response`,
so that an error rate is countable and a provider's failures do not silently vanish.
The session pointer is *not* moved on failure, so a failed row is always a leaf and
is never replayed into a later conversation.

### The stats view

Nothing derived is materialized.  A `stats` view names each duration, in
milliseconds, and each rate:

```sql
CREATE VIEW stats AS SELECT
    mid, model_id, api_type, status,
    substr(model_id, 1, instr(model_id || '+', '+') - 1) AS provider,
    t_start / 1000000000 AS time,
    (t_request - t_start) / 1e6 AS ms_overhead,
    (t_first   - t_start) / 1e6 AS ms_ttft,
    ...
  FROM messages;
```

`dic --stats` is then a single `GROUP BY model_id` over that view: frequency of use
(`count(*)`) and runtime performance (`avg(ms_ttft)`, `avg(tok_per_sec)`, ...) are
the same aggregate over the same rows, so they are one query, with averages
restricted to `status = 200`.  Python joins the resulting cells with tabs and
computes nothing; the output is therefore already `sort`- and `awk`-shaped.
This query scans `messages` and will grow slower with the table, which is
acceptable: `--stats` is never on the latency path.

Grouping by `provider` instead, or filtering by `time`, is a matter of editing the
one query — which is why the view stores the parts rather than the answers.

The `dic` tool also accepts a `-c` flag to continue the previous conversation.
In `llm`, the `-c` flag is global, and so if you use the `llm` tool in two separate terminal bash sessions, they will follow the same conversation.
In `dic`, however, `-c` is local to the current shell session.
The session is identified by the `DIC_SESSION` environment variable,
which the user is expected to set in their shell startup file, e.g.

```bash
export DIC_SESSION=$(uuidgen)
```

If `DIC_SESSION` is unset, the session key is the literal string `global`,
and so `-c` behaves exactly as it does in `llm`.
This makes the mechanism trivially explicit:
subshells, pipelines and command substitutions inherit the variable and therefore the conversation;
two terminals get two conversations;
two terminals that deliberately export the same value share one conversation;
and `DIC_SESSION=foo dic -c ...` is a one-off way to address a named conversation.

The pointer for each session is stored as a single file at
`$XDG_RUNTIME_DIR/fac/dic/<session>` whose contents are the last `mid` written by that session.
This location is a tmpfs, so the pointers are wiped on logout and reboot with no garbage collection,
no liveness checking, and no locking beyond an atomic rename.
The file's mtime serves as the "last used" time for free.
There is deliberately no `sessions` table in sqlite:
session state is ephemeral, and the message tree in sqlite is immutable and append-only.
If `XDG_RUNTIME_DIR` is unset, fall back to `/tmp/fac-$UID/dic/`.

If `-c` is passed we assume that `--mid` is not passed,
and the correct behavior is to first do a lookup to find the appropriate `--mid` value and then proceed as if that value was passed.
That is, `-c` is pure sugar for `--mid $(cat $XDG_RUNTIME_DIR/fac/dic/$DIC_SESSION)`.
If both are passed, `--mid` wins.
If `-c` is passed and the session pointer does not exist, this is an error and `dic` exits nonzero;
it must never silently start a new conversation or continue somebody else's.

A third table `attachments` stores blobs of file attachments used with `-a`.
It has a ULID as primary key; a `path` column, a `mime-type` column, and a `data` column which is the raw bytes of the attachment.
Attachments are always stored as the original bytes and never in a provider's encoding,
because the same attachment may later have to be re-encoded for a different provider.

## Model configuration

Models are configured in JSON, not YAML.
`json` is a C extension already in the interpreter; `import yaml` alone costs tens of
milliseconds, which is more than the time-to-first-token `dic` exists to protect.
`tomllib` is pure Python and its array-of-tables syntax is painful at a thousand models.

There are four sources, weakest first:

1. `dic/models.json`, the defaults shipped with the package,
2. `~/.config/fac/providers.json`, the user's providers,
3. `~/.config/fac/models.json`, the user's models,
4. `$DIC_MODELS`, if it names a file.

Each file is one JSON *object* mapping a model id to that entry's own keys,
so lookup is a dict lookup and not a scan.
Ids beginning with `#` are ignored, which is where comments go.
Entries with the same id in two files are merged key by key,
later files winning, so a user file may change one price without restating a model.

There is no separate schema for providers.
`providers.json` and `models.json` differ only in which entries people tend to put in them.

### Inheritance

A model id is a `+`-separated chain of names, weakest first:

```json
{
  "groq":      {"abstract": true,
                "api_base": "https://api.groq.com/openai/v1",
                "api_key_name": "GROQ_API_KEY"},
  "groq+qwen": {"model_name": "qwen/qwen3.8-27b", "alias": "qwen",
                "options": {"max_tokens": 200}},
  "thinking-high": {"abstract": true,
                    "options": {"thinking": {"type": "enabled",
                                             "budget_tokens": 8000}}}
}
```

`dic -m groq+qwen` resolves the chain `groq`, then `groq+qwen`,
merging each entry into the accumulated result with RFC 7386 semantics:
objects merge recursively and a `null` deletes a key.
A model therefore states only what it changes about its provider,
and `-o` still overrides individual options on top of that.

Entries that exist only to be inherited from — providers, and mixins such as
`thinking-high` — set `"abstract": true`.
They cannot be named with `-m`, and that is the *only* difference between a provider
and a model: one schema, one namespace, one table.

Chains nobody wrote down also work.
`dic -m thinking-high+groq+qwen` materializes each prefix of the chain,
giving each the keys of the longest *defined* suffix of that prefix,
so the combination means exactly what it reads as: the mixin, then the provider, then the model.

The delimiter is `+` rather than `/`, because `/` already appears inside model names
(`qwen/qwen3.8-27b`, OpenRouter's `anthropic/claude-...`);
rather than `[]`, because those are glob metacharacters and `dic -m [groq]qwen`
is an error under zsh's default `nomatch`;
and rather than a space, because that would require quoting on every invocation.
Composition is associative and rightmost-wins, so a linear chain loses nothing
that a nested notation would have bought: a named intermediate is just another entry.

### Keys

After inheritance an entry must have:
1. `model_name`: the name of the model in the API
2. `api_key_name`: the name of the environment variable that stores the API key (semantics differ from `llm`)
3. `api_base`: the API endpoint, without the protocol-specific path suffix

Optional keys are:
1. `api_type`: which wire protocol to speak; defaults to `openai-chat`
2. `options`: a mapping merged verbatim into the JSON request body
3. `headers`: a mapping merged verbatim into the HTTP request headers
4. `cost_input` and `cost_output`: the price per million tokens for the input and output of the API call.
   At this point, batching and other types of cost-saving measures are not supported.
5. `system`: a default system prompt for this model, used when the conversation is
   new and neither `-s` nor `DIC_SYSTEM` is given.
   Unlike `DIC_SYSTEM` this is per-model, which is what a model-specific house style needs.
6. `alias`: the shell alias and `-m` shorthand for this entry.
7. `abstract`: this entry may only be inherited from.

The `options` and `headers` keys are the primary extensibility mechanism.
Most new provider features are new JSON fields or new beta headers,
so these can be used before `dic` knows anything about them.
`dic` must not validate them: unknown keys are forwarded to the API and the API is allowed to reject them.
Client-side validation is what makes tools obsolete on release day.

### The config cache

Parsing configuration on every invocation is a cost paid by every invocation,
so the parsed entries live in `dic.db` beside the messages:

```sql
CREATE TABLE config(id TEXT PRIMARY KEY, parent TEXT, keys TEXT,
                    abstract INTEGER, alias TEXT, pos INTEGER, source TEXT);
CREATE TABLE config_meta(path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER);
```

`keys` is the entry's *own* JSON only; `parent` is everything left of the last `+`,
`NULL` at the root.
Startup stats each source file (~20 µs each) and reparses only when an mtime or size
no longer matches `config_meta`, in which case both tables are rewritten in one transaction.

Resolution is then a single round trip: a recursive CTE walks the ancestor chain —
the same shape of query as `history()`, which walks a conversation's ancestor chain,
so *the same query answers "who is my configuration past" and "who is my conversational past"* —
linearizes it root-first, and folds it with sqlite's `json_patch`,
which is RFC 7386 merge for free.
Depth is capped so a hand-written cycle cannot hang `dic`.

### Defaults, aliases and listing

When `-m` is not given, the model is `$DIC_MODEL` if it is set.
Otherwise `dic` uses the first configured entry whose `api_key_name` is actually set
in the environment, so an install holding only one provider's key needs no configuration at all;
failing even that, the first entry, which then fails with a message naming the key to export.
An id that is not configured is an error, never a silent fallback.

`dic --models` lists every non-abstract id; `dic --aliases` prints
`alias qwen='dic -m groq+qwen'` for every entry with an `alias` key,
which `dic.sh` evals.
Shell names and model names therefore come from one table and cannot drift apart,
and the same list is what shell completion should use.

The fully resolved id is what is stored in the `model_id` column,
so a conversation still says which entry produced each turn after the files are edited.

### Wire protocols

`api_type` selects one of a small number of built-in adapters.
Adapters correspond to protocols, not vendors, and each is a few dozen lines that
builds a request body and parses a stream of server-sent events.

| `api_type`           | notes |
| -------------------- | ----- |
| `openai-chat`        | the default; also OpenRouter, Groq, Together, vLLM, llama.cpp, ollama, ... |
| `openai-responses`   | OpenAI's newer endpoint |
| `anthropic-messages` | Anthropic's native endpoint |

Note that Anthropic also offers an OpenAI-compatible shim at the same `api_base`,
which can be used with `api_type: openai-chat`,
but it silently ignores unsupported fields and does not expose reasoning or tool-use blocks faithfully,
so `anthropic-messages` is preferred.

As a final escape hatch, if `api_type` names a file `~/.config/fac/adapters/<api_type>.py`,
that file is imported and used as the adapter.
This import happens only when a model that references it is actually selected,
so it costs nothing at startup for everyone else.

### Switching models mid-conversation

Because `--mid` can point at any message, a conversation may span several providers.
To make this work, `dic` has a provider-neutral intermediate representation of a conversation turn:
a list of `{role, blocks}`, where each block is one of
`text`, `image` (raw bytes plus mime type), `tool_call`, `tool_result`, or `thinking`.
Each adapter implements a conversion to and from this representation.

When building a request, `dic` walks the ancestor chain and for each historical turn:

1. if the stored `api_type` equals the target `api_type`, the stored `response_raw` blocks are replayed
   verbatim; this preserves full fidelity, provider signatures, and any prompt-cache prefix;
2. otherwise the turn is converted through the intermediate representation.

Text, system prompts, attachments and tool calls survive conversion.
Provider-specific opaque blocks do not:
Anthropic `thinking` blocks carry signatures that are only valid for the model that produced them,
and OpenAI reasoning items are encrypted.
On a cross-provider transition these blocks are dropped entirely, never partially;
they remain in the database for display and audit.
Prompt caching is also lost at the point of transition, by definition.

Conversion must additionally normalize the history into the form the target requires:
merge consecutive same-role turns, drop empty text blocks,
and move the system prompt between a top-level parameter and a leading message as appropriate.
A single-provider conversation never touches this code path.

## shell integration

The script `dic.sh` contains useful aliases and defaults.
