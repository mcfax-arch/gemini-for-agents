# Gemini for Agents

<p align="center">
  <img src="logo.png" width="200" alt="gemini-for-agents logo">
</p>

Google Gemini → OpenAI-compatible API proxy, заточенный под AI-агентов.

Использует Gemini Web API напрямую — **без API-ключа**, бесплатно, с полной поддержкой tool calling.

Работает на **Windows, macOS, Linux** (и WSL).

---

## Чем отличается от оригинала

Форк [gemini-web2api](https://github.com/Sophomoresty/gemini-web2api) с фокусом на работу AI-агентов:

- **Детерминированный planner** — когда Gemini «забывает» вызвать инструмент, синтезатор анализирует запрос и вызывает нужный tool самостоятельно
- **Retry/repair tool calls** — если Gemini вернула сломанный JSON или пропустила tool call, сервер автоматически перестраивает запрос
- **Compact tool definitions** — схемы инструментов упакованы в 2–3× меньший размер без потери функциональности
- **Кроссплатформенный парсинг путей** — корректно извлекает пути Windows (C:\...), Linux (/home/...), macOS (/Users/...) и git-bash (/c/Users/...) в зависимости от ОС
- **Кроссплатформенный запуск** — один `launch.py` для всех ОС

---

## Быстрый старт

### Установка (одна команда)

```bash
# Linux / macOS / Windows (git-bash, WSL)
curl -fsSL https://raw.githubusercontent.com/mcfax-arch/gemini-for-agents/main/install.py | python3
```

Эта команда:
1. Клонирует репозиторий в `~/.gemini-for-agents/`
2. Определяет вашу ОС
3. Создаёт сервис автозапуска:
   - **Windows**: Task Scheduler (скрытый, при входе)
   - **macOS**: launchd user agent
   - **Linux**: systemd user service
4. Запускает сервер

После перезагрузки сервер стартует автоматически.

### Ручной запуск

```bash
git clone https://github.com/mcfax-arch/gemini-for-agents.git
cd gemini-for-agents

python launch.py              # запустить как демон
python launch.py --status      # проверить статус
python launch.py --stop        # остановить
python launch.py --restart     # перезапустить

# Или просто в foreground:
python gemini_web2api.py
```

Сервер стартует на `http://localhost:8081/v1`.

### Удаление

```bash
curl -fsSL https://raw.githubusercontent.com/mcfax-arch/gemini-for-agents/main/install.py | python3 - --uninstall
```

---

## Возможности

| Возможность | |
|-------------|---|
| OpenAI-совместимый API (`/v1/chat/completions`) | ✅ |
| Tool calling (функции) | ✅ **улучшено** |
| Streaming (SSE) | ✅ |
| 6 моделей (Flash, Thinking, Pro, Lite, Auto) | ✅ |
| Web search (встроенный Gemini Search) | ✅ |
| Режим консультанта (`@think=N`) | ✅ |
| Google Native API (`/v1beta/models`) | ✅ |
| Codex Responses API (`/v1/responses`) | ✅ |
| Детерминированный planner для tool calls | ✅ |
| Авто-retry при ошибках инструментов | ✅ |
| Compact tool definitions | ✅ |
| Windows / macOS / Linux | ✅ |
| Автозапуск (Task Scheduler / launchd / systemd) | ✅ |

---

## Модели

| Модель | Описание |
|--------|----------|
| `gemini-3.5-flash` | Быстрая, общего назначения |
| `gemini-3.5-flash-thinking` | Глубокое рассуждение, длинный вывод |
| `gemini-3.5-flash-thinking-lite` | Адаптивная глубина |
| `gemini-3.1-pro` | Pro (с cookie — реальный Pro) |
| `gemini-auto` | Автовыбор |
| `gemini-flash-lite` | Лёгкая, быстрая |

### Thinking Depth

Аппендикс `@think=N` к любой модели:

```
gemini-3.5-flash-thinking@think=0   # deepest (default)
gemini-3.5-flash-thinking@think=2   # medium
gemini-3.5-flash-thinking@think=4   # shallowest
```

---

## Использование

### Hermes Agent

```yaml
# config.yaml
providers:
  gemini-web2api:
    name: Gemini for Agents
    base_url: http://127.0.0.1:8081/v1
    api_key: dummy
    api_mode: chat_completions
    discover_models: true

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
    api_key="anything"
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

### Tool Calling

```python
resp = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"]
            }
        }
    }]
)
```

---

## Конфигурация

Создать `config.json` рядом с `gemini_web2api.py`:

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

| Поле | Описание |
|------|----------|
| `port` | Порт сервера (по умолчанию 8081) |
| `host` | Адрес (`127.0.0.1` — только localhost, `0.0.0.0` — все интерфейсы) |
| `api_keys` | Пустой массив — без аутентификации. Если заполнить — требуется `Authorization: Bearer *** `retry_attempts` | Количество повторов при ошибках Gemini |
| `cookie_file` | Путь к файлу с cookie для Pro-режима |
| `proxy` | HTTP-прокси (например, `http://127.0.0.1:7890`) |
| `log_requests` | Логировать ли запросы |

Прокси также подхватывается из переменных окружения `HTTPS_PROXY` / `HTTP_PROXY`.

---

## Cookie для Pro-режима

`gemini-3.1-pro` без cookie работает как Flash. Для реального Pro:

1. Откройте `gemini.google.com` в браузере, войдите в любой Google-аккаунт
2. DevTools → Application → Cookies → `https://gemini.google.com`
3. Скопируйте: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. Сохраните в файл:

```
SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx
```

Запуск: `python gemini_web2api.py --cookie-file cookie.txt`

---

## Docker

```bash
docker build -t gemini-for-agents .
docker run -d --name gemini-for-agents -p 8081:8081 \
  -v ./config.json:/app/config.json gemini-for-agents
```

Или через docker-compose:

```bash
docker compose up -d
```

---

## Ограничения

- **Нет изображений** — Gemini требует проприетарный RPC-протокол для загрузки
- **Не настоящий Pro** — без cookie `gemini-3.1-pro` работает как Flash
- **Rate limits** — Google может throttl'ить частые запросы
- Python 3.8+, без внешних зависимостей (stdlib)

---

## Как это работает

Сервер перехватывает OpenAI-формат запросов и пересылает их в Gemini StreamGenerate API — тот же эндпоинт, что использует веб-интерфейс `gemini.google.com`. Выбор модели контролируется полем `[79]` в protobuf-like payload.

## Лицензия

MIT
