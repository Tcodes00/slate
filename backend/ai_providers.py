"""
Multi-provider LLM routing. Anthropic's shape (backend/copilot.py's
original direct implementation) is the reference; OpenAI and Gemini each
get their own real request/response translation here, not a thin
Anthropic-shaped wrapper that would break the moment either provider's
actual API contract mattered.

Every provider fails the exact same way when its own API key env var is
missing — a clear HTTPException-friendly error, not a silent fallback to
a different provider or a fake response — same contract
ANTHROPIC_API_KEY has always had everywhere else in this codebase.

Honesty note (see README.md): OpenAI's and Gemini's APIs aren't reachable
from the sandbox this was built in — network egress there is allowlisted
to specific domains, and neither api.openai.com nor
generativelanguage.googleapis.com is on it. Anthropic's real API also
isn't reachable without a configured key. So every provider here was
verified for what's actually checkable without a live key: the missing-
key path raises the right error before any network call is attempted,
the request-building logic produces the correct shape for each
provider's real, documented API contract, and the message-format
translation (OpenAI's flat role/content list vs. Gemini's nested
parts-with-"model"-instead-of-"assistant" shape) is unit tested against
those contracts directly. What's NOT verified: an actual response from
OpenAI or Gemini, the same gap Anthropic itself has had here all along.
"""

import os
from typing import Optional

import httpx

import outbound

PROVIDERS = {
    "anthropic": {"name": "Anthropic (Claude)", "default_model": "claude-sonnet-5", "key_env": "ANTHROPIC_API_KEY"},
    "openai": {"name": "OpenAI (GPT)", "default_model": "gpt-4o", "key_env": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1/chat/completions"},
    "gemini": {"name": "Google (Gemini)", "default_model": "gemini-2.0-flash", "key_env": "GEMINI_API_KEY"},
    # xAI and DeepSeek both publish OpenAI-compatible chat/completions
    # endpoints (documented, not assumed) — same wire format as OpenAI
    # itself, just a different base URL and key, so they share
    # _call_openai_compatible() below instead of two more copies of the
    # same translation.
    "grok": {"name": "xAI (Grok)", "default_model": "grok-2-latest", "key_env": "GROK_API_KEY", "base_url": "https://api.x.ai/v1/chat/completions"},
    "deepseek": {"name": "DeepSeek", "default_model": "deepseek-chat", "key_env": "DEEPSEEK_API_KEY", "base_url": "https://api.deepseek.com/v1/chat/completions"},
    # A model server the *operator* runs next to Slate (Ollama, vLLM, LM Studio,
    # llama.cpp, LiteLLM...): one environment variable, LOCAL_LLM_BASE_URL.
    # Same OpenAI-compatible wire format; the key is optional (Ollama wants
    # none). Per-tenant model servers are "custom:<id>" endpoints instead —
    # see routers/model_endpoints.py. key_env here is the URL variable on
    # purpose: callers print it in their "isn't configured" message.
    "local": {"name": "Local model (Ollama / vLLM / LM Studio)", "default_model": os.getenv("LOCAL_LLM_MODEL", "llama3.1"), "key_env": "LOCAL_LLM_BASE_URL"},
}

# Set by routers/model_endpoints.py at import: (tenant_id, endpoint_id) -> dict | None.
# Kept as a callback so this module stays free of database imports.
_endpoint_resolver = None


def set_endpoint_resolver(fn) -> None:
    global _endpoint_resolver
    _endpoint_resolver = fn


def chat_url(base: str) -> str:
    """Accepts a bare host, a /v1 base, or the full /chat/completions URL —
    people paste all three."""
    base = base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def embeddings_url(base: str) -> str:
    base = base.strip().rstrip("/")
    if base.endswith("/embeddings"):
        return base
    if base.endswith("/v1"):
        return base + "/embeddings"
    return base + "/v1/embeddings"


def _parse_custom(provider: str) -> Optional[tuple]:
    """'custom:<tenant_id>:<endpoint_id>' -> (tenant_id, endpoint_id). The tenant id is
    written by get_effective_provider_model() after it has verified the endpoint
    belongs to the calling tenant — never taken from user input."""
    parts = provider.split(":")
    if len(parts) != 3 or parts[0] != "custom":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _resolve_custom(provider: str) -> Optional[dict]:
    parsed = _parse_custom(provider)
    if parsed is None or _endpoint_resolver is None:
        return None
    return _endpoint_resolver(*parsed)


def is_configured(provider: str) -> bool:
    if provider.startswith("custom:"):
        # Whether the endpoint still exists and belongs to this tenant is checked when it is
        # called (and fails closed there); callers print PROVIDERS[provider] when this is False.
        return True
    entry = PROVIDERS.get(provider)
    return bool(entry and os.getenv(entry["key_env"], ""))


def default_model(provider: str) -> str:
    if provider.startswith("custom:"):
        endpoint = _resolve_custom(provider)
        return endpoint["model"] if endpoint else ""
    entry = PROVIDERS.get(provider)
    return entry["default_model"] if entry else ""


async def call_llm(provider: str, model: str, messages: list, max_tokens: int) -> str:
    """messages is the same flat [{"role": "user"|"assistant", "content": str}, ...]
    shape used throughout this codebase already (see copilot.py) — each
    provider function below translates it into that provider's actual
    wire format, not the other way around, so callers never need to
    know which provider they're talking to."""
    if provider.startswith("custom:"):
        return await _call_custom(provider, model, messages, max_tokens)
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider '{provider}'")
    key = os.getenv(PROVIDERS[provider]["key_env"], "")
    if not key:
        raise RuntimeError(f"{PROVIDERS[provider]['key_env']} is not configured on the server")

    if provider == "anthropic":
        return await _call_anthropic(key, model, messages, max_tokens)
    if provider == "gemini":
        return await _call_gemini(key, model, messages, max_tokens)
    if provider == "local":
        # `key` is the base URL here (see the registry comment); the API key is optional.
        return await _call_openai_compatible(chat_url(key), PROVIDERS["local"]["name"], os.getenv("LOCAL_LLM_API_KEY", ""), model, messages, max_tokens)
    # openai, grok, deepseek all speak the same OpenAI-compatible wire
    # format — only the base URL and error label differ.
    return await _call_openai_compatible(PROVIDERS[provider]["base_url"], PROVIDERS[provider]["name"], key, model, messages, max_tokens)


async def _call_anthropic(key: str, model: str, messages: list, max_tokens: int) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "messages": messages},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API error: {resp.text[:300]}")
    data = resp.json()
    return "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()


