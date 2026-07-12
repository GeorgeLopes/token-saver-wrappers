"""
token-saver proxy — modular token reduction pipeline.

Sits between mitmproxy and headroom in the token-saver pod.
Each module is independently toggleable via FEATURE_* env vars.

Pipeline order (request):
  1. prompt_cache      — deterministic cache (SHA256 body → stored response)
  2. summarize         — collapse old conversation history into a summary
  3. strip_system      — deduplicate repeated instructions in system prompt
  4. minify_tools      — remove descriptions/defaults from tool definitions
  5. router            — classify complexity, switch to cheaper model for simple tasks
  6. translate         — pt-BR → EN translation (existing)

Pipeline order (response):
  1. translate         — EN → pt-BR translation (existing)
  2. strip_response    — remove unused metadata fields
  3. prompt_cache      — store response in cache

Visibility:
  - X-Token-Saver-Modules response header lists active modules
  - GET /dashboard — HTML dashboard with per-module stats
  - GET /stats — JSON stats
  - GET /health — module status + cache info

Dependencies: deep-translator (optional, for translate module only)
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Feature flags — each module toggleable via env
# ---------------------------------------------------------------------------
FEATURE_TRANSLATE = os.environ.get("FEATURE_TRANSLATE", os.environ.get("TRANSLATE_ENABLED", "0")) not in ("0", "false", "")
FEATURE_STRIP_RESPONSE = os.environ.get("FEATURE_STRIP_RESPONSE", "1") not in ("0", "false", "")
FEATURE_STRIP_SYSTEM = os.environ.get("FEATURE_STRIP_SYSTEM", "1") not in ("0", "false", "")
FEATURE_MINIFY_TOOLS = os.environ.get("FEATURE_MINIFY_TOOLS", "1") not in ("0", "false", "")
FEATURE_SUMMARIZE = os.environ.get("FEATURE_SUMMARIZE", "0") not in ("0", "false", "")
FEATURE_PROMPT_CACHE = os.environ.get("FEATURE_PROMPT_CACHE", "0") not in ("0", "false", "")
FEATURE_ROUTER = os.environ.get("FEATURE_ROUTER", "0") not in ("0", "false", "")

# Router config — cheap model for simple tasks
ROUTER_CHEAP_MODEL = os.environ.get("ROUTER_CHEAP_MODEL", "deepseek-chat")
ROUTER_CHEAP_PROVIDER_URL = os.environ.get("ROUTER_CHEAP_PROVIDER_URL", "")  # if set, bypass headroom

# Summarizer config — cheap model for summarization
SUMMARIZE_MODEL = os.environ.get("SUMMARIZE_MODEL", "deepseek-chat")
SUMMARIZE_MAX_TOKENS = int(os.environ.get("SUMMARIZE_MAX_TOKENS", "3000"))
SUMMARIZE_THRESHOLD = int(os.environ.get("SUMMARIZE_THRESHOLD", "15000"))

# Network config
HEADROOM_HOST = os.environ.get("HEADROOM_HOST", "127.0.0.1")
HEADROOM_PORT = int(os.environ.get("HEADROOM_PORT", "8787"))
LISTEN_PORT = int(os.environ.get("TRANSLATE_LISTEN_PORT", "8786"))
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/token-saver-cache")

HEADROOM_BASE = f"http://{HEADROOM_HOST}:{HEADROOM_PORT}"

# ---------------------------------------------------------------------------
# Stats collector — thread-safe counters
# ---------------------------------------------------------------------------
@dataclass
class ModuleStats:
    requests: int = 0
    tokens_saved_est: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    summaries: int = 0
    reroutes: int = 0  # router redirects to cheap model
    total_requests: int = 0  # all requests through proxy
    strips_system: int = 0
    strips_tools: int = 0
    strips_response: int = 0
    translates_in: int = 0
    translates_out: int = 0

_stats = ModuleStats()
_stats_lock = threading.Lock()

ACTIVE_MODULES: list[str] = []

def _update_active_modules():
    ACTIVE_MODULES.clear()
    if FEATURE_PROMPT_CACHE: ACTIVE_MODULES.append("prompt_cache")
    if FEATURE_SUMMARIZE: ACTIVE_MODULES.append("summarize")
    if FEATURE_STRIP_SYSTEM: ACTIVE_MODULES.append("strip_system")
    if FEATURE_MINIFY_TOOLS: ACTIVE_MODULES.append("minify_tools")
    if FEATURE_ROUTER: ACTIVE_MODULES.append("router")
    if FEATURE_TRANSLATE: ACTIVE_MODULES.append("translate")
    if FEATURE_STRIP_RESPONSE: ACTIVE_MODULES.append("strip_response")

_update_active_modules()

# ---------------------------------------------------------------------------
# Language detection & translation (translate module)
# ---------------------------------------------------------------------------
PT_PATTERNS = re.compile(
    r'\b(?:você|vocês|para|como|uma|um|não|mais|muito|isso|esse|essa|'
    r'aquele|aquela|são|estão|ser|fazer|pode|faz|tem|têm|porque|'
    r'quando|onde|qual|também|então|ainda|assim|porém|'
    r'[áâãàéêíóôõúç])',
    re.IGNORECASE,
)
SKIP_TRANSLATE_RE = re.compile(
    r'^[`{}\[\]]|^\s*$|^https?://|^/[^/\s]+/|^<[a-z]+|^```|^import |^from |^def |^class '
)

_translator_cache: Any = None
_translator_pt_cache: Any = None


def _get_translator():
    global _translator_cache
    if _translator_cache is None:
        try:
            from deep_translator import GoogleTranslator
            _translator_cache = GoogleTranslator(source='auto', target='en')
        except ImportError:
            _translator_cache = False
    return _translator_cache if _translator_cache is not False else None


def _get_translator_pt():
    global _translator_pt_cache
    if _translator_pt_cache is None:
        try:
            from deep_translator import GoogleTranslator
            _translator_pt_cache = GoogleTranslator(source='en', target='pt')
        except ImportError:
            _translator_pt_cache = False
    return _translator_pt_cache if _translator_pt_cache is not False else None


def is_portuguese(text: str) -> bool:
    if not text or not text.strip() or len(text) < 8:
        return False
    accented = len(re.findall(r'[áâãàéêíóôõúç]', text, re.IGNORECASE))
    pt_words = len(PT_PATTERNS.findall(text))
    return (accented >= 2) or (pt_words >= 3)


def should_translate_text(text: str) -> bool:
    if not text or not text.strip():
        return False
    if SKIP_TRANSLATE_RE.match(text.strip()):
        return False
    return is_portuguese(text)


def translate_to_en(text: str) -> str:
    t = _get_translator()
    if t is None:
        return text
    try:
        result = t.translate(text)
        return result if result else text
    except Exception:
        return text


def translate_to_pt(text: str) -> str:
    t = _get_translator_pt()
    if t is None:
        return text
    try:
        result = t.translate(text)
        return result if result else text
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Module 1: Deterministic prompt cache (SHA256 body → stored response)
# ---------------------------------------------------------------------------
_cache_db: sqlite3.Connection | None = None
_cache_lock = threading.Lock()


def _ensure_cache_db() -> sqlite3.Connection:
    global _cache_db
    if _cache_db is not None:
        return _cache_db
    os.makedirs(CACHE_DIR, exist_ok=True)
    _cache_db = sqlite3.connect(f"{CACHE_DIR}/prompt_cache.db", check_same_thread=False)
    _cache_db.execute("PRAGMA journal_mode=WAL")
    _cache_db.execute(
        "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, response BLOB, model TEXT, created REAL)"
    )
    _cache_db.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created)")
    _cache_db.commit()
    return _cache_db


def _cache_key(body: bytes, model: str) -> str:
    """Deterministic cache key: SHA256(body) + model."""
    h = hashlib.sha256(body)
    h.update(model.encode())
    return h.hexdigest()


def cache_lookup(body: bytes, model: str) -> tuple[bool, bytes | None]:
    """Returns (hit, response_body)."""
    if not FEATURE_PROMPT_CACHE or not body:
        return False, None
    try:
        db = _ensure_cache_db()
        key = _cache_key(body, model)
        row = db.execute("SELECT response FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            with _stats_lock:
                _stats.cache_hits += 1
            return True, bytes(row[0])
        with _stats_lock:
            _stats.cache_misses += 1
    except Exception:
        pass
    return False, None


def cache_store(body: bytes, model: str, response: bytes) -> None:
    if not FEATURE_PROMPT_CACHE or not body or not response:
        return
    try:
        db = _ensure_cache_db()
        key = _cache_key(body, model)
        db.execute(
            "INSERT OR REPLACE INTO cache(key, response, model, created) VALUES (?, ?, ?, ?)",
            (key, response, model, time.time()),
        )
        db.commit()
        # Cleanup: keep max 10K entries
        db.execute("DELETE FROM cache WHERE rowid NOT IN (SELECT rowid FROM cache ORDER BY created DESC LIMIT 10000)")
        db.commit()
    except Exception:
        pass


def cache_stats() -> dict:
    try:
        db = _ensure_cache_db()
        count = db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        size = os.path.getsize(f"{CACHE_DIR}/prompt_cache.db") if os.path.exists(f"{CACHE_DIR}/prompt_cache.db") else 0
        return {"entries": count, "size_bytes": size}
    except Exception:
        return {"entries": 0, "size_bytes": 0}


# ---------------------------------------------------------------------------
# Module 2: Conversation summarization
# ---------------------------------------------------------------------------
def _estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token for English, ~3 for Portuguese."""
    return len(text) // 3


