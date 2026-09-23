"""Anthropic's native /messages protocol.

Preferred over Anthropic's OpenAI-compatible shim, which silently ignores
unsupported fields and does not expose thinking or tool-use blocks faithfully.
The assistant turn is reassembled block by block from the stream so that
thinking blocks and their signatures can be replayed exactly.

Exports PATH, auth, build, parse, finish; see adaptors/openai_chat.py.
"""
import base64, json

PATH = "/messages"


def auth(key):
    """>>> auth("k")
    {'x-api-key': 'k', 'anthropic-version': '2023-06-01'}
    """
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def build(model, turns, system, params):
    """IR turns to a streaming messages body; system is a top-level parameter.

    max_tokens is required by the API, so a default is supplied and params
    (merged last) can override it like anything else.

    A turn carrying "raw" is replayed verbatim, and an image becomes a base64
    source block.

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "sys", {"max_tokens": 10})
    >>> b["messages"]
    [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    >>> b["system"], b["max_tokens"]
    ('sys', 10)
    >>> b = build({"model_name": "m"},
    ...           [{"role": "assistant", "blocks": [], "raw": [{"type": "thinking"}]},
    ...            {"role": "user", "blocks": [
    ...                {"type": "image", "mime_type": "image/png", "data": b"hi"}]}],
    ...           None, {})
    >>> b["messages"][0]
    {'role': 'assistant', 'content': [{'type': 'thinking'}]}
    >>> b["messages"][1]["content"][0]["source"]
    {'type': 'base64', 'media_type': 'image/png', 'data': 'aGk='}
    """
    msgs = []
    for turn in turns:
        if "raw" in turn:
            msgs.append({"role": "assistant", "content": turn["raw"]})
            continue
        content = []
        for block in turn["blocks"]:
            if block["type"] == "text":
                content.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": block["mime_type"],
                    "data": base64.b64encode(block["data"]).decode()}})
        msgs.append({"role": turn["role"], "content": content})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "max_tokens": 4096}
    if system:
        body["system"] = system
    body.update(params)
    return body


def parse(event, acc):
    """Rebuild acc["raw"] from the event stream, yielding (text, kind) to print.

    kind is "thinking" for a reasoning delta and "" for anything else, so the
    caller can paint thinking differently from the answer.

    Unknown block types accumulate as-is, so a feature dic has never heard of
    still round-trips into the database and back to the API.

    >>> acc = {}
    >>> parse({"type": "message_start",
    ...        "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}, acc)
    ('', '')
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "text", "text": ""}}, acc)
    ('', '')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "text_delta", "text": "hi"}}, acc)
    ('hi', '')
    >>> parse({"type": "message_delta", "usage": {"output_tokens": 1}}, acc)
    ('', '')
    >>> acc["usage"], acc["raw"]
    ((5, 1), [{'type': 'text', 'text': 'hi'}])
    >>> acc = {}
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "thinking", "thinking": ""}}, acc)
    ('', '')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "thinking_delta", "thinking": "hmm"}}, acc)
    ('hmm', 'thinking')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "signature_delta", "signature": "sig"}}, acc)
    ('', '')
    >>> acc["raw"]
    [{'type': 'thinking', 'thinking': 'hmm', 'signature': 'sig'}]
    >>> acc = {}
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "tool_use", "name": "f"}}, acc)
    ('', '')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "input_json_delta", "partial_json": '{"a": 1}'}}, acc)
    ('', '')
    >>> parse({"type": "content_block_stop"}, acc)
    ('', '')
    >>> acc["raw"]
    [{'type': 'tool_use', 'name': 'f', 'input': {'a': 1}}]
    """
    kind = event.get("type")
    blocks = acc.setdefault("raw", [])
    if kind == "error":
        # an error frame after a 200: not a reply, and never a conversation
        error = event.get("error") or {}
        acc["error"] = (f"{error.get('type') or 'error'}: "
                        f"{error.get('message') or 'stream failed'}")
        return "", ""
    if kind == "message_start":
        usage = (event.get("message") or {}).get("usage") or {}
        acc["usage"] = (usage.get("input_tokens"), usage.get("output_tokens"))
    elif kind == "content_block_start":
        blocks.append(dict(event.get("content_block") or {}))
    elif kind == "content_block_delta" and blocks:
        block, delta = blocks[-1], event.get("delta") or {}
        for field, delta_type in (("text", "text_delta"),
                                  ("thinking", "thinking_delta"),
                                  ("signature", "signature_delta")):
            if delta.get("type") == delta_type:
                block[field] = block.get(field, "") + delta[field]
                if field == "signature":
                    return "", ""          # opaque; replayed, never printed
                return delta[field], "thinking" if field == "thinking" else ""
        if delta.get("type") == "input_json_delta":
            block["_json"] = block.get("_json", "") + delta["partial_json"]
    elif kind == "content_block_stop" and blocks:
        block = blocks[-1]
        if "_json" in block:
            partial = block.pop("_json")   # popped once: json.loads may fail
            try:
                block["input"] = json.loads(partial or "{}")
            except ValueError:
                block["input"] = {}        # a tool call cut off mid-arguments
    elif kind == "message_delta":
        usage = event.get("usage") or {}
        acc["usage"] = (acc.get("usage", (None, None))[0],
                        usage.get("output_tokens"))
        acc["stop"] = ((event.get("delta") or {}).get("stop_reason")
                       or acc.get("stop"))
    return "", ""


def finish(acc):
    """The reassembled content blocks, ready to replay verbatim.

    A block whose stream ended before its content_block_stop still carries the
    scratch key the argument deltas accumulated in: it is dic's own, never the
    provider's, so it is dropped rather than replayed.

    >>> finish({"raw": [{"type": "thinking", "signature": "s"}]})
    [{'type': 'thinking', 'signature': 's'}]
    >>> finish({"raw": [{"type": "tool_use", "_json": '{"a"'}]})
    [{'type': 'tool_use', 'input': {}}]
    >>> finish({})
    []
    """
    for block in acc.get("raw", []):
        if "_json" in block:
            block.pop("_json")
            block.setdefault("input", {})
    return acc.get("raw", [])
