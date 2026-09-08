# dic SPEC

`dic` is a minimalist CLI tool for working with chat LLMs models.
It is part of the `fac` suite of tools, which all use Latin-based names for different AI tasks.
(The idea is that working with AIs is like working with demons and magic,
and Latin is the traditional language for controlling demons and casting spells.)

`dic` is similar to simonw's `llm` tool but with an emphasis on speed and composability.

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
Code should be succinct and low token.

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

**TODO:**
Working with tools is currently not implemented, but planned for the future.
The database and internal message representation are designed so that this can be added
without breaking existing conversations.

**OUT OF SCOPE:**

1. The `llm` command provides tools for dynamically building the prompt (for example using "fragments" or "prompt templates").
    `dic` will never implement these features;
    they should instead live in separate programs that can be used to generate interesting prompts,
    and then have those prompts passed into `dic`.

1. The `llm` command provides tools for working with non-chat models (e.g. embedding models).
    `dic` is part of a suite of latin-named tools called `fac`,
    and these other model types are implemented in other tools.
    `dic` is only for chat models.

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
- `time`: the unix time that the message was sent
- `attachments`: a list of indexes into the attachments table
- `model_id`: the `model_id` used to generate the response
- `api_type`: the wire protocol used to generate the response (see "Model configuration")
- `tokens_input`, `tokens_output`: token counts reported by the API, if any

There must be an index on `prev_mid`, since reconstructing a conversation walks the tree upwards.

The `dic` tool accepts a `--mid` flag which allows continuing the conversation from any previous message,
and so the `messages` table must contain all information needed to ensure that the conversation can be reconstructed in the future.
This is why both `response` and `response_raw` are stored:
`response` is what a human wants to read, but `response_raw` is what must be sent back to the API,
and it may contain blocks (reasoning blocks, signatures, tool calls) that `dic` does not itself understand.
`dic` must be able to replay these blocks without understanding them.

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

All models are stored in the file `~/.config/fac/models.yaml`
This file is similar in structure to `llm`'s `~/.config/io.datasette.llm/extra-openai-models.yaml`.
In particular: The yaml file is structured as a list of objects,
with each object representing a single model that can be called.

An example is shown below:

```yaml
- model_id: groq-qwen
  model_name: qwen/qwen3.8-27b
  api_key_name: GROQ_API_KEY
  api_base: https://api.groq.com/openai/v1
  cost_input: 0.0
  cost_output: 0.0
- model_id: fable
  model_name: claude-fable-5.1
  api_key_name: ANTHROPIC_API_KEY
  api_base: https://api.anthropic.com/v1
  api_type: anthropic-messages
  cost_input: 10.0
  cost_output: 50.0
  headers:
    anthropic-beta: some-new-feature-2026-01-01
  params:
    thinking:
      type: enabled
      budget_tokens: 8000
```

The key semantics are as follows.
Required keys are:
1. `model_id`: the name of the model when using the `-m` flag to set the name.
2. `model_name`: the name of the model in the API
3. `api_key_name`: the name of the environment variable that stores the API key (semantics differ from `llm`)
4. `api_base`: the API endpoint, without the protocol-specific path suffix

Optional keys are:
1. `api_type`: which wire protocol to speak; defaults to `openai-chat`
2. `params`: a mapping merged verbatim into the JSON request body
3. `headers`: a mapping merged verbatim into the HTTP request headers
4. `cost_input` and `cost_output`: the price per million tokens for the input and output of the API call.
   At this point, batching and other types of cost-saving measures are not supported.

The `params` and `headers` keys are the primary extensibility mechanism.
Most new provider features are new JSON fields or new beta headers,
so these can be used before `dic` knows anything about them.
`dic` must not validate them: unknown keys are forwarded to the API and the API is allowed to reject them.
Client-side validation is what makes tools obsolete on release day.

The first entry in `models.yaml` is the default model when `-m` is not given.

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
