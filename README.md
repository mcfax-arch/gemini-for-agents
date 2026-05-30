# Gemini for Agents

<p align="center">
  <img src="logo.png" width="200" alt="gemini-for-agents logo">
</p>

**Google Gemini** → **OpenAI-compatible API**, заточенный под AI-агентов (Hermes, Claude Code, Codex, OpenCode и любые другие).

Использует Gemini Web API напрямую — **без API-ключа**, бесплатно, с полной поддержкой tool calling.

---

## Чем отличается от оригинала

Это форк [gemini-web2api](https://github.com/Sophomoresty/gemini-web2api) с фокусом на работу AI-агентов:

- **Детерминированный planner** — когда Gemini «забывает» вызвать инструмент, синтезатор анализирует запрос и вызывает нужный tool самостоятельно
- **Retry/repair tool calls** — если Gemini вернула сломанный JSON или пропустила tool call, сервер автоматически перестраивает запрос
- **Compact tool definitions** — схемы инструментов упакованы в 2-3x меньший размер без потери функциональности
- **Парсинг под Windows** — корректные POSIX-пути, извлечение shell команд из естественного языка

## Возможности

| Возможность | Статус |
|-------------|--------|
| OpenAI-совместимый API (`/v1/chat/completions`) | ✅ |
| Tool calling (функции) | ✅ **улучшено** |
| Streaming (SSE) | ✅ |
| Несколько моделей (Flash, Thinking, Pro, Lite) | ✅ |
| Web search (встроенный Gemini Search) | ✅ |
| Режим консультанта (`@think=N`) | ✅ |
| Google Native API (`/v1beta/models`) | ✅ |
| Codex Responses API (`/v1/responses`) | ✅ |
| Детерминированный planner для tool calls | ✅ **новое** |
| Авто-retry при ошибках инструментов | ✅ **новое** |
| Compact tool definitions | ✅ **новое** |

## Модели

| Модель | Описание | Context |
|--------|----------|---------|
| `gemini-3.5-flash` | Быстрая, общего назначения | ~1M |
| `gemini-3.5-flash-thinking` | Глубокое рассуждение, длинный вывод | ~1M |
| `gemini-3.5-flash-thinking-lite` | Адаптивная глубина | ~1M |
| `gemini-3.1-pro` | Pro (с cookie — реальный Pro) | ~1M |
| `gemini-auto` | Автовыбор | ~1M |
| `gemini-flash-lite` | Лёгкая, быстрая | ~1M |

## Быстрый старт

### Вариант 1: Скачать и запустить (Linux / macOS / WSL)

```bash
curl -fsSL https://raw.githubusercontent.com/mcfax-arch/gemini-for-agents/main/install.sh | bash
```

Скрипт:
1. Скачивает последнюю версию
2. Устанавливает в `~/.gemini-for-agents/`
3. Создаёт systemd-сервис (Linux) или launchd (macOS)
4. Запускает сервер на `http://localhost:8081`

### Вариант 2: Windows (PowerShell)

```powershell
powershell -c "irm https://raw.githubusercontent.com/mcfax-arch/gemini-for-agents/main/install.ps1 | iex"
```

Скрипт:
1. Скачивает последнюю версию
2. Устанавливает в `$env:USERPROFILE\Documents\gemini-for-agents\`
3. Создаёт задачу в Task Scheduler (автозапуск при входе)
4. Запускает сервер скрытно (без окна)

### Вариант 3: Вручную (любая платформа)

```bash
git clone https://github.com/mcfax-arch/gemini-for-agents.git
cd gemini-for-agents

# Просто запустить
python gemini_web2api.py
```

Сервер стартует на `http://localhost:8081/v1`.

### Вариант 4: Docker

```bash
docker run -d --name gemini-for-agents -p 8081:8081 mcfax/gemini-for-agents
```

---

## Использование с AI-агентами

### Hermes Agent

Добавить в `config.yaml`:

```yaml
providers:
  gemini-web2api:
    name: Gemini for Agents
    base_url: http://127.0.0.1:8081/v1
    api_key: dummy
    api_mode: chat_completions
    discover_models: true
    models:
      gemini-3.5-flash:
        context_length: 1048576
      gemini-3.5-flash-thinking:
        context_length: 1048576
      gemini-3.1-pro:
        context_length: 1048576
```

Использовать в fallback chain:

```yaml
fallback_providers:
- provider: custom:gemini-web2api-(local)
  model: gemini-3.5-flash-thinking
  base_url: http://127.0.0.1:8081/v1
  api_mode: chat_completions
```

### Любой OpenAI-клиент

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8081/v1",
    api_key="anything"  # не проверяется, если api_keys пустой
)

resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "Hello!"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"]
            }
        }
    }]
)
print(resp.choices[0].message.content)
```

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

---

## Конфигурация

Создать `config.json` в директории сервера:

```json
{
  "port": 8081,
  "host": "127.0.0.1",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "default_model": "gemini-3.5-flash",
  "api_keys": [],
  "cookie_file": null,
  "proxy": null,
  "log_requests": true
}
```

- `api_keys: []` — аутентификация отключена
- `api_keys: ["sk-key1"]` — требуется `Authorization: Bearer sk-key1`
- `proxy: "http://127.0.0.1:7890"` — HTTP-прокси для Gemini
- `cookie_file: "/path/to/cookie.txt"` — cookie для Pro-режима

## Cookie для Pro

Для `gemini-3.1-pro` в полную силу нужны cookie из браузера (любой Google-аккаунт):

1. Открыть `gemini.google.com`, войти
2. DevTools → Application → Cookies → `https://gemini.google.com`
3. Скопировать: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. Сохранить в файле:

```
SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx
```

Запустить: `python gemini_web2api.py --cookie-file cookie.txt`

## Автоматизация

### Windows (Task Scheduler)

```powershell
.\install.ps1
```

Создаёт задачу `Hermes Gemini Web2API` — запускает сервер при каждом входе в систему, скрытно, без окна.

### Linux (systemd)

```bash
./install.sh
```

Создаёт systemd-юнит `gemini-for-agents` — автозапуск при загрузке.

---

## Ограничения

- **Нет изображений** — Gemini требует проприетарный RPC-протокол для загрузки
- **Не настоящий Pro** — без cookie `gemini-3.1-pro` работает как Flash
- **Rate limits** — Google может throttl'ить частые запросы
- Только Python 3.8+, без внешних зависимостей (stdlib)

## Как это работает

Сервер перехватывает OpenAI-формат запросов и пересылает их в Gemini StreamGenerate API — тот же эндпоинт, что использует веб-интерфейс `gemini.google.com`. Выбор модели контролируется полем `[79]` в protobuf-like payload.

## Лицензия

MIT
