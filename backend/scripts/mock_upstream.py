"""
A mock *upstream* for Slate's verification scripts — everything Slate calls out to:

  * an OpenAI-compatible model server:  POST /v1/chat/completions, POST /v1/embeddings
  * a generic HTTP service for workflow HTTP nodes:  /echo, /flaky, /slow, /status/{code},
    /big, /redirect, plus /_log and /_reset so tests can see exactly what arrived

It is test tooling. Its embeddings are a deterministic hashed bag-of-words (64 dims) — enough to
make retrieval tests meaningful for *lexical* overlap, and deliberately NOT a claim about semantic
quality. Its chat model just echoes the end of the last message, which is what lets a test prove
which prompt actually reached the model.

    MOCK_PORT=9300 python backend/scripts/mock_upstream.py
"""

import asyncio
import hashlib
import json
import math
import os
import re

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

PORT = int(os.getenv("MOCK_PORT", "9300"))
DIMS = 64
app = FastAPI(title="Slate mock upstream")
LOG: list = []
FLAKY: dict = {}


async def record(request: Request) -> dict:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = raw.decode("utf-8", errors="replace")[:2000]
    entry = {"method": request.method, "path": request.url.path, "query": dict(request.query_params),
             "headers": {k.lower(): v for k, v in request.headers.items()}, "body": body}
    LOG.append(entry)
    return entry


def embed(text: str) -> list:
    vec = [0.0] * DIMS
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.md5(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % DIMS
        vec[index] += (1.0 if digest[4] % 2 == 0 else -1.0)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@app.post("/v1/chat/completions")
async def chat(request: Request):
    entry = await record(request)
    body = entry["body"] if isinstance(entry["body"], dict) else {}
    model = body.get("model", "")
    if model == "fail-500":
        return PlainTextResponse("upstream model exploded", status_code=500)
    if model == "slow":
        await asyncio.sleep(3)
    if model == "bad-shape":
        return {"not": "openai"}
    messages = body.get("messages") or [{"content": ""}]
    last = str(messages[-1].get("content", ""))
    return {"id": "mock-1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"MOCK-REPLY[{model}] …{last[-160:]}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(last) // 4, "completion_tokens": 8, "total_tokens": len(last) // 4 + 8}}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    entry = await record(request)
    body = entry["body"] if isinstance(entry["body"], dict) else {}
    inputs = body.get("input", [])
    inputs = [inputs] if isinstance(inputs, str) else inputs
    if body.get("model") == "fail-500":
        return PlainTextResponse("embedding upstream exploded", status_code=500)
    return {"object": "list", "model": body.get("model", ""), "data": [{"object": "embedding", "index": i, "embedding": embed(str(t))} for i, t in enumerate(inputs)],
            "usage": {"prompt_tokens": sum(len(str(t)) // 4 for t in inputs), "total_tokens": sum(len(str(t)) // 4 for t in inputs)}}


@app.api_route("/echo", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def echo(request: Request):
    entry = await record(request)
    return {"method": entry["method"], "query": entry["query"], "headers": entry["headers"], "body": entry["body"]}


@app.get("/flaky")
async def flaky(request: Request, key: str = "k", fail: int = 2):
    await record(request)
    FLAKY[key] = FLAKY.get(key, 0) + 1
    if FLAKY[key] <= fail:
        return JSONResponse({"error": "temporarily unavailable", "attempt": FLAKY[key]}, status_code=503)
    return {"ok": True, "attempt": FLAKY[key]}


@app.get("/slow")
async def slow(request: Request, seconds: float = 2.0):
    await record(request)
    await asyncio.sleep(seconds)
    return {"slept": seconds}


@app.get("/status/{code}")
async def status(code: int, request: Request):
    await record(request)
    return JSONResponse({"status": code}, status_code=code)


@app.get("/big")
async def big(request: Request, kb: int = 100):
    await record(request)
    return Response(b"x" * (kb * 1024), media_type="text/plain")


@app.get("/redirect")
async def redirect(request: Request):
    await record(request)
    return RedirectResponse("/echo?redirected=1", status_code=302)


@app.get("/_log")
async def get_log():
    return LOG


@app.post("/_reset")
async def reset():
    LOG.clear()
    FLAKY.clear()
    return {"ok": True}


@app.get("/_health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
