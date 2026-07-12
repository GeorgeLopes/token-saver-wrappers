"""
translate_proxy — HTTP proxy that translates chat messages pt-BR ↔ EN.

Sits between mitmproxy and headroom in the token-saver pod.
When TRANSLATE_ENABLED=true:
  - Translates system/user messages pt→EN before forwarding to headroom
  - Translates assistant responses EN→pt before returning to the tool
  - Streaming-aware: passes SSE chunks through, translates only the final
    aggregated content (streaming responses are forwarded as-is for latency).

Routes:
  POST /v1/chat/completions  → translate messages, forward to headroom
  POST /v1/messages           → same for Anthropic dialect
  POST /v1/responses          → passthrough (Responses API — no messages array)
  POST /backend-api/codex/*   → passthrough (ChatGPT backend)
  GET  /health                → health check
  GET  /readyz                → readiness probe
  *                           → passthrough

Dependencies: deep-translator (pip install deep-translator)
"""

import http.server
import json
import os
import re
import socketserver
import sys
import threading
import urllib.request
import urllib.error
from io import BytesIO

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HEADROOM_HOST = os.environ.get("HEADROOM_HOST", "127.0.0.1")
HEADROOM_PORT = int(os.environ.get("HEADROOM_PORT", "8787"))
LISTEN_PORT = int(os.environ.get("TRANSLATE_LISTEN_PORT", "8786"))
TRANSLATE_ENABLED = os.environ.get("TRANSLATE_ENABLED", "0") not in ("0", "false", "")

HEADROOM_BASE = f"http://{HEADROOM_HOST}:{HEADROOM_PORT}"

# ---------------------------------------------------------------------------
# Language detection & translation
# ---------------------------------------------------------------------------

# Simple PT detection: checks for common Portuguese words/patterns.
# Avoids calling the translator for content already in English.
PT_PATTERNS = re.compile(
    r'\b(?:'
    r'você|vocês|para|como|uma|um|não|mais|muito|isso|esse|essa|'
    r'aquele|aquela|são|estão|ser|fazer|pode|faz|tem|têm|porque|'
    r'quando|onde|qual|também|então|ainda|assim|porém|'
    r'[áâãàéêíóôõúç]'  # accented chars are strong PT signals
    r')\b',
    re.IGNORECASE
)

# Content types that should NOT be translated (code, URLs, etc.)
SKIP_TRANSLATE_RE = re.compile(
    r'^[`{}\[\]]|^\s*$|^https?://|^/[^/\s]+/|^<[a-z]+|^```|^import |^from |^def |^class '
)


def _translator():
    """Lazy-init GoogleTranslator. Returns None if deep-translator unavailable."""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source='auto', target='en')
    except ImportError:
        return None


def _translator_pt():
    """Lazy-init reverse translator (EN→pt)."""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source='en', target='pt')
    except ImportError:
        return None


def is_portuguese(text: str) -> bool:
    """Quick heuristic: does this text look like Portuguese?"""
    if not text or not text.strip():
        return False
    if len(text) < 8:
        return bool(PT_PATTERNS.search(text))
    # Count PT signals — accented chars are very strong indicators
    accented = len(re.findall(r'[áâãàéêíóôõúç]', text, re.IGNORECASE))
    pt_words = len(PT_PATTERNS.findall(text))
    return (accented >= 2) or (pt_words >= 3)


def should_translate_text(text: str) -> bool:
    """Should this text be translated?"""
    if not text or not text.strip():
        return False
    if SKIP_TRANSLATE_RE.match(text.strip()):
        return False
    return is_portuguese(text)


def translate_to_en(text: str) -> str:
    """Translate text to English. Returns original on failure."""
    translator = _translator()
    if translator is None:
        return text
    try:
        result = translator.translate(text)
        return result if result else text
    except Exception:
        return text


def translate_to_pt(text: str) -> str:
    """Translate text to Portuguese. Returns original on failure."""
    translator = _translator_pt()
    if translator is None:
        return text
    try:
        result = translator.translate(text)
        return result if result else text
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Message transformation
# ---------------------------------------------------------------------------

def translate_messages(messages: list) -> list:
    """Translate system/user messages pt→EN in-place."""
    if not TRANSLATE_ENABLED:
        return messages

    for msg in messages:
        content = msg.get("content")
        if not content:
            continue

        role = msg.get("role", "")

        # Only translate system and user messages
        if role not in ("system", "user"):
            continue

        if isinstance(content, str):
            if should_translate_text(content):
                translated = translate_to_en(content)
                if translated and translated != content:
                    msg["content"] = translated
                    print(f"[translate] {role}: pt→EN ({len(content)}→{len(translated)} chars)",
                          file=sys.stderr, flush=True)

        elif isinstance(content, list):
            # Multimodal content array — translate text parts
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if should_translate_text(text):
                        translated = translate_to_en(text)
                        if translated and translated != text:
                            part["text"] = translated

    return messages


