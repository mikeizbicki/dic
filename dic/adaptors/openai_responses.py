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
    inp = []
    for t in turns:
        if "raw" in t:
            inp.extend(t["raw"])
            continue
        content = []
        for b in t["blocks"]:
            if b["type"] == "text":
                content.append({"type": "input_text" if t["role"] == "user" else "output_text",
                                "text": b["text"]})
            elif b["type"] == "image":
                content.append({"type": "input_image", "image_url": data_url(b)})
        inp.append({"role": t["role"], "content": content})
    body = {"model": model["model_name"], "input": inp, "stream": True}
    if system:
        body["instructions"] = system
    body.update(params)
    return body


def parse(e, acc):
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
    ty = e.get("type")
    if ty == "response.output_text.delta":
        return e.get("delta") or ""
    if ty in ("response.completed", "response.incomplete", "response.failed"):
        r = e.get("response") or {}
        acc["raw"] = r.get("output") or []
        u = r.get("usage") or {}
        acc["usage"] = (u.get("input_tokens"), u.get("output_tokens"))
    return ""


def finish(acc):
    """The server's own output items, stored verbatim.

    >>> finish({"raw": [{"type": "reasoning"}]})
    [{'type': 'reasoning'}]
    >>> finish({})
    []
    """
    return acc.get("raw", [])
