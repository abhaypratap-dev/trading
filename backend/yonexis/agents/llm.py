"""Minimal OpenAI-compatible chat client (works with Ollama, vLLM, LM Studio, Azure OpenAI, ...)."""
from __future__ import annotations

import json
import re

import httpx

from yonexis.config import settings


class LLMError(RuntimeError):
    pass


def chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 4000, model: str | None = None) -> str:
    if not settings.LLM_ENABLED:
        raise LLMError("the AI model is switched off in this deployment (LLM_ENABLED=false) - use the local version for AI writing features")
    url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    body = {"model": model or settings.LLM_MODEL, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False}
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}"}
    if "azure" in settings.LLM_BASE_URL:
        headers = {"api-key": settings.LLM_API_KEY}
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=settings.LLM_TIMEOUT)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as e:
        raise LLMError(f"LLM call to {url} failed: {e}. Is the model server running and is {body['model']} pulled?") from e


def ask(system: str, user: str, **kw) -> str:
    return chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply."""
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise LLMError(f"no JSON in model reply: {text[:300]}")
        raw = text[start:]
        depth, end = 0, None
        opener = raw[0]
        closer = "}" if opener == "{" else "]"
        in_str = esc = False
        for i, ch in enumerate(raw):
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        raw = raw[:end] if end else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned invalid JSON: {e}: {raw[:300]}") from e


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def health() -> dict:
    if not settings.LLM_ENABLED:
        return {"ok": False, "disabled": True, "model": None, "error": "switched off in this deployment (LLM_ENABLED=false)"}
    try:
        base = settings.LLM_BASE_URL.rstrip("/")
        r = httpx.get(base + "/models", timeout=5, headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"})
        models = [m.get("id") for m in r.json().get("data", [])]
        return {"ok": r.status_code == 200, "base_url": base, "model": settings.LLM_MODEL,
                "model_available": settings.LLM_MODEL in models or any(settings.LLM_MODEL in (m or "") for m in models),
                "models": models}
    except Exception as e:
        return {"ok": False, "base_url": settings.LLM_BASE_URL, "model": settings.LLM_MODEL, "error": str(e)}