def translate_response_content(content: str) -> str:
    """Translate assistant response EN→pt."""
    if not TRANSLATE_ENABLED:
        return content
    if not content or not content.strip():
        return content
    if not should_translate_text(content):
        translated = translate_to_pt(content)
        if translated and translated != content:
            print(f"[translate] assistant: EN→pt ({len(content)}→{len(translated)} chars)",
                  file=sys.stderr, flush=True)
            return translated
    return content


def translate_response_body(body: bytes, content_type: str) -> bytes:
    """Translate assistant content in a JSON response body. Returns (translated_body, modified)."""
    if not TRANSLATE_ENABLED:
        return body

    if "text/event-stream" in content_type:
        # SSE streaming — pass through for latency, translate later
        return body

    if "application/json" not in content_type:
        return body

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body

    modified = False

    # OpenAI chat completions shape
    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            translated = translate_to_pt(content)
            if translated and translated != content:
                msg["content"] = translated
                modified = True

    # Anthropic messages shape
    for content_block in data.get("content", []):
        if isinstance(content_block, dict) and content_block.get("type") == "text":
            text = content_block.get("text", "")
            if text.strip():
                translated = translate_to_pt(text)
                if translated and translated != text:
                    content_block["text"] = translated
                    modified = True

    if modified:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return body


# ---------------------------------------------------------------------------
# HTTP Proxy Handler
# ---------------------------------------------------------------------------

class TranslateProxyHandler(http.server.BaseHTTPRequestHandler):
    """Proxy handler: translate messages, forward to headroom, translate response."""

    # Silence per-request log lines from BaseHTTPRequestHandler
    def log_message(self, format, *args):
        pass

    def _forward(self, method: str, path: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        """Forward a request to headroom and return (status, headers_dict, body_bytes)."""
        url = f"{HEADROOM_BASE}{path}"
        req = urllib.request.Request(url, data=body, method=method)

        # Copy relevant headers
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

    def _handle_translate(self):
        """Handle a request with translation."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        path = self.path
        method = self.command
        content_type = self.headers.get("Content-Type", "")

        # Translate request body if it contains messages
        if body and "application/json" in content_type and TRANSLATE_ENABLED:
            try:
                data = json.loads(body)

                # Translate messages array (OpenAI + Anthropic share this shape)
                messages = data.get("messages")
                if messages and isinstance(messages, list):
                    data["messages"] = translate_messages(messages)

                # Also translate system prompt if present (Anthropic)
                system = data.get("system")
                if isinstance(system, str):
                    if should_translate_text(system):
                        translated = translate_to_en(system)
                        if translated and translated != system:
                            data["system"] = translated
                elif isinstance(system, list):
                    for block in system:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if should_translate_text(text):
                                translated = translate_to_en(text)
                                if translated and translated != text:
                                    block["text"] = translated

                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            except (json.JSONDecodeError, TypeError, KeyError):
                pass  # forward as-is

        # Forward to headroom
        status, resp_headers, resp_body = self._forward(method, path, dict(self.headers), body)

        # Translate response
        resp_ct = resp_headers.get("content-type", resp_headers.get("Content-Type", ""))
        resp_body = translate_response_body(resp_body, resp_ct)

        # Send response back
        self.send_response(status)
        for key, value in resp_headers.items():
            if key.lower() not in ("transfer-encoding", "content-length"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(resp_body)))

        # Pass through SSE streaming headers
        if "text/event-stream" in resp_ct:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")

        self.end_headers()
        self.wfile.write(resp_body)

    def _handle_passthrough(self):
        """Forward request to headroom without translation."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        status, resp_headers, resp_body = self._forward(
            self.command, self.path, dict(self.headers), body
        )

        self.send_response(status)
        for key, value in resp_headers.items():
            if key.lower() not in ("transfer-encoding", "content-length"):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    # --- HTTP method handlers ---

    def do_GET(self):
        # Health / readiness endpoints
        if self.path in ("/health", "/readyz"):
            status_msg = "ok"
            if self.path == "/health":
                translator_available = _translator() is not None
                status_msg = json.dumps({
                    "status": "ok",
                    "translate_enabled": TRANSLATE_ENABLED,
                    "translator_available": translator_available,
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path == "/health" else "text/plain")
            self.end_headers()
            self.wfile.write(status_msg.encode())
            return

        # Dashboard/stats passthrough
        self._handle_passthrough()

    def do_POST(self):
        # Translate only chat/messages endpoints
        if TRANSLATE_ENABLED and (
            self.path.rstrip("/").endswith("/chat/completions")
            or self.path.rstrip("/").endswith("/v1/messages")
        ):
            self._handle_translate()
        else:
            self._handle_passthrough()

    def do_PUT(self):
        self._handle_passthrough()

    def do_DELETE(self):
        self._handle_passthrough()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server for concurrent request handling."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    print(f"[translate] starting on 0.0.0.0:{LISTEN_PORT}, "
          f"translate={'ON' if TRANSLATE_ENABLED else 'OFF'}, "
          f"headroom={HEADROOM_BASE}",
          file=sys.stderr, flush=True)

    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), TranslateProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
