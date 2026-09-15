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

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "sys", {"max_tokens": 10})
    >>> b["messages"]
    [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    >>> b["system"], b["max_tokens"]
    ('sys', 10)
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
    """
    kind = event.get("type")
    blocks = acc.setdefault("raw", [])
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
                return delta[field], "thinking" if field == "thinking" else ""
        if delta.get("type") == "input_json_delta":
            block["_json"] = block.get("_json", "") + delta["partial_json"]
    elif kind == "content_block_stop" and blocks:
        block = blocks[-1]
        if "_json" in block:
            block["input"] = json.loads(block.pop("_json") or "{}")
    elif kind == "message_delta":
        usage = event.get("usage") or {}
        acc["usage"] = (acc.get("usage", (None, None))[0],
                        usage.get("output_tokens"))
    return "", ""


def finish(acc):
    """The reassembled content blocks, ready to replay verbatim.

    >>> finish({"raw": [{"type": "thinking", "signature": "s"}]})
    [{'type': 'thinking', 'signature': 's'}]
    >>> finish({})
    []
    """
    return acc.get("raw", [])
