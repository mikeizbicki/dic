"""OpenAI's newer /responses protocol.

History is a flat list of input items rather than messages, and the assistant
turn is stored as the server's own output list so that reasoning items (which
are encrypted and meaningless to dic) can be replayed untouched.

Exports PATH, auth, build, parse, finish; see adaptors/openai_chat.py.
"""
from store import data_url

PATH = "/responses"


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}


def build(model, turns, system, params):
    """IR turns to a streaming responses body; system is the top-level instructions.

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "sys", {})
    >>> b["input"]
    [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}]
    >>> b["instructions"], b["stream"]
    ('sys', True)
    """
    items = []
    for turn in turns:
        if "raw" in turn:
            items.extend(turn["raw"])
            continue
        content = []
        for block in turn["blocks"]:
            if block["type"] == "text":
                content.append({"type": "input_text" if turn["role"] == "user"
                                        else "output_text",
                                "text": block["text"]})
            elif block["type"] == "image":
                content.append({"type": "input_image", "image_url": data_url(block)})
        items.append({"role": turn["role"], "content": content})
    body = {"model": model["model_name"], "input": items, "stream": True}
    if system:
        body["instructions"] = system
    body.update(params)
    return body


def parse(event, acc):
    """Text deltas print; the terminal event carries the output list and usage.

    >>> acc = {}
    >>> parse({"type": "response.output_text.delta", "delta": "hi"}, acc)
    'hi'
    >>> parse({"type": "response.completed", "response":
    ...        {"output": [{"type": "message"}],
    ...         "usage": {"input_tokens": 2, "output_tokens": 1}}}, acc)
    ''
    >>> acc["usage"], acc["raw"]
    ((2, 1), [{'type': 'message'}])
    """
    kind = event.get("type")
    if kind == "response.output_text.delta":
        return event.get("delta") or ""
    if kind in ("response.completed", "response.incomplete", "response.failed"):
        resp = event.get("response") or {}
        acc["raw"] = resp.get("output") or []
        usage = resp.get("usage") or {}
        acc["usage"] = (usage.get("input_tokens"), usage.get("output_tokens"))
    return ""


def finish(acc):
    """The server's own output items, stored verbatim.

    >>> finish({"raw": [{"type": "reasoning"}]})
    [{'type': 'reasoning'}]
    >>> finish({})
    []
    """
    return acc.get("raw", [])