async def _call_openai_compatible(base_url: str, provider_name: str, key: str, model: str, messages: list, max_tokens: int) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            base_url,
            headers={"content-type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})},
            json={"model": model, "max_tokens": max_tokens, "messages": messages},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"{provider_name} API error: {resp.text[:300]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _to_gemini_contents(messages: list) -> list:
    """Gemini's real shape: no flat role/content list. Each turn is
    {"role": "user"|"model", "parts": [{"text": ...}]} — note "model",
    not "assistant", which is the one detail a copy-pasted-from-OpenAI
    translation would get wrong."""
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    return contents


async def _call_gemini(key: str, model: str, messages: list, max_tokens: int) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            headers={"content-type": "application/json"},
            json={"contents": _to_gemini_contents(messages), "generationConfig": {"maxOutputTokens": max_tokens}},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API error: {resp.text[:300]}")
    data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


async def _call_custom(provider: str, model: str, messages: list, max_tokens: int) -> str:
    """A tenant's own OpenAI-compatible server. Goes through outbound.safe_request:
    the URL is tenant-controlled, so it gets the SSRF treatment — resolved, checked,
    connection pinned, no redirects, bounded response."""
    endpoint = _resolve_custom(provider)
    if endpoint is None:
        raise RuntimeError("The selected custom model endpoint no longer exists, is disabled, or doesn't belong to this workspace")
    headers = {"content-type": "application/json"}
    if endpoint["api_key"]:
        if endpoint["auth_header"].lower() == "authorization":
            headers["Authorization"] = f"Bearer {endpoint['api_key']}"
        else:
            headers[endpoint["auth_header"]] = endpoint["api_key"]
    try:
        resp = await outbound.safe_request(
            "POST", chat_url(endpoint["base_url"]), headers=headers, timeout=60, label=f"Model endpoint '{endpoint['name']}'",
            json={"model": model or endpoint["model"], "max_tokens": max_tokens, "messages": messages},
        )
    except outbound.OutboundError as e:
        raise RuntimeError(str(e))
    if resp.status_code != 200:
        raise RuntimeError(f"{endpoint['name']} API error (HTTP {resp.status_code}): {resp.text()[:300]}")
    try:
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise RuntimeError(f"{endpoint['name']} answered, but not in the OpenAI chat-completions format")
