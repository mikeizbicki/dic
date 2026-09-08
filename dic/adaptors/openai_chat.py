"""The OpenAI /chat/completions protocol: the default, and the lingua franca.

Also spoken by OpenRouter, Groq, Together, vLLM, llama.cpp and ollama.

Every adaptor module exports the same five names:
    PATH            the path appended to the model's api_base
    auth(key)       headers carrying the API key
    build(...)      IR turns -> request body
    parse(e, acc)   one SSE event -> text to print, accumulating state in acc
    finish(acc)     acc -> the JSON stored in messages.response_raw
"""
from store import data_url

PATH = "/chat/completions"


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}


def build(model, turns, system, params):
    """IR turns to a streaming chat-completions body; system is a leading message.

    A content list of pure text is collapsed to a plain string, which is what
    older and smaller servers actually accept.

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "be brief", {"temperature": 0})
    >>> b["messages"]
    [{'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'hi'}]
    >>> b["model"], b["stream"], b["temperature"]
    ('m', True, 0)
    """
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for t in turns:
        if "raw" in t:
            msgs.append(t["raw"])
            continue
        parts = []
        for b in t["blocks"]:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                parts.append({"type": "image_url", "image_url": {"url": data_url(b)}})
        if all(p["type"] == "text" for p in parts):
            parts = "\n\n".join(p["text"] for p in parts)
        msgs.append({"role": t["role"], "content": parts})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "stream_options": {"include_usage": True}}
    body.update(params)
    return body


def parse(e, acc):
    """Return the text of one delta event and record usage when it appears.

    >>> acc = {}
    >>> parse({"choices": [{"delta": {"content": "hi"}}]}, acc)
    'hi'
    >>> parse({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}, acc)
    ''
    >>> acc["usage"]
    (3, 1)
    """
    u = e.get("usage")
    if u:
        acc["usage"] = (u.get("prompt_tokens"), u.get("completion_tokens"))
    out = ""
    for ch in e.get("choices") or []:
        out += (ch.get("delta") or {}).get("content") or ""
    if out:
        acc.setdefault("text", []).append(out)
    return out


def finish(acc):
    """The assistant message to replay next turn; this protocol has no opaque blocks.

    >>> finish({"text": ["ab", "c"]})
    {'role': 'assistant', 'content': 'abc'}
    """
    return {"role": "assistant", "content": "".join(acc.get("text", []))}
