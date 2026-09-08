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


def parse(e, acc):
    """Rebuild acc["raw"] from the event stream; only text deltas are printed.

    Unknown block types accumulate as-is, so a feature dic has never heard of
    still round-trips into the database and back to the API.

    >>> acc = {}
    >>> parse({"type": "message_start",
    ...        "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}, acc)
    ''
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "text", "text": ""}}, acc)
    ''
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "text_delta", "text": "hi"}}, acc)
    'hi'
    >>> parse({"type": "message_delta", "usage": {"output_tokens": 1}}, acc)
    ''
    >>> acc["usage"], acc["raw"]
    ((5, 1), [{'type': 'text', 'text': 'hi'}])
    """
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


def finish(acc):
    """The reassembled content blocks, ready to replay verbatim.

    >>> finish({"raw": [{"type": "thinking", "signature": "s"}]})
    [{'type': 'thinking', 'signature': 's'}]
    >>> finish({})
    []
    """
    return acc.get("raw", [])
