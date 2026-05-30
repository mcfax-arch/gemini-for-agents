#!/usr/bin/env python3
"""Audit/test runner for gemini-for-agents.

Does not print secrets. Writes compact JSON with pass/fail and response excerpts.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("gfa_monolith", ROOT / "gemini_web2api.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

BASE = os.environ.get("GFA_BASE", "http://127.0.0.1:8081/v1")
MODEL = os.environ.get("GFA_MODEL", "gemini-3.5-flash")


def post_json(path, payload, timeout=240):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE.rsplit("/v1", 1)[0] + path,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "elapsed": round(time.time()-t0, 3), "json": json.loads(body)}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body[:1000]
        return {"ok": False, "status": e.code, "elapsed": round(time.time()-t0, 3), "json": parsed}
    except Exception as e:
        return {"ok": False, "status": None, "elapsed": round(time.time()-t0, 3), "error": repr(e)}


def get_json(path, timeout=30):
    req = urllib.request.Request(BASE.rsplit("/v1", 1)[0] + path, headers={"Authorization": "Bearer dummy"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "status": resp.status, "json": json.loads(resp.read().decode("utf-8", errors="replace"))}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def compact_response(resp):
    out = {"ok": resp.get("ok"), "status": resp.get("status"), "elapsed": resp.get("elapsed")}
    js = resp.get("json")
    if isinstance(js, dict):
        out["keys"] = list(js.keys())[:10]
        if "error" in js:
            out["error"] = js["error"]
        if "choices" in js and js["choices"]:
            ch = js["choices"][0]
            out["finish_reason"] = ch.get("finish_reason")
            msg = ch.get("message") or ch.get("delta") or {}
            out["message_content_preview"] = (msg.get("content") or "")[:500] if isinstance(msg, dict) else str(msg)[:500]
            if isinstance(msg, dict) and msg.get("tool_calls"):
                out["tool_calls"] = msg["tool_calls"]
        if "output" in js:
            out["output_preview"] = str(js["output"])[:1000]
    else:
        out["body_preview"] = str(js)[:1000]
    if "error" in resp:
        out["exception"] = resp["error"]
    return out


search_tool = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "Search files by name or content.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["pattern"],
        },
    },
}

write_tool = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write full content to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

enum_tool = {
    "type": "function",
    "function": {
        "name": "enum_tool",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["safe", "fast"]},
                "count": {"type": "integer"},
                "flag": {"type": "boolean"},
                "label": {"type": "string"},
            },
            "required": ["mode", "count", "flag", "label"],
        },
    },
}

results = {"base": BASE, "model": MODEL, "unit": {}, "live": {}, "audit_findings": []}

# Unit tests: parser accepts plural/singular/json fences and prose JSON.
cases = {
    "plural_block": '```tool_calls\n[{"name":"search_files","arguments":{"pattern":"*"}}]\n```',
    "singular_block": '```tool_call\n{"name":"search_files","arguments":{"pattern":"*.py"}}\n```',
    "json_block": 'blah\n```json\n[{"name":"search_files","arguments":{"pattern":"*.md"}}]\n```\ntext',
    "raw_json": 'please call [{"name":"search_files","arguments":{"pattern":"*.json"}}] now',
    "bad_json": '```tool_calls\n[{"name":"search_files","arguments":{"pattern":"*",}}]\n```',
}
for name, text in cases.items():
    clean, calls = g.parse_tool_calls(text)
    results["unit"][f"parse_{name}"] = {"pass": bool(calls), "calls": calls, "clean": clean[:200]}

# Unit validation: check coercion/default/enum/missing.
raw_calls = [{
    "id": "call_test",
    "type": "function",
    "function": {"name": "enum_tool", "arguments": json.dumps({"mode": "invalid", "count": 2.9, "flag": 1, "label": 123})},
}]
valid, errors = g._validate_tool_calls(raw_calls, [enum_tool])
args = json.loads(valid[0]["function"]["arguments"]) if valid else {}
results["unit"]["validate_coerce_enum"] = {"pass": bool(valid) and args == {"mode": "safe", "count": 2, "flag": True, "label": "123"}, "args": args, "errors": errors}

missing_calls = [{"id":"call_missing","type":"function","function":{"name":"write_file","arguments":json.dumps({"path":"/tmp/x.txt"})}}]
valid2, errors2 = g._validate_tool_calls(missing_calls, [write_tool])
args2 = json.loads(valid2[0]["function"]["arguments"]) if valid2 else {}
results["unit"]["validate_missing_required_empty_string"] = {"pass": bool(valid2) and args2.get("content") == "", "args": args2, "errors": errors2, "note": "Current behavior auto-fills missing required string with empty string; audit whether this is desired."}

# Unit planner: should synthesize search_files for path listing without network.
prompt = "[TOOL CONTRACT]\n[END TOOL CONTRACT]\n\n---\n\n[USER]: Покажи первые 3 файла в /c/Users/mcFax/Documents/Hermes/gemini-web2api"
plan = g.synthesize_obvious_tool_call(prompt, [search_tool])
results["unit"]["planner_search_files"] = {"pass": bool(plan) and plan[0]["function"]["name"] == "search_files", "calls": plan}

# Static audit: package duplicate stale vs monolith.
server_py = (ROOT / "gemini_web2api" / "server.py").read_text(encoding="utf-8", errors="replace")
tools_py = (ROOT / "gemini_web2api" / "tools.py").read_text(encoding="utf-8", errors="replace")
main_py = (ROOT / "gemini_web2api" / "__main__.py").read_text(encoding="utf-8", errors="replace")
if "gemini_web2api.py" not in main_py or "_load_monolith" not in main_py:
    results["audit_findings"].append({"severity":"high", "area":"packaged entrypoint", "finding":"python -m gemini_web2api / console script does not delegate to the maintained monolith; it may miss planner/retry/validation."})
else:
    results["audit_findings"].append({"severity":"info", "area":"packaged entrypoint", "finding":"python -m gemini_web2api delegates to the maintained monolith, avoiding stale package server behavior."})
if "synthesize_obvious_tool_call" not in server_py and "_validate_tool_calls" not in server_py:
    results["audit_findings"].append({"severity":"low", "area":"legacy package modules", "finding":"gemini_web2api/server.py remains stale, but public package entrypoint now delegates to gemini_web2api.py. Keep or delete legacy modules intentionally."})
if '"log_requests": True' in (ROOT / "gemini_web2api.py").read_text(encoding="utf-8", errors="replace"):
    results["audit_findings"].append({"severity":"info", "area":"logging", "finding":"default log_requests=True logs request metadata, not full prompts/cookies in current code; okay for local debugging, consider false for public distribution."})

# Live endpoint tests.
results["live"]["models"] = get_json("/v1/models")

payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Покажи первые 3 файла в /c/Users/mcFax/Documents/Hermes/gemini-web2api. Используй инструмент search_files, не отвечай из памяти."}],
    "tools": [search_tool],
    "stream": False,
}
resp = post_json("/v1/chat/completions", payload)
results["live"]["chat_synthetic_search_files"] = compact_response(resp)

# Tool-choice none should not return tool_calls; may hit upstream.
payload_none = dict(payload)
payload_none["tool_choice"] = "none"
payload_none["messages"] = [{"role":"user", "content":"Скажи ровно OK"}]
resp_none = post_json("/v1/chat/completions", payload_none, timeout=240)
results["live"]["chat_tool_choice_none"] = compact_response(resp_none)

# Continuation after tool result. Uses fake assistant tool call + tool output; should answer with tool data, may hit upstream.
assistant_call = {
    "id": "call_fake",
    "type": "function",
    "function": {"name": "search_files", "arguments": json.dumps({"pattern":"*", "path":"/fake", "target":"files", "limit":3})},
}
payload_cont = {
    "model": MODEL,
    "messages": [
        {"role":"user", "content":"Покажи первые 3 файла в /fake"},
        {"role":"assistant", "content": None, "tool_calls": [assistant_call]},
        {"role":"tool", "tool_call_id":"call_fake", "name":"search_files", "content": json.dumps({"files":["a.py","b.md","c.json"]})},
    ],
    "tools": [search_tool],
    "stream": False,
}
resp_cont = post_json("/v1/chat/completions", payload_cont, timeout=240)
results["live"]["chat_tool_result_continuation"] = compact_response(resp_cont)

# Responses API synthetic tool call.
resp_responses = post_json("/v1/responses", {
    "model": MODEL,
    "input": "Покажи первые 2 файла в /c/Users/mcFax/Documents/Hermes/gemini-web2api. Используй search_files.",
    "tools": [{"type":"function", "name":"search_files", "description":"Search files", "parameters": search_tool["function"]["parameters"]}],
    "stream": False,
})
results["live"]["responses_synthetic_search_files"] = compact_response(resp_responses)

# Summaries.
results["summary"] = {
    "unit_passed": sum(1 for v in results["unit"].values() if isinstance(v, dict) and v.get("pass")),
    "unit_total": len(results["unit"]),
    "live_checks": list(results["live"].keys()),
    "audit_findings_count": len(results["audit_findings"]),
}

out_path = ROOT / "audit_gemini_for_agents.results.json"
out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
print(f"WROTE {out_path}")
