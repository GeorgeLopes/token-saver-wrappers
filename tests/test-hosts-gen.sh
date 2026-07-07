#!/usr/bin/env bash
# Unit test for lib/gen_intercept_hosts.py — no containers needed.

set -euo pipefail
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$TESTS_DIR")"

t_fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/models.json" <<'EOF'
{
  "providers": {
    "myllm": {
      "baseUrl": "https://llm.example.com:8443/api/v1",
      "api": "openai-completions",
      "apiKey": "k",
      "models": [
        {"id": "m1"},
        {"id": "m2", "baseUrl": "http://other.example.org/v1"}
      ]
    },
    "local-ollama": {
      "baseUrl": "http://127.0.0.1:11434/v1",
      "api": "openai-completions",
      "apiKey": "k",
      "models": [{"id": "m3"}]
    }
  }
}
EOF

OUT="$(python3 "$REPO_DIR/lib/gen_intercept_hosts.py" "$TMP/models.json")"

echo "$OUT" | grep -qx "llm.example.com:8443" || t_fail "custom provider host:port missing"
echo "$OUT" | grep -qx "other.example.org"    || t_fail "per-model baseUrl host missing"
echo "$OUT" | grep -qx "api.deepseek.com"     || t_fail "builtin host missing"
echo "$OUT" | grep -q  "127.0.0.1"            && t_fail "loopback host must be excluded"

# Missing models.json → builtins only, no crash
OUT2="$(python3 "$REPO_DIR/lib/gen_intercept_hosts.py" "$TMP/nonexistent.json")"
echo "$OUT2" | grep -qx "api.deepseek.com" || t_fail "builtins missing when models.json absent"

# hermes-style YAML config: hosts extracted from any base_url URL.
cat > "$TMP/config.yaml" <<'EOF'
model:
  provider: custom
  base_url: https://genai.internal.example.com/api/v2
  api_mode: chat_completions
fallback_providers:
  - provider: custom
    base_url: https://fallback.example.net:9443/v1
EOF
OUT3="$(python3 "$REPO_DIR/lib/gen_intercept_hosts.py" "$TMP/config.yaml")"
echo "$OUT3" | grep -qx "genai.internal.example.com"  || t_fail "yaml base_url host missing"
echo "$OUT3" | grep -qx "fallback.example.net:9443"   || t_fail "yaml host:port missing"
echo "$OUT3" | grep -qx "api.deepseek.com"            || t_fail "builtins missing for yaml input"

# Both file types together.
OUT4="$(python3 "$REPO_DIR/lib/gen_intercept_hosts.py" "$TMP/models.json" "$TMP/config.yaml")"
echo "$OUT4" | grep -qx "llm.example.com:8443"       || t_fail "json host missing in combined run"
echo "$OUT4" | grep -qx "genai.internal.example.com" || t_fail "yaml host missing in combined run"

# opencode config (JSONC): provider.<name>.options.baseURL, comments and
# trailing commas tolerated; $schema URL and loopback hosts excluded. The `//`
# inside a URL value must NOT be treated as a comment.
mkdir -p "$TMP/oc"
cat > "$TMP/oc/opencode.jsonc" <<'EOF'
{
  // custom provider via an internal genai platform
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "genai": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://genai.example.internal:8443/v1" }, /* endpoint */
      "models": { "big-1": {} },
    },
    "ds": { "options": { "baseURL": "https://api.deepseek.com/v1" } },
    "local": { "options": { "baseURL": "http://127.0.0.1:4000/v1" } },
  },
}
EOF
OUT5="$(python3 "$REPO_DIR/lib/gen_intercept_hosts.py" "$TMP/oc/opencode.jsonc")"
echo "$OUT5" | grep -qx "genai.example.internal:8443" || t_fail "opencode custom host:port missing"
echo "$OUT5" | grep -qx "api.deepseek.com"            || t_fail "opencode built-in provider host missing"
echo "$OUT5" | grep -q  "opencode.ai"                 && t_fail "opencode \$schema host must be excluded"
echo "$OUT5" | grep -q  "127.0.0.1"                   && t_fail "opencode loopback host must be excluded"

printf '\033[1;32m[PASS]\033[0m test-hosts-gen\n' >&2
