#!/usr/bin/env python3
"""
gemini-web2api - Gemini Web to OpenAI API proxy.

Converts Google Gemini's web interface into an OpenAI-compatible API server.
Zero authentication required. Works on any platform (Windows/macOS/Linux).

Usage:
    pip install httpx
    python gemini_web2api.py [--port 8081] [--config config.json]

Client configuration (Cherry Studio, ChatBox, etc.):
    Base URL: http://localhost:8081/v1
    API Key: (anything or empty)

How it works:
    Sends requests directly to Gemini's public StreamGenerate endpoint.
    The backend does not verify authentication for basic text generation.
    Model selection via MODE_CATEGORY field [79] in the request payload.
    This is NOT a user-tier spoofing attack - the endpoint simply doesn't
    require auth for anonymous access.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import hashlib
import argparse
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "1.1.0"

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    # Tool calling is prompt-emulated because Gemini Web has no public native
    # function-calling protocol. Keep this strict and compact for agent use.
    "tool_retry_attempts": 1,
    "empty_tool_result_retry_attempts": 1,
    "tool_result_recovery_max_chars": 24000,
    "tool_description_max_chars": 220,
    "tool_property_description_max_chars": 120,
}

CONFIG = dict(DEFAULT_CONFIG)

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Fast general-purpose model",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

# ─── Utilities ───────────────────────────────────────────────────────────────

def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def load_cookie() -> tuple:
    """Load cookie from file. Returns (cookie_str, sapisid)."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    body = urllib.parse.urlencode({"f.req": json.dumps(outer)}).encode()
    reqid = int(time.time()) % 1000000
    url = (
        "https://gemini.google.com/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int):
    """Send prompt and yield incremental text deltas using httpx streaming."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    body = urllib.parse.urlencode({"f.req": json.dumps(outer)})
    reqid = int(time.time()) % 1000000
    url = (
        "https://gemini.google.com/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    proxy = CONFIG.get("proxy")

    if not HAS_HTTPX:
        # Fallback: non-streaming with urllib
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    prev_text = ""
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
        with client.stream("POST", url, content=body, headers=headers) as resp:
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"wrb.fr"' not in line or len(line) < 200:
                        continue
                    try:
                        arr = json.loads(line)
                        inner_str = arr[0][2]
                        if not inner_str or len(inner_str) < 50:
                            continue
                        inner2 = json.loads(inner_str)
                        if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = t[len(prev_text):]
                                            delta = clean_gemini_text(delta)
                                            if delta:
                                                yield delta
                                            prev_text = t
                    except (json.JSONDecodeError, IndexError, TypeError):
                        pass


def clean_gemini_text(text: str) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip()


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response to extract final text."""
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

def _truncate_text(value, max_chars: int) -> str:
    """Single-line, bounded text for tool declarations."""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def _tool_function(tool: dict) -> dict:
    return tool.get("function", tool) if isinstance(tool, dict) and tool.get("type") == "function" else (tool or {})


def _compact_parameters(parameters: dict) -> dict:
    """Reduce OpenAI JSON schema to the parts Gemini needs to call tools.

    Real Hermes tool schemas are long and description-heavy. Gemini Web often
    treats those descriptions as normal chat context and replies with prose
    instead of a tool call. This compact form keeps names/types/enums/defaults
    and short descriptions, while removing verbosity.
    """
    if not isinstance(parameters, dict):
        return {"type": "object", "properties": {}}
    required = parameters.get("required") or []
    props = parameters.get("properties") or {}
    compact_props = {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            compact_props[name] = {"type": "string"}
            continue
        item = {"type": spec.get("type", "string")}
        if "enum" in spec:
            item["enum"] = spec["enum"]
        if "default" in spec:
            item["default"] = spec["default"]
        desc = _truncate_text(spec.get("description", ""), CONFIG.get("tool_property_description_max_chars", 120))
        if desc:
            item["description"] = desc
        if name in required:
            item["required"] = True
        compact_props[name] = item
    return {"type": parameters.get("type", "object"), "required": required, "properties": compact_props}


def compact_tool_defs(tools: list) -> list:
    compact = []
    for tool in tools or []:
        fn = _tool_function(tool)
        name = fn.get("name") or tool.get("name", "") if isinstance(tool, dict) else ""
        if not name:
            continue
        desc = _truncate_text(fn.get("description", ""), CONFIG.get("tool_description_max_chars", 220))
        compact.append({
            "name": name,
            "description": desc,
            "parameters": _compact_parameters(fn.get("parameters", {})),
        })
    return compact


def _tool_calls_to_block(tool_calls: list) -> str:
    calls = []
    for tc in tool_calls or []:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw_arguments": args}
        calls.append({"name": fn.get("name", ""), "arguments": args or {}})
    return "```tool_calls\n" + json.dumps(calls, ensure_ascii=False, indent=2) + "\n```"


def _content_to_text(content) -> str:
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") in ("text", "input_text")
        )
    if content is None:
        return ""
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def messages_to_prompt(messages: list, tools: list = None) -> str:
    """Convert OpenAI messages to a Gemini-Web-friendly prompt string."""
    parts = []
    tool_defs = compact_tool_defs(tools)
    if tool_defs:
        parts.append(
            "[TOOL CONTRACT — STRICT, MACHINE READABLE]\n"
            "You are connected to an automated tool-execution loop. The tools listed below are AVAILABLE NOW.\n"
            "Do NOT say a tool is unavailable, failed, inaccessible, or not configured unless a [TOOL RESULT] explicitly says so.\n"
            "Do NOT browse the web or guess repository/file contents when a local/file/tool answer is needed.\n\n"
            "When the user asks to inspect/read/list/search/edit/run/check files, current system state, or any information you do not already know, you MUST call tools.\n"
            "To call tools, your ENTIRE assistant reply must be exactly ONE markdown block named tool_calls containing a JSON array:\n\n"
            "```tool_calls\n"
            "[\n"
            "  {\"name\": \"<exact tool name>\", \"arguments\": { ... }}\n"
            "]\n"
            "```\n\n"
            "GOOD example:\n"
            "```tool_calls\n"
            "[{\"name\": \"search_files\", \"arguments\": {\"target\": \"files\", \"path\": \"C:/Users/mcFax\", \"pattern\": \"*\", \"limit\": 5}}]\n"
            "```\n\n"
            "BAD examples (do not do these):\n"
            "- Plain text like: 'I cannot access local files' before trying a tool.\n"
            "- Shell commands in prose or ```bash fences.\n"
            "- Tool names not present in Available tools.\n"
            "- JSON object without the surrounding array.\n\n"
            "Tool selection guide:\n"
            "- To list files/directories: use search_files with {\"target\":\"files\", \"path\":..., \"pattern\":\"*\", \"limit\":...}.\n"
            "- To read contents of a known file: use read_file with {\"path\":...}.\n"
            "- To write or create a file: use write_file with {\"path\":..., \"content\":...}.\n"
            "- To edit small portions of a file (replace some text): use patch with {\"path\":..., \"old_string\":..., \"new_string\":...}.\n"
            "- To run any shell command (ls, dir, echo, wget, etc.): use terminal with {\"command\":...}.\n"
            "Rules:\n"
            "1. Tool name must match Available tools exactly.\n"
            "2. arguments must be a valid JSON object. Use {} if no arguments are needed.\n"
            "3. If a previous [TOOL RESULT] is present and enough to answer, then answer normally without a tool_calls block.\n"
            "4. Otherwise, call the next needed tool.\n\n"
            "Available tools (compact schema):\n"
            f"{json.dumps(tool_defs, ensure_ascii=False, indent=2)}\n"
            "[END TOOL CONTRACT]"
        )

    for msg in messages:
        role = msg.get("role", "user")
        content = _content_to_text(msg.get("content", ""))
        if role == "system":
            parts.append(f"[SYSTEM]:\n{content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                prefix = f"[ASSISTANT]:\n{content}\n" if content else "[ASSISTANT]:\n"
                parts.append(prefix + _tool_calls_to_block(msg.get("tool_calls", [])))
            else:
                parts.append(f"[ASSISTANT]:\n{content}")
        elif role == "tool":
            name = msg.get("name", "")
            call_id = msg.get("tool_call_id", "")
            parts.append(f"[TOOL RESULT name={name} id={call_id}]:\n{content}")
        else:
            parts.append(f"[USER]:\n{content}" if role != "user" else content)

    if tool_defs:
        if _messages_require_tool(messages) and not _messages_have_tool_result(messages):
            parts.append(
                "[TOOL REQUIRED FOR THIS TURN — FINAL DIRECTIVE]\n"
                "The latest user request explicitly requires tool use or local/current inspection.\n"
                "Your next assistant message MUST be only a ```tool_calls JSON array block.\n"
                "For listing files, prefer: search_files with target='files', pattern='*', the requested path, and the requested limit.\n"
                "For reading known files, prefer: read_file with the requested path.\n"
                "For creating/writing files, prefer: write_file with path and content.\n"
                "For editing/replacing text in a file, prefer: patch with path, old_string, new_string.\n"
                "For running shell commands, prefer: terminal with the exact command string.\n"
                "Do not output prose. Do not say you used a tool. Actually emit the tool call block now."
            )
        parts.append(
            "[SYSTEM REMINDER]: If this request needs a tool and no sufficient [TOOL RESULT] is already present, "
            "reply ONLY with a ```tool_calls JSON array block. Do not apologize. Do not claim tools are unavailable."
        )
    return "\n\n---\n\n".join(p for p in parts if p)


def _json_loads_loose(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = text
        # Common truncation/format repairs from web LLMs.
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        if fixed.startswith("{") and not fixed.endswith("}"):
            fixed += "}"
        if fixed.startswith("[") and not fixed.endswith("]"):
            fixed += "]"
        return json.loads(fixed)


def _extract_json_payloads(text: str) -> list:
    payloads = []
    # Preferred plural block, plus backwards-compatible singular/json blocks.
    fence_re = r"```(?:tool_calls|tool_call|json)\s*\n(.*?)\n```"
    for match in re.findall(fence_re, text, re.DOTALL | re.IGNORECASE):
        payloads.append(match.strip())
    if payloads:
        return payloads

    # Last resort: find a JSON array/object containing name+arguments in prose.
    idxs = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not idxs:
        return []
    start = min(idxs)
    end = max(text.rfind("]"), text.rfind("}"))
    if end > start:
        payloads.append(text[start:end + 1].strip())
    return payloads


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_calls blocks. Returns (clean_text, OpenAI tool_calls_list)."""
    tool_calls = []
    for payload in _extract_json_payloads(text or ""):
        try:
            data = _json_loads_loose(payload)
        except Exception:
            continue
        if isinstance(data, dict):
            # Backwards compatible: {"name":..., "arguments":...}
            data = [data]
        if not isinstance(data, list):
            continue
        for call in data:
            if not isinstance(call, dict) or not call.get("name"):
                continue
            args = call.get("arguments", {})
            if args is None:
                args = {}
            if isinstance(args, str):
                # OpenAI wants a JSON string; keep valid JSON strings as-is,
                # otherwise wrap raw text so Hermes can surface a clear error.
                try:
                    json.loads(args) if args.strip() else {}
                    args_str = args if args.strip() else "{}"
                except json.JSONDecodeError:
                    args_str = json.dumps({"_raw_arguments": args}, ensure_ascii=False)
            else:
                args_str = json.dumps(args, ensure_ascii=False)
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": call["name"], "arguments": args_str},
            })
    clean = re.sub(r"```(?:tool_calls|tool_call|json)\s*\n.*?\n```", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    return clean, tool_calls


def _prompt_has_tool_result(prompt: str) -> bool:
    # Actual tool results are serialized as: [TOOL RESULT name=... id=...]
    # The instruction text also mentions "[TOOL RESULT]" as a concept, so do
    # not treat that as an executed tool result.
    return "[TOOL RESULT name=" in (prompt or "")


def build_tool_result_recovery_prompt(prompt: str) -> str:
    """Build a smaller prompt when Gemini returns empty after tool results.

    Gemini Web occasionally returns HTTP 200 with no parseable text when the
    full Hermes prompt contains a large tool contract + long history + tool
    results. The information needed for the final answer is normally in the
    latest tool-result tail, so retry with a compact continuation prompt.
    """
    text = prompt or ""
    marker = "[TOOL RESULT name="
    idx = text.rfind(marker)
    tail_start = max(0, idx - 6000) if idx >= 0 else max(0, len(text) - 12000)
    tail = text[tail_start:]
    max_chars = int(CONFIG.get("tool_result_recovery_max_chars", 24000) or 24000)
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return (
        "You are continuing a Hermes Agent task on Windows 11.\n"
        "The previous response was empty after tool execution.\n"
        "Use the tool results below to answer the user's request directly.\n"
        "Do not call more tools unless absolutely necessary.\n\n"
        "[RECENT CONVERSATION AND TOOL RESULTS]\n"
        f"{tail}\n"
        "[END]\n\n"
        "Now provide the final answer in Russian if the user wrote Russian."
    )


def _messages_have_tool_result(messages: list) -> bool:
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in messages or [])


def _latest_user_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role", "user") == "user":
            return _content_to_text(msg.get("content", ""))
    return ""


def _messages_require_tool(messages: list) -> bool:
    t = _latest_user_text(messages).lower()
    patterns = [
        "use the tool", "must call", "do not answer directly", "do not answer from memory",
        "используй инструмент", "использовать инструмент", "не отвечай из памяти",
        "покажи", "прочитай", "найди", "список файлов", "файлы", "файлов", "директори",
        "list files", "read file", "search files", "show files", "directory",
        "c:/", "/c/users", ".py", ".md", ".json",
    ]
    return any(p in t for p in patterns)


def _looks_like_missed_tool_call(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "cannot access", "can't access", "unable to access", "не могу получить доступ",
        "tool", "инструмент", "воспользовался инструмент", "использовал инструмент",
        "unavailable", "not available", "not configured",
        "i would", "я бы", "based on", "looks like", "похоже",
        "local files", "локаль", "filesystem", "файлов",
        "as requested", "как требовалось", "как вы просили",
        # Additional patterns for write/edit/exec hallucination
        "created file", "wrote to file", "edited the file", "i have created", "i have written",
        "создал файл", "записал в файл", "отредактировал", "изменил",
        "ran command", "executed command", "выполнил команду", "запустил",
        "i will create", "i will write", "i will run", "i will edit",
        "let me create", "let me write", "let me run",
    ]
    return any(p in t for p in patterns)


def _looks_like_tool_required_prompt(prompt: str) -> bool:
    t = (prompt or "").lower()
    patterns = [
        # File/directory operations
        "use the tool", "must call", "do not answer directly", "do not answer from memory",
        "используй инструмент", "использовать инструмент", "не отвечай из памяти",
        "покажи", "прочитай", "найди", "список файлов", "файлов", "директори",
        "list files", "read file", "search files", "show files", "directory",
        "c:/", "/c/users", ".py", ".md", ".json",
        # Write/edit operations
        "write file", "create file", "write to file", "save to file",
        "запиши в файл", "создай файл", "запиши файл",
        "edit file", "edit the file", "replace in file", "change file",
        "редактируй", "измени файл", "замени текст", "исправь",
        "patch", "old_string", "new_string",
        # Terminal/exec operations
        "run command", "execute", "run the following", "run this",
        "выполни команду", "запусти", "выполни",
        "команду", "terminal", "shell",
        # Web/search operations
        "search the web", "search online", "search internet", "look up",
        "поищи в интернете", "найди в интернете", "поищи информацию",
        "web search", "web_extract", "extract from",
        # Generic "goal-oriented" agent tasks
        "your task", "you need to", "you must", "first tool",
    ]
    return any(p in t for p in patterns)


def build_tool_retry_prompt(prompt: str, bad_text: str) -> str:
    return (
        f"{prompt}\n\n---\n"
        "[TOOL FORMAT ERROR — RETRY REQUIRED]\n"
        "Your previous response was rejected because tools were available but you answered with prose instead of a tool call.\n"
        "Rejected response preview:\n"
        f"{_truncate_text(bad_text, 700)}\n\n"
        "If the task needs local files, system state, current data, inspection, editing, or command execution, "
        "reply now ONLY with this exact format and no other text:\n"
        "```tool_calls\n"
        "[{\"name\": \"<exact available tool name>\", \"arguments\": {}}]\n"
        "```\n"
        "Never say tools are unavailable unless a [TOOL RESULT] explicitly says that."
    )



def _available_tool_names(tools: list) -> set:
    names = set()
    for tool in tools or []:
        fn = _tool_function(tool)
        if fn.get("name"):
            names.add(fn["name"])
    return names


def _extract_path(text: str) -> str:
    """Extract a filesystem path from text — platform-aware.

    Returns the last-matching path from the prompt to prefer the user's real request
    over instruction examples that appear earlier.

    Windows (os.name == 'nt'):
      - C:\\Users\\..., C:/Users/...
      - /c/Users/... (git-bash / MSYS / WSL interop)

    Linux / macOS (os.name == 'posix'):
      - /home/..., /Users/..., /tmp/..., /var/..., /etc/..., /opt/...
      - Any absolute path with 2+ levels: /foo/bar/...
    """
    t = text or ""

    if os.name == 'nt':
        # Windows paths. Allow both slash and backslash as path separators after
        # the drive prefix; stop only at whitespace/quotes/common punctuation.
        matches = re.findall(r"([A-Za-z]:[\\/][^\s`'\"\],)]+)", t)
        if matches:
            return matches[-1].rstrip(".,;:")
        # git-bash / MSYS / WSL interop paths (/c/Users/...)
        matches = re.findall(r"(/[a-zA-Z]/[^\s`'\"\\\],)]+)", t)
        if matches:
            return matches[-1].rstrip(".,;:")
    else:

        # Match absolute paths with at least 2 path components
        matches = re.findall(
            r"((?:/home|/Users|/tmp|/var|/etc|/opt|/usr|/bin|/sbin|/lib|/mnt|/media|/run|/srv)"
            r"(?:/[^\s`'\"\\\],)]+)+)",
            t, re.IGNORECASE,
        )
        if matches:
            return matches[-1].rstrip(".,;:")
        # Fallback: any absolute path with 3+ components (e.g. /foo/bar/baz)
        matches = re.findall(r"(/[^\s`'\"\\\],)]+/[^\s`'\"\\\],)]+/[^\s`'\"\\\],)]+)", t)
        if matches:
            return matches[-1].rstrip(".,;:")

    return ""


def _extract_requested_limit(text: str, default: int = 10) -> int:
    t = text or ""
    m = re.search(r"(?:first|первые|первых|top)\s+(\d{1,3})", t, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{1,3})\s+(?:files|файл)", t, re.IGNORECASE)
    if m:
        return max(1, min(int(m.group(1)), 100))
    return default


def _make_tool_call(name: str, arguments: dict) -> list:
    return [{
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }]


def _find_tool_schema(name: str, tools: list) -> dict | None:
    """Find the JSON schema for a tool by name."""
    for tool in tools or []:
        fn = _tool_function(tool)
        if fn.get("name") == name:
            return fn["parameters"] if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
    return None


def _validate_tool_calls(tool_calls: list, tools: list) -> tuple:
    """Validate tool call arguments against their schemas.

    Returns (valid_calls, errors) where errors is a list of descriptive messages
    for calls with invalid or missing required arguments.
    """
    if not tool_calls or not tools:
        return tool_calls or [], []

    valid = []
    errors = []

    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name", "")
        if not name:
            valid.append(tc)
            continue

        args_raw = fn.get("arguments", {})
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw) if args_raw.strip() else {}
            except json.JSONDecodeError:
                errors.append(f"Tool '{name}': arguments is not valid JSON")
                continue
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            errors.append(f"Tool '{name}': invalid arguments type")
            continue

        schema = _find_tool_schema(name, tools)
        if not schema:
            valid.append(tc)
            continue

        props = schema.get("properties", {})
        required = schema.get("required", [])

        # Check required args
        missing = [r for r in required if r not in args or args[r] is None or (isinstance(args[r], str) and not args[r].strip())]
        if missing:
            # Try to fix: add sensible defaults where possible
            fixed = False
            for m in missing:
                prop_schema = props.get(m, {})
                default_val = prop_schema.get("default")
                if default_val is not None:
                    args[m] = default_val
                    fixed = True
                elif prop_schema.get("type") in ("string", "") and "default" not in prop_schema:
                    args[m] = ""
                    fixed = True

            if not fixed:
                errors.append(f"Tool '{name}': missing required arguments: {', '.join(missing)}")
                continue

        # Type check basic types
        for arg_name, arg_val in list(args.items()):
            prop_schema = props.get(arg_name, {})
            expected_type = prop_schema.get("type", "")
            if expected_type == "integer" and isinstance(arg_val, (int, float)) and not isinstance(arg_val, bool):
                if isinstance(arg_val, float):
                    args[arg_name] = int(arg_val)
            elif expected_type == "number" and isinstance(arg_val, (int, float)) and not isinstance(arg_val, bool):
                pass
            elif expected_type == "string" and isinstance(arg_val, (int, float)):
                args[arg_name] = str(arg_val)
            elif expected_type == "array" and isinstance(arg_val, list):
                pass
            elif expected_type == "boolean" and isinstance(arg_val, (bool, int)):
                if isinstance(arg_val, int):
                    args[arg_name] = bool(arg_val)

            # Check enums
            enum_vals = prop_schema.get("enum", [])
            if enum_vals and arg_val not in enum_vals:
                if len(enum_vals) > 0:
                    args[arg_name] = enum_vals[0]  # use first valid value

        # Rebuild the tool call with fixed arguments
        tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
        valid.append(tc)

    return valid, errors


def _extract_user_request(prompt: str) -> str:
    """Extract the last user request from the full prompt (strips instruction boilerplate)."""
    # Split at section markers to isolate user text
    sections = re.split(r'\n\n---\n\n', prompt or "")
    for section in reversed(sections):
        t = section.strip()
        # Skip sections that are instruction blocks (start with [TOOL, [SYSTEM)
        if t.startswith("[") and ("CONTRACT" in t or "REMINDER" in t or "REQUIRED" in t):
            continue
        # Skip [TOOL RESULT] sections
        if t.startswith("[TOOL RESULT"):
            continue
        # Skip [ASSISTANT]: sections
        if t.startswith("[ASSISTANT]") or t.startswith("[SYSTEM]"):
            continue
        # Found the user message — strip any [USER]: prefix
        if t.startswith("[USER]:"):
            t = t[len("[USER]:"):].strip()
        if t:
            return t
    return prompt[-600:] if prompt else ""


def _extract_shell_command(text: str) -> str:
    """Extract a shell command from user request text."""
    # Search only the user message, not the full prompt
    low = (text or "").strip()
    # "run <command>", "run the following: <command>"
    for prefix in ["run the command: ", "run the following: ", "run the following command: ",
                    "run command: ", "run: ", "run ",
                    "execute: ", "execute ",
                    "выполни команду: ", "выполни команду ", "выполни: ", "выполни ",
                    "запусти команду: ", "запусти команду ", "запусти: ", "запусти "]:
        idx = low.lower().find(prefix)
        if idx != -1:
            cmd = low[idx + len(prefix):].strip()
            # Stop at end of sentence or end of string
            cmd = re.split(r'[.!;\n]', cmd)[0].strip()
            if (cmd.startswith("'") and cmd.endswith("'")) or (cmd.startswith('"') and cmd.endswith('"')):
                cmd = cmd[1:-1]
            if cmd and len(cmd) < 500:
                return cmd
    # Fallback: pick the last line that looks like a command
    for line in reversed(low.split('\n')):
        line = line.strip().strip('.,;:')
        if line and not any(x in line.lower() for x in ["use the", "используй", "tool", "инструмент"]):
            # Check it doesn't look like instruction text
            if not line.startswith("[") and len(line.split()) <= 20:
                return line
    return ""


def _extract_content_for_file(text: str) -> str:
    """Extract file content from user request — best-effort heuristic."""
    # Try to find content after markers like "with content:", 'with the text:', etc.
    patterns = [
        r'content:\s*["\'](.*?)["\']\s*(?:$|path|file)',
        r'text:\s*["\'](.*?)["\']\s*(?:$|path|file)',
        r'содержимым:\s*["\'](.*?)["\']',
        r'содержимым\s+["\'](.*?)["\']',
        r'содержимым\s+"(.*?)"',
        r'say:\s*["\'](.*?)["\']',
        r'напиши\s+(?:в\s+)?["\'](.*?)["\']',
        r'запиши\s+(?:в\s+)?["\'](.*?)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, text or "", re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _extract_old_new_strings(text: str) -> tuple:
    """Extract old_string and new_string for patch tool."""
    low = text or ""
    # "replace X with Y", "change X to Y", "замени X на Y"
    m = re.search(r'replace\s+["\'](.*?)["\']\s+with\s+["\'](.*?)["\']', low, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'change\s+["\'](.*?)["\']\s+to\s+["\'](.*?)["\']', low, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'замени\s+["\'](.*?)["\']\s+на\s+["\'](.*?)["\']', low, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    # Try old_string=... new_string=... style
    m = re.search(r'old_string[=:]\s*["\'](.*?)["\']', low, re.DOTALL | re.IGNORECASE)
    if m:
        old_s = m.group(1)
        m2 = re.search(r'new_string[=:]\s*["\'](.*?)["\']', low, re.DOTALL | re.IGNORECASE)
        if m2:
            return old_s, m2.group(1)
    return "", ""


def synthesize_obvious_tool_call(prompt: str, tools: list) -> list:
    """Last-resort planner for obvious agent tasks when Gemini Web ignores the
    tool protocol. This does not answer the user; it only returns a real
    OpenAI-style tool call so the caller can execute the actual tool.
    """
    if _prompt_has_tool_result(prompt) or not _looks_like_tool_required_prompt(prompt):
        return []
    names = _available_tool_names(tools)
    text = prompt or ""
    low = text.lower()

    # Determine if this is a multi-turn (has assistant+tool history) or first turn

    # Isolate the "user portion" of the prompt (after [END TOOL CONTRACT]).
    user_portion = text.split("[END TOOL CONTRACT]", 1)[-1] if "[END TOOL CONTRACT]" in text else text
    is_multi_turn = "[TOOL RESULT name=" in text or ("```tool_calls" in user_portion and not "```tool_calls" in text.split("[END TOOL CONTRACT]")[0] if "[END TOOL CONTRACT]" in text else "```tool_calls" in text)

    # For all analysis, use only the user's actual request text, not the full prompt
    user_text = _extract_user_request(user_portion) if not is_multi_turn else text
    user_low = user_text.lower()
    path = _extract_path(user_text)

    # 1. READ_FILE — read file contents (most specific: explicit "read/прочитай")
    if path and "read_file" in names and any(k in user_low for k in ["read", "cat", "прочитай", "содержимое", "открой"]):
        return _make_tool_call("read_file", {"path": path})

    # 2. WRITE_FILE — create/write a file
    content = _extract_content_for_file(text)
    if path and "write_file" in names and any(k in user_low for k in [
        "write file", "create file", "write to file", "save to file",
        "запиши в файл", "создай файл", "запиши файл"
    ]):
        kwargs = {"path": path}
        if content:
            kwargs["content"] = content
        return _make_tool_call("write_file", kwargs)

    # 3. PATCH — edit a file (replace text)
    if path and "patch" in names and any(k in user_low for k in [
        "replace", "change", "edit file", "patch", "редактируй", "измени", "замени"
    ]):
        old_s, new_s = _extract_old_new_strings(text)
        if old_s:
            return _make_tool_call("patch", {
                "path": path,
                "old_string": old_s,
                "new_string": new_s or "",
            })

    # 4. SEARCH_FILES — list/search files/directories (generic catch-all for files)
    if path and "search_files" in names and any(k in user_low for k in [
        "list", "show", "first", "files", "directory", "покажи", "первые", "файлы", "файлов", "директори", "список"
    ]):
        return _make_tool_call("search_files", {
            "pattern": "*",
            "target": "files",
            "path": path,
            "limit": _extract_requested_limit(text, 10),
        })

    # 5. TERMINAL — run a shell command
    user_cmd = _extract_shell_command(user_text)
    if "terminal" in names and any(k in user_low for k in [
        "run", "execute", "terminal", "command",
        "выполни", "запусти", "команду", "выполнить команду"
    ]):
        if user_cmd and len(user_cmd) > 2:
            return _make_tool_call("terminal", {"command": user_cmd})
        if any(k in user_low for k in ["ls", "dir", "echo", "pwd", "cd", "whoami"]):
            return _make_tool_call("terminal", {"command": next(k for k in ["ls", "dir", "echo", "pwd", "cd", "whoami"] if k in user_low)})

    # 6. WEB_SEARCH / WEB_EXTRACT — search the web
    if any(n in names for n in {"web_search", "web_extract"}) and any(k in user_low for k in [
        "search the web", "search online", "search internet", "look up",
        "найди в интернете", "поищи в интернете", "поищи информацию", "lookup",
    ]):
        query = user_text.strip()
        for prefix in ["search the web for ", "search online for ", "search internet for ", "look up ",
                        "найди в интернете ", "поищи в интернете ", "поищи информацию "]:
            idx = query.lower().find(prefix.lower())
            if idx != -1:
                query = query[idx + len(prefix):].strip(" .!,;:")
        if len(query) > 10 and "web_search" in names:
            return _make_tool_call("web_search", {"query": query[:300]})

    return []


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(fmt % args)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self._handle_google_models_list()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if self.path == "/v1/chat/completions":
                self.handle_chat(body)
            elif self.path == "/v1/responses":
                self.handle_responses(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            return None, None, None, f"Unknown model: {model_name}"
        return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

    def _call_gemini(self, prompt, model_id, think_mode, tools):
        # If the latest turn obviously requires a local/file tool, do not ask
        # Gemini Web to "decide" first — it often fabricates a prose answer.
        # Return a real OpenAI-style tool call so the client executes the tool.
        if tools and not _prompt_has_tool_result(prompt):
            synthetic_calls = synthesize_obvious_tool_call(prompt, tools)
            if synthetic_calls:
                valid_calls, errors = _validate_tool_calls(synthetic_calls, tools)
                if errors:
                    log(f"[VALIDATE] Planner: {'; '.join(errors)}")
                if valid_calls:
                    return "", valid_calls

        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        tool_calls = None

        if not text.strip() and tools and _prompt_has_tool_result(prompt):
            attempts = int(CONFIG.get("empty_tool_result_retry_attempts", 1) or 0)
            for attempt in range(attempts):
                log(
                    "[RECOVER] Empty response after tool results; "
                    f"retrying with compact prompt ({attempt + 1}/{attempts}, "
                    f"raw={len(raw)} bytes, prompt={len(prompt)} chars)"
                )
                recovery_prompt = build_tool_result_recovery_prompt(prompt)
                raw_retry = gemini_stream_generate(recovery_prompt, model_id, think_mode)
                retry_text = extract_response_text(raw_retry)
                if retry_text.strip():
                    text = retry_text
                    break

        if tools and text:
            text, tool_calls = parse_tool_calls(text)

            # Validate parsed tool calls against schema
            if tool_calls:
                valid_calls, errors = _validate_tool_calls(tool_calls, tools)
                if errors:
                    log(f"[VALIDATE] Gemini: {'; '.join(errors)}")
                if not valid_calls and not text.strip():
                    # All calls were invalid — force retry
                    tool_calls = None
                    text = "[VALIDATION ERROR] All tool calls rejected by schema check."
                else:
                    tool_calls = valid_calls

            # Gemini Web tool use is prompt-emulated, not native function calling.
            # With real agent schemas it sometimes answers with prose like
            # "I can't access local files" instead of emitting a tool_calls block.
            # If this is the first tool round (no tool result yet), give it one
            # strict repair attempt before returning plain text to the client.
            attempts = int(CONFIG.get("tool_retry_attempts", 1) or 0)
            if attempts > 0 and not tool_calls and not _prompt_has_tool_result(prompt) and (_looks_like_missed_tool_call(text) or _looks_like_tool_required_prompt(prompt)):
                retry_prompt = build_tool_retry_prompt(prompt, text)
                for _ in range(attempts):
                    raw_retry = gemini_stream_generate(retry_prompt, model_id, think_mode)
                    retry_text = extract_response_text(raw_retry)
                    retry_clean, retry_calls = parse_tool_calls(retry_text or "")
                    if retry_calls:
                        valid_calls, errors = _validate_tool_calls(retry_calls, tools)
                        if errors:
                            log(f"[VALIDATE] Retry: {'; '.join(errors)}")
                        if valid_calls:
                            return retry_clean or "", valid_calls
                    text = retry_clean or retry_text or text
                synthetic_calls = synthesize_obvious_tool_call(prompt, tools)
                if synthetic_calls:
                    valid_calls, errors = _validate_tool_calls(synthetic_calls, tools)
                    if errors:
                        log(f"[VALIDATE] Last-resort planner: {'; '.join(errors)}")
                    if valid_calls:
                        return "", valid_calls
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        prompt = messages_to_prompt(req.get("messages", []), tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream and not tools:
            # True streaming: forward chunks as they arrive
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                for delta_text in gemini_stream_generate_iter(prompt, model_id, think_mode):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                # Final chunk
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Non-streaming (or tool calling which needs full response)
        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            # Stream mode with tools: send as single chunk (need full parse for tool_calls)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                          "total_tokens": (len(prompt)+len(text))//4},
            })

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")

        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if c.get("type") in ("text", "input_text"))
                        messages.append({"role": role, "content": content})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        prompt = messages_to_prompt(messages, tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            ev = {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "model": model_name, "output": []}}
            self.wfile.write(f"event: response.created\ndata: {json.dumps(ev)}\n\n".encode())
            for item in output:
                if item["type"] == "function_call":
                    ev = {"type": "response.function_call_arguments.done", "item_id": item["id"], "call_id": item["call_id"], "name": item["name"], "arguments": item["arguments"]}
                    self.wfile.write(f"event: response.function_call_arguments.done\ndata: {json.dumps(ev)}\n\n".encode())
                elif item["type"] == "message":
                    for ci, cp in enumerate(item["content"]):
                        ev = {"type": "response.output_text.done", "item_id": item["id"], "content_index": ci, "text": cp["text"]}
                        self.wfile.write(f"event: response.output_text.done\ndata: {json.dumps(ev)}\n\n".encode())
            resp_obj = {"id": rid, "object": "response", "status": "completed", "model": model_name, "output": output,
                        "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}}
            self.wfile.write(f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': resp_obj})}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}})


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> str:
        """Convert Google API contents format to prompt string."""
        parts = []
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        prompt = self._google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            text, _ = self._call_gemini(prompt, model_id, think_mode, None)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        candidate = {
            "content": {"parts": [{"text": text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(response_obj)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