def _build_summary_request(messages: list, model: str) -> dict:
    """Build a summary request by taking oldest messages + a summarization prompt."""
    # Take oldest ~60% of messages as context to summarize
    split = max(1, len(messages) * 6 // 10)
    old = messages[:split]
    new = messages[split:]

    # Build text from old messages
    old_text_parts = []
    for m in old:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            old_text_parts.append(f"[{role}]: {content[:500]}")  # truncate each
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    old_text_parts.append(f"[{role}]: {part['text'][:500]}")

    old_text = "\n".join(old_text_parts[-20:])  # keep last 20 segments

    summary_msg = {
        "role": "user",
        "content": (
            "Summarize the following conversation history in 2-3 sentences, "
            "preserving key facts, decisions, and code patterns. Write in English.\n\n"
            f"{old_text}\n\nSummary:"
        ),
    }

    # Build request to cheap model for summarization
    return {
        "model": model,
        "messages": [summary_msg],
        "max_tokens": 200,
        "temperature": 0,
    }


def summarize_conversation(messages: list) -> list:
    """Replace old conversation history with a summary paragraph."""
    if not FEATURE_SUMMARIZE or not messages:
        return messages

    # Estimate total tokens
    total_chars = sum(
        len(m.get("content", "")) if isinstance(m.get("content"), str)
        else sum(len(p.get("text", "")) for p in m.get("content", []) if isinstance(p, dict) and p.get("type") == "text")
        for m in messages
    )
    if total_chars < SUMMARIZE_THRESHOLD * 3:
        return messages  # below threshold

    try:
        req_body = json.dumps(_build_summary_request(messages, SUMMARIZE_MODEL)).encode()
        req = urllib.request.Request(
            f"{HEADROOM_BASE}/v1/chat/completions",
            data=req_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            summary = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        if summary:
            # Keep system prompt + last 40% of messages + summary
            split = max(1, len(messages) * 6 // 10)
            new_messages = messages[split:]
            # Insert summary as a system message right after the system prompt
            summary_msg = {
                "role": "system",
                "content": f"[Previous conversation summary]: {summary.strip()}",
            }
            # Find system message index and insert after it
            insert_idx = 1 if (new_messages and new_messages[0].get("role") == "system") else 0
            new_messages.insert(insert_idx, summary_msg)

            with _stats_lock:
                _stats.summaries += 1
                _stats.tokens_saved_est += (total_chars // 3) - (_estimate_tokens(summary) + len(new_messages) * 100)

            print(f"[summarize] collapsed {len(messages)}→{len(new_messages)} msgs "
                  f"(~{total_chars // 3}→~{sum(_estimate_tokens(m.get('content','')) for m in new_messages)} tokens est)",
                  file=sys.stderr, flush=True)
            return new_messages
    except Exception as e:
        print(f"[summarize] failed: {e}", file=sys.stderr, flush=True)

    return messages


# ---------------------------------------------------------------------------
# Module 3: System prompt dedup
# ---------------------------------------------------------------------------
# Common repetitive patterns in coding agent system prompts
# Hermes-specific patterns added after real-world testing (8 requests, 0 hits).
DEDUP_PATTERNS = [
    # Hermes: repeated tool-use enforcement (multiple variants)
    (re.compile(r'(You MUST use your tools[^.]*\.)\s*(?=.*You MUST use your tools)', re.DOTALL), r'\1'),
    (re.compile(r'(Keep working until the task is actually complete[^.]*\.)\s*(?=.*Keep working until the task)', re.DOTALL), r'\1'),
    (re.compile(r'(Do not stop with a summary[^.]*\.)\s*(?=.*Do not stop)', re.DOTALL), r'\1'),
    # Generic: repeated "MUST" / "CRITICAL" / "IMPORTANT" admonitions
    (re.compile(r'(You MUST[^.]*\.)\s*(?=.*\1)', re.DOTALL), r'\1'),
    (re.compile(r'(CRITICAL:[^.]*\.)\s*(?=.*\1)', re.DOTALL), r'\1'),
    (re.compile(r'(IMPORTANT:[^.]*\.)\s*(?=.*\1)', re.DOTALL), r'\1'),
    # Repeated "Do not" instructions
    (re.compile(r'(Do not[^.]*\.)\s*(?=.*\1)', re.DOTALL), r'\1'),
    # Repeated tool-use enforcement blocks
    (re.compile(r'(When you say you will perform an action[^.]*\.)\s*(?=.*\1)', re.DOTALL), r'\1'),
    # Hermes: repeated "If you have tools available" admonitions
    (re.compile(r'(If you have tools available[^.]*\.)\s*(?=.*If you have tools)', re.DOTALL), r'\1'),
    # Collapse multiple blank lines (3+ → 2)
    (re.compile(r'\n{3,}'), '\n\n'),
    # Collapse multiple spaces at line starts
    (re.compile(r'\n {4,}'), '\n  '),
]


def strip_system_prompt(messages: list) -> list:
    """Deduplicate repeated instructions in system messages."""
    if not FEATURE_STRIP_SYSTEM or not messages:
        return messages

    modified = False
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        original_len = len(content)

        # Apply dedup patterns
        for pattern, replacement in DEDUP_PATTERNS:
            new_content = pattern.sub(replacement, content)
            if new_content != content:
                content = new_content
                modified = True

        # Trim trailing whitespace on each line
        content = "\n".join(line.rstrip() for line in content.split("\n"))
        content = content.strip()

        if modified:
            msg["content"] = content
            saved = original_len - len(content)
            with _stats_lock:
                _stats.strips_system += 1
                _stats.tokens_saved_est += saved // 3
            print(f"[strip_system] saved ~{saved // 3} tokens", file=sys.stderr, flush=True)

    return messages


# ---------------------------------------------------------------------------
# Module 4: Tool definition minification
# ---------------------------------------------------------------------------
TOOL_STRIP_KEYS = {
    "description": True,
    "examples": True,
    "default": True,
    "title": True,
    "additionalProperties": True,
    "minItems": True,
    "maxItems": True,
    "minLength": True,
    "maxLength": True,
    "minimum": True,
    "maximum": True,
    "pattern": True,
    "nullable": True,
    "readOnly": True,
    "writeOnly": True,
}


def _minify_schema(obj: Any) -> Any:
    """Recursively strip verbose JSON Schema keys."""
    if isinstance(obj, dict):
        # Strip keys
        result = {}
        for k, v in obj.items():
            if k in TOOL_STRIP_KEYS:
                continue
            result[k] = _minify_schema(v)
        return result
    elif isinstance(obj, list):
        return [_minify_schema(item) for item in obj]
    return obj


def minify_tools(body: dict) -> dict:
    """Strip descriptions and verbose keys from tool definitions."""
    if not FEATURE_MINIFY_TOOLS:
        return body

    tools = body.get("tools")
    if not tools or not isinstance(tools, list):
        return body

    original_chars = len(json.dumps(tools))
    body["tools"] = _minify_schema(tools)
    new_chars = len(json.dumps(body["tools"]))

    if new_chars < original_chars:
        with _stats_lock:
            _stats.strips_tools += 1
            _stats.tokens_saved_est += (original_chars - new_chars) // 3
        print(f"[minify_tools] saved ~{(original_chars - new_chars) // 3} tokens", file=sys.stderr, flush=True)

    return body


# ---------------------------------------------------------------------------
# Module 5: Model router
# ---------------------------------------------------------------------------
# Keywords that indicate a complex task (deserves the full model)
COMPLEX_KEYWORDS = re.compile(
    r'\b(?:'
    r'refator|refactor|implement|debug|diagnos|arquitet|architect|'
    r'design pattern|migrat|migração|security|segurança|vulnerab|'
    r'performance|otimiz|optimiz|concurrency|paralel|race condition|'
    r'schema|database|transaction|distributed|recurs|complex|'
    r'explain|explique|analyze|analise|review full|full review'
    r')',
    re.IGNORECASE,
)

# Keywords that indicate a simple task
SIMPLE_KEYWORDS = re.compile(
    r'\b(?:'
    r'list|lista|show|mostra|what is|o que é|how to|como faz|'
    r'read|ler|cat|ls|find|grep|search|buscar|'
    r'simple|simples|quick|rápido|basic|básico|'
    r'fix typo|corrige typo|rename|renomeia|commit|push|pull'
    r')',
    re.IGNORECASE,
)


def _get_last_user_message(messages: list) -> str:
    """Extract last user message content."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")
    return ""


def router_should_reroute(messages: list) -> tuple[bool, str]:
    """Returns (should_reroute, reason)."""
    if not FEATURE_ROUTER or not messages:
        return False, ""

    text = _get_last_user_message(messages)
    if not text:
        return False, ""

    # Complex task → stay with main model
    if COMPLEX_KEYWORDS.search(text):
        return False, "complex task detected"

    # Simple task → reroute to cheap model
    if SIMPLE_KEYWORDS.search(text):
        return True, "simple task detected"

    # Heuristic: short messages (< 100 chars) tend to be simple
    if len(text) < 100:
        return True, "short query"

    return False, ""


def router_rewrite_body(body: dict) -> dict | None:
    """Rewrite request body to use cheap model. Returns modified body or None if no change."""
    if not FEATURE_ROUTER:
        return None

    messages = body.get("messages", [])
    should_reroute, reason = router_should_reroute(messages)

    if not should_reroute:
        return None

    body = dict(body)  # shallow copy
    original_model = body.get("model", "unknown")
    body["model"] = ROUTER_CHEAP_MODEL

    # If ROUTER_CHEAP_PROVIDER_URL is set, inject x-headroom headers to bypass headroom's default
    if ROUTER_CHEAP_PROVIDER_URL:
        # We can't inject headers here — they're set by mitm_addon
        pass

    with _stats_lock:
        _stats.reroutes += 1
        _stats.tokens_saved_est += 5000  # rough estimate: cheap model is ~80% cheaper

    print(f"[router] {original_model}→{ROUTER_CHEAP_MODEL} ({reason})",
          file=sys.stderr, flush=True)
    return body


# ---------------------------------------------------------------------------
# Module 6: Response field stripping
# ---------------------------------------------------------------------------
RESPONSE_STRIP_KEYS = {
    "system_fingerprint",
    "logprobs",
    "finish_details",
    "index",
    "usage",  # headroom adds its own usage header; strip provider's
    "object",
    "created",
}


def _strip_response_obj(obj: Any) -> Any:
    """Recursively remove verbose keys from response JSON."""
    if isinstance(obj, dict):
        return {
            k: _strip_response_obj(v)
            for k, v in obj.items()
            if k not in RESPONSE_STRIP_KEYS
        }
    elif isinstance(obj, list):
        return [_strip_response_obj(item) for item in obj]
    return obj


def strip_response(body: bytes) -> bytes:
    """Remove unused metadata from response JSON."""
    if not FEATURE_STRIP_RESPONSE or not body:
        return body

    try:
        data = json.loads(body)
        original_len = len(body)

        stripped = _strip_response_obj(data)

        new_body = json.dumps(stripped, ensure_ascii=False).encode("utf-8")
        saved = original_len - len(new_body)
        with _stats_lock:
            _stats.strips_response += 1
            if saved > 0:
                _stats.tokens_saved_est += saved // 4  # conservative: ~4 chars/token
        if saved > 0:
            print(f"[strip_response] removed {saved} bytes", file=sys.stderr, flush=True)
        return new_body
    except (json.JSONDecodeError, TypeError):
        pass

    return body


# ---------------------------------------------------------------------------
# Translation module wrappers (for pipeline integration)
# ---------------------------------------------------------------------------

def translate_messages_in(messages: list) -> list:
    """Translate system/user messages pt→EN."""
    if not FEATURE_TRANSLATE or not messages:
        return messages

    for msg in messages:
        content = msg.get("content")
        if not content:
            continue
        role = msg.get("role", "")
        if role not in ("system", "user"):
            continue

        if isinstance(content, str):
            if should_translate_text(content):
                translated = translate_to_en(content)
                if translated and translated != content:
                    msg["content"] = translated
                    with _stats_lock:
                        _stats.translates_in += 1
                        _stats.tokens_saved_est += (len(content) - len(translated)) // 3
                    print(f"[translate] {role}: pt→EN", file=sys.stderr, flush=True)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if should_translate_text(text):
                        translated = translate_to_en(text)
                        if translated and translated != text:
                            part["text"] = translated
                            with _stats_lock:
                                _stats.translates_in += 1

    return messages


def translate_response_out(body: bytes, content_type: str) -> bytes:
    """Translate assistant response EN→pt."""
    if not FEATURE_TRANSLATE:
        return body
    if "text/event-stream" in content_type:
        return body  # SSE passthrough
    if "application/json" not in content_type:
        return body

    try:
        data = json.loads(body)
        modified = False

        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                translated = translate_to_pt(content)
                if translated and translated != content:
                    msg["content"] = translated
                    modified = True
                    with _stats_lock:
                        _stats.translates_out += 1

        for block in data.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text.strip():
                    translated = translate_to_pt(text)
                    if translated and translated != text:
                        block["text"] = translated
                        modified = True

        if modified:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (json.JSONDecodeError, TypeError):
        pass

    return body


# ---------------------------------------------------------------------------
# Request pipeline
# ---------------------------------------------------------------------------

def process_request(body: bytes) -> tuple[bytes, dict | None]:
    """
    Run request through all enabled modules.
    Returns (processed_body, cache_hit_response_dict_or_None).
    """
    if not body:
        return body, None

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body, None

    model = data.get("model", "unknown")

    # 1. Prompt cache lookup
    if FEATURE_PROMPT_CACHE:
        hit, cached = cache_lookup(body, model)
        if hit and cached:
            print("[prompt_cache] HIT", file=sys.stderr, flush=True)
            return body, json.loads(cached)

    # 2. Summarize conversation
    messages = data.get("messages")
    if messages and isinstance(messages, list):
        data["messages"] = summarize_conversation(messages)

    # 3. Strip system prompt
    if messages and isinstance(messages, list):
        data["messages"] = strip_system_prompt(data["messages"])

    # 4. Minify tools
    data = minify_tools(data)

    # 5. Router
    routed = router_rewrite_body(data)
    if routed:
        data = routed
        model = data.get("model", model)

    # 6. Translate pt→EN
    messages = data.get("messages")
    if messages and isinstance(messages, list):
        data["messages"] = translate_messages_in(messages)

    # Also translate system prompt (Anthropic)
    system = data.get("system")
    if isinstance(system, str) and FEATURE_TRANSLATE:
        if should_translate_text(system):
            translated = translate_to_en(system)
            if translated and translated != system:
                data["system"] = translated
    elif isinstance(system, list) and FEATURE_TRANSLATE:
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if should_translate_text(text):
                    translated = translate_to_en(text)
                    if translated != text:
                        block["text"] = translated

    with _stats_lock:
        _stats.total_requests += 1

    return json.dumps(data, ensure_ascii=False).encode("utf-8"), None


def process_response(body: bytes, content_type: str, request_body: bytes, model: str) -> bytes:
    """Run response through all enabled modules."""
    if not body:
        return body

    # 1. Translate EN→pt
    body = translate_response_out(body, content_type)

    # 2. Strip response metadata
    body = strip_response(body)

    # 3. Store in prompt cache
    if FEATURE_PROMPT_CACHE:
        cache_store(request_body, model, body)

    return body


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token Saver Proxy</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 2rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; color: #58a6ff; }}
  .subtitle {{ color: #8b949e; margin-bottom: 2rem; font-size: 0.9rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.2rem; }}
  .card h3 {{ font-size: 0.75rem; text-transform: uppercase; color: #8b949e; margin-bottom: 0.5rem; letter-spacing: 0.05em; }}
  .card .value {{ font-size: 1.8rem; font-weight: 700; color: #58a6ff; }}
  .card .unit {{ font-size: 0.8rem; color: #8b949e; }}
  .modules {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 2rem; }}
  .module {{ padding: 0.3rem 0.8rem; border-radius: 20px; font-size: 0.8rem; font-weight: 600; }}
  .module.on {{ background: #1a7f37; color: #fff; }}
  .module.off {{ background: #21262d; color: #8b949e; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #30363d; font-size: 0.85rem; }}
  th {{ color: #8b949e; font-weight: 600; }}
  td {{ color: #c9d1d9; }}
  .positive {{ color: #3fb950; }}
  .refresh {{ color: #8b949e; font-size: 0.75rem; }}
</style>
</head>
<body>
<h1>Token Saver Proxy</h1>
<p class="subtitle">Modular token reduction pipeline — refresh for live stats</p>

<div class="modules">
  __MODULES__
</div>

<div class="grid">
  <div class="card">
    <h3>Total Requests</h3>
    <div class="value">__TOTAL_REQUESTS__</div>
  </div>
  <div class="card">
    <h3>Tokens Saved (est)</h3>
    <div class="value">__TOKENS_SAVED__<span class="unit"> tokens</span></div>
  </div>
  <div class="card">
    <h3>Cache Hit Rate</h3>
    <div class="value">__CACHE_HIT_RATE__<span class="unit">%</span></div>
  </div>
  <div class="card">
    <h3>Cache Entries</h3>
    <div class="value">__CACHE_ENTRIES__</div>
  </div>
</div>

<table>
  <thead>
    <tr><th>Module</th><th>Activations</th><th>Detail</th></tr>
  </thead>
  <tbody>
    __MODULE_ROWS__
  </tbody>
</table>

<p class="refresh">Auto-refreshes every 5s</p>
<script>setTimeout(() => location.reload(), 5000);</script>
</body>
</html>"""


def build_dashboard() -> bytes:
    with _stats_lock:
        s = _stats

    cache = cache_stats()
    hit_rate = 0
    if (s.cache_hits + s.cache_misses) > 0:
        hit_rate = round(s.cache_hits / (s.cache_hits + s.cache_misses) * 100, 1)

    modules_html = ""
    module_names = ["prompt_cache", "summarize", "strip_system", "minify_tools", "router", "translate", "strip_response"]
    for name in module_names:
        active = name in ACTIVE_MODULES
        modules_html += f'<span class="module {"on" if active else "off"}">{name}</span> '

    rows = ""
    for name, count, detail in [
        ("prompt_cache", s.cache_hits, f"{s.cache_hits} hits / {s.cache_misses} misses"),
        ("summarize", s.summaries, f"{s.summaries} conversations collapsed"),
        ("strip_system", s.strips_system, f"{s.strips_system} system prompts deduped"),
        ("minify_tools", s.strips_tools, f"{s.strips_tools} tool defs minified"),
        ("router", s.reroutes, f"{s.reroutes} rerouted to cheaper model"),
        ("translate", s.translates_in, f"{s.translates_in} in / {s.translates_out} out"),
        ("strip_response", s.strips_response, f"{s.strips_response} responses stripped"),
    ]:
        rows += f"<tr><td>{name}</td><td>{count}</td><td>{detail}</td></tr>"

    html = DASHBOARD_HTML
    html = html.replace("__MODULES__", modules_html)
    html = html.replace("__TOTAL_REQUESTS__", str(s.total_requests))
    html = html.replace("__TOKENS_SAVED__", f"{s.tokens_saved_est:,}")
    html = html.replace("__CACHE_HIT_RATE__", str(hit_rate))
    html = html.replace("__CACHE_ENTRIES__", str(cache["entries"]))
    html = html.replace("__MODULE_ROWS__", rows)
    return html.encode()


def build_stats_json() -> bytes:
    with _stats_lock:
        s = _stats
    cache = cache_stats()
    data = {
        "modules": ACTIVE_MODULES,
        "total_requests": s.total_requests,
        "tokens_saved_est": s.tokens_saved_est,
        "cache": {
            "hits": s.cache_hits,
            "misses": s.cache_misses,
            "entries": cache["entries"],
            "size_bytes": cache["size_bytes"],
        },
        "activations": {
            "prompt_cache": s.cache_hits,
            "summarize": s.summaries,
            "strip_system": s.strips_system,
            "minify_tools": s.strips_tools,
            "router": s.reroutes,
            "translate_in": s.translates_in,
            "translate_out": s.translates_out,
            "strip_response": s.strips_response,
        },
    }
    return json.dumps(data, indent=2).encode()


# ---------------------------------------------------------------------------
# HTTP Proxy Handler
# ---------------------------------------------------------------------------

class TokenSaverProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def _forward(self, method: str, path: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        url = f"{HEADROOM_BASE}{path}"
        req = urllib.request.Request(url, data=body, method=method)

        forward_headers = [
            "content-type", "authorization", "x-api-key", "x-headroom-base-url",
            "x-headroom-original-path", "anthropic-version", "x-stainless-",
            "user-agent", "accept", "x-request-id",
        ]
        for key, value in headers.items():
            kl = key.lower()
            if any(kl == h or kl.startswith(h) for h in forward_headers):
                req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_headers = dict(resp.headers)
                resp_body = resp.read()
                return resp.status, resp_headers, resp_body
        except urllib.error.HTTPError as e:
            resp_headers = dict(e.headers) if e.headers else {}
            resp_body = e.read()
            return e.code, resp_headers, resp_body
        except Exception as e:
            error_body = json.dumps({"error": str(e)}).encode()
            return 502, {"content-type": "application/json"}, error_body

    def _send(self, status: int, resp_headers: dict, body: bytes, extra_headers: dict | None = None):
        self.send_response(status)
        for key, value in resp_headers.items():
            if key.lower() not in ("transfer-encoding", "content-length"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Token-Saver-Modules", ", ".join(ACTIVE_MODULES))

        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)

        self.end_headers()
        self.wfile.write(body)

    def _handle_llm_request(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        content_type = self.headers.get("Content-Type", "")

        if not body or "application/json" not in content_type:
            # Passthrough
            status, resp_headers, resp_body = self._forward(self.command, self.path, dict(self.headers), body)
            self._send(status, resp_headers, resp_body)
            return

        # Extract model for cache key and stats
        try:
            req_model = json.loads(body).get("model", "unknown")
        except Exception:
            req_model = "unknown"

        # ---- Request pipeline ----
        original_body = body
        processed_body, cache_hit = process_request(body)

        if cache_hit is not None:
            # Cache hit — return stored response directly
            cached_body = json.dumps(cache_hit, ensure_ascii=False).encode()
            self._send(200, {"content-type": "application/json", "x-token-saver-cache": "HIT"}, cached_body)
            return

        # ---- Forward to headroom ----
        status, resp_headers, resp_body = self._forward(
            self.command, self.path, dict(self.headers), processed_body
        )

        # ---- Response pipeline ----
        resp_ct = resp_headers.get("content-type", resp_headers.get("Content-Type", ""))
        resp_body = process_response(resp_body, resp_ct, original_body, req_model)

        # Pass through SSE streaming
        extra = {}
        if "text/event-stream" in resp_ct:
            extra["Cache-Control"] = "no-cache"
            extra["Connection"] = "keep-alive"

        self._send(status, resp_headers, resp_body, extra)

    def _handle_passthrough(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        status, resp_headers, resp_body = self._forward(self.command, self.path, dict(self.headers), body)
        self._send(status, resp_headers, resp_body)

    def do_GET(self):
        if self.path == "/dashboard":
            self._send(200, {"content-type": "text/html; charset=utf-8"}, build_dashboard())
            return
        if self.path == "/stats":
            self._send(200, {"content-type": "application/json"}, build_stats_json())
            return
        if self.path in ("/health", "/readyz"):
            data = json.dumps({
                "status": "ok",
                "modules": ACTIVE_MODULES,
                "translator_available": _get_translator() is not None if FEATURE_TRANSLATE else None,
                "cache_entries": cache_stats()["entries"],
            }).encode()
            self._send(200, {"content-type": "application/json"}, data)
            return
        self._handle_passthrough()

    def do_POST(self):
        path = self.path.rstrip("/")
        if path.endswith("/chat/completions") or path.endswith("/v1/messages"):
            self._handle_llm_request()
        else:
            self._handle_passthrough()

    def do_PUT(self):
        self._handle_passthrough()

    def do_DELETE(self):
        self._handle_passthrough()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("X-Token-Saver-Modules", ", ".join(ACTIVE_MODULES))
        self.end_headers()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    features = [
        ("translate", FEATURE_TRANSLATE),
        ("strip_response", FEATURE_STRIP_RESPONSE),
        ("strip_system", FEATURE_STRIP_SYSTEM),
        ("minify_tools", FEATURE_MINIFY_TOOLS),
        ("summarize", FEATURE_SUMMARIZE),
        ("prompt_cache", FEATURE_PROMPT_CACHE),
        ("router", FEATURE_ROUTER),
    ]
    status = " ".join(f"{name}={on}" for name, on in features)
    print(f"[token-saver-proxy] listen=0.0.0.0:{LISTEN_PORT} headroom={HEADROOM_BASE} {status}",
          file=sys.stderr, flush=True)
    print(f"[token-saver-proxy] dashboard: http://0.0.0.0:{LISTEN_PORT}/dashboard",
          file=sys.stderr, flush=True)

    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), TokenSaverProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _cache_db:
            _cache_db.close()


if __name__ == "__main__":
    main()
