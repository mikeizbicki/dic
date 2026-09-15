"""The OpenAI /chat/completions protocol: the default, and the lingua franca.

Also spoken by OpenRouter, Groq, Together, vLLM, llama.cpp and ollama.

Every adaptor module exports the same five names:
    PATH            the path appended to the model's api_base
    auth(key)       headers carrying the API key
    build(...)      IR turns -> request body
    parse(event, acc)  one SSE event -> (text, kind) to print, state in acc
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
    for turn in turns:
        if "raw" in turn:
            msgs.append(turn["raw"])
            continue
        parts = []
        for block in turn["blocks"]:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                parts.append({"type": "image_url",
                              "image_url": {"url": data_url(block)}})
        if all(part["type"] == "text" for part in parts):
            parts = "\n\n".join(part["text"] for part in parts)
        msgs.append({"role": turn["role"], "content": parts})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "stream_options": {"include_usage": True}}
    body.update(params)
    return body


def parse(event, acc):
    """Return the (text, kind) of one delta event and record usage when it appears.

    Reasoning deltas -- DeepSeek, vLLM, OpenRouter and friends put them in
    delta.reasoning_content -- come back tagged "thinking" so the caller can
    paint them differently from the answer, and are kept out of the assistant
    message that is replayed next turn.

    >>> acc = {}
    >>> parse({"choices": [{"delta": {"content": "hi"}}]}, acc)
    ('hi', '')
    >>> parse({"choices": [{"delta": {"reasoning_content": "hmm"}}]}, acc)
    ('hmm', 'thinking')
    >>> parse({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}, acc)
    ('', '')
    >>> acc["usage"]
    (3, 1)
    """
    usage = event.get("usage")
    if usage:
        acc["usage"] = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
    text, kind = "", ""
    for choice in event.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content"):
            text += delta["content"]
        else:
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                text, kind = text + reasoning, "thinking"
    if text and kind != "thinking":
        acc.setdefault("text", []).append(text)
    return text, kind


def finish(acc):
    """The assistant message to replay next turn; this protocol has no opaque blocks.

    >>> finish({"text": ["ab", "c"]})
    {'role': 'assistant', 'content': 'abc'}
    """
    return {"role": "assistant", "content": "".join(acc.get("text", []))}
