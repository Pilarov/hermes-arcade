"""OpenAI-compatible API router for Hermes serve (Phase 9).

Mounts at /v1/ on the FastAPI app.
Supports: GET /v1/models, POST /v1/chat/completions (streaming).

Architecture:
  hermes serve → FastAPI (web_server.py)
    ├── /             → Dashboard UI
    ├── /api/         → Internal API
    └── /v1/          → OpenAI-compatible (this module)
         ├── /models               → list models
         └── /chat/completions     → chat (sync + streaming)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compat"])


class ChatMessage(BaseModel):
    role: str
    content: str | List[Dict[str, Any]]


class ChatCompletionRequest(BaseModel):
    model: str = ""
    messages: List[ChatMessage]
    stream: bool = False
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    stop: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Model listing
# ---------------------------------------------------------------------------

@router.get("/models")
async def list_models():
    """Return available models in OpenAI format."""
    from hermes_cli.config import load_config

    config = load_config()
    model_name = config.get("model", "hermes-agent")

    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": 0,
                "owned_by": "hermes",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Chat completion
# ---------------------------------------------------------------------------

def _read_config():
    """Read provider config once per request."""
    import os

    from dotenv import load_dotenv
    from hermes_cli.config import load_config

    config = load_config()

    # Load .env overrides
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    env_file = os.path.join(hermes_home, ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file, override=True)

    model = config.get("model", "deepseek-chat")
    provider = config.get("provider", "openai")
    prov = config.get("providers", {}).get(provider, {})

    api_key = prov.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    base_url = prov.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")

    return model, provider, api_key, base_url


def _make_agent(model: str, provider: str, api_key: str, base_url: str):
    """Create a fresh AIAgent instance."""
    from run_agent import AIAgent

    return AIAgent(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        quiet_mode=True,
        max_iterations=10,
    )


@router.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-compatible chat completion endpoint."""
    user_msg = _extract_user_message(req.messages)

    try:
        model, provider, api_key, base_url = _read_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Config error: {e}")

    if req.stream:
        return StreamingResponse(
            _stream_response(model, provider, api_key, base_url, user_msg, req.model),
            media_type="text/event-stream",
        )

    # Non-streaming
    try:
        agent = _make_agent(model, provider, api_key, base_url)
        response_text = agent.chat(user_msg) or ""
        agent.close()
    except Exception as e:
        logger.error("Chat error: %s", e, exc_info=True)
        response_text = f"Error: {e}"

    return _format_response(req.model, user_msg, response_text)


async def _stream_response(
    model: str,
    provider: str,
    api_key: str,
    base_url: str,
    user_msg: str,
    request_model: str,
) -> AsyncGenerator[str, None]:
    """Stream response chunks in SSE format."""
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    try:
        agent = _make_agent(model, provider, api_key, base_url)
        response_text = agent.chat(user_msg) or ""
        agent.close()
    except Exception as e:
        response_text = f"Error: {e}"

    # Split into chunks for streaming simulation
    words = response_text.split()
    for i in range(0, len(words), 3):
        chunk = " ".join(words[i : i + 3])
        chunk_data = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request_model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": chunk + " "},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk_data)}\n\n"
        await asyncio.sleep(0.05)

    # Final chunk with finish_reason
    final = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request_model,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_user_message(messages: List[ChatMessage]) -> str:
    """Extract user content from chat messages."""
    for msg in reversed(messages):
        if msg.role == "user":
            content = msg.content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            return str(content)
    return "Hello"


def _format_response(
    request_model: str, user_msg: str, response_text: str
) -> Dict[str, Any]:
    """Format a chat completion response in OpenAI format."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(user_msg.split()),
            "completion_tokens": len(response_text.split()),
            "total_tokens": len(user_msg.split()) + len(response_text.split()),
        },
    }
