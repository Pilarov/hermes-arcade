# Блок 6: API Сервер — Безопасность + Стриминг (AUD-29,30)

## Статус: HIGH — нет аутентификации на проде

### Проблемы из аудита

| ID | Что | Где |
|----|-----|-----|
| AUD-29 | `/v1/chat/completions` без аутентификации | `openai_api.py:118-143` |
| AUD-30 | Стриминг симулированный (ждёт полный ответ → режет на слова) | `openai_api.py:146-196` |

### Дополнительные проблемы

| Проблема | Описание |
|----------|----------|
| Нет conversation history | `_extract_user_message` берёт только последнее user-сообщение |
| Нет tool calling | `tools` в запросе игнорируются |
| `.env` загрузка без профилей | `os.path.expanduser("~/.hermes")` вместо `get_hermes_home()` |
| Нет rate limiting | Любое количество запросов |

---

## ТЗ

### 6.1 API Key аутентификация

```python
from fastapi import Depends, HTTPException, Header
from hermes_cli.config import load_config

def verify_api_key(authorization: str = Header(None)):
    """Проверить API key из заголовка Authorization."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    
    # Поддерживаем оба формата: "Bearer sk-..." и "sk-..."
    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    
    config = load_config()
    valid_key = config.get("gateway", {}).get("api_server", {}).get("key", "")
    
    if not valid_key or token != valid_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return token

@router.post("/chat/completions")
def chat_completions(req: ChatCompletionRequest, api_key: str = Depends(verify_api_key)):
    ...
```

Конфиг:
```yaml
gateway:
  api_server:
    key: "sk-hermes-openwebui-key"  # сгенерировать при первом запуске
```

### 6.2 Реальный стриминг (если AIAgent поддерживает)

```python
async def _stream_response(model, provider, api_key, base_url, user_msg, request_model):
    """Реальный стриминг через AIAgent streaming API."""
    agent = _make_agent(model, provider, api_key, base_url)
    
    # Если AIAgent поддерживает streaming:
    for chunk in agent.chat_stream(user_msg):  # generator
        chunk_data = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request_model,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk_data)}\n\n"
    
    yield "data: [DONE]\n\n"
    agent.close()
```

Если AIAgent НЕ поддерживает streaming — оставить симулированный, но **задокументировать**.

### 6.3 Conversation history

```python
def _build_conversation(messages: list[ChatMessage]) -> str:
    """Построить conversation из всех сообщений, а не только последнего."""
    parts = []
    for msg in messages:
        role = msg.role
        content = msg.content
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        parts.append(f"[{role}]: {content}")
    return "\n".join(parts)
```

### 6.4 Profile-aware `.env` loading

```python
# Было:
load_dotenv(os.path.expanduser("~/.hermes/.env"))

# Стало:
from hermes_constants import get_hermes_home
load_dotenv(os.path.join(get_hermes_home(), ".env"))
```

---

## Acceptance Criteria

- [ ] Запрос без `Authorization` header → 401
- [ ] Запрос с неверным ключом → 401
- [ ] Запрос с верным ключом → 200 + ответ агента
- [ ] Стриминг: документирован как симулированный или реальный
- [ ] Conversation history: все сообщения передаются агенту
- [ ] Профили: `.env` загружается из правильного `HERMES_HOME`

## Ссылки

- FastAPI Depends: https://fastapi.tiangolo.com/tutorial/security/
- OpenAI API Reference: https://platform.openai.com/docs/api-reference/chat
