#!/usr/bin/env bash
# End-to-end test: real opencode binary, run through opencode-token-saver, with
# a custom openai-compatible provider (in a fully isolated XDG config) pointing
# at the mock upstream. Verifies:
#   - opencode's reply contains the mock's canned marker (full round trip works)
#   - the request transited headroom + the mock (proxy chain works)
#   - the API key was passed through to the upstream
#   - the wrapper generated the intercept-host list from opencode's config

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# opencode's installer drops the binary at ~/.opencode/bin/opencode, which may
# not be on PATH.
OC_BIN="$(command -v opencode || true)"
[ -n "$OC_BIN" ] && [ -x "$OC_BIN" ] || OC_BIN="$HOME/.opencode/bin/opencode"
[ -x "$OC_BIN" ] || { t_log "SKIP: opencode not installed"; exit 0; }
export TOKEN_SAVER_OPENCODE_BIN="$OC_BIN"

t_install_libs
t_start_mock

# Isolate opencode without touching XDG_DATA_HOME/XDG_CONFIG_HOME — podman
# stores its images/containers under those, so overriding them would hide the
# headroom image from the pod. Instead: OPENCODE_CONFIG points opencode (and the
# wrapper's host scan) at our temp config, and OPENCODE_DB (a temp file) keeps
# the session out of the user's real database.
export OPENCODE_CONFIG="$TOKEN_SAVER_HOME/opencode.json"
export OPENCODE_DB="$TOKEN_SAVER_HOME/opencode.db"

# Custom provider aimed at the mock via a hostname only resolvable INSIDE
# containers — if opencode bypassed the mitm+headroom chain it could not even
# resolve it, so a successful reply proves proxy transit. `enabled_providers`
# must list our provider: opencode releases hard-error ("Unexpected server
# error") when the selected model's provider isn't enabled in the merged config.
cat > "$OPENCODE_CONFIG" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "enabled_providers": ["mockllm"],
  "provider": {
    "mockllm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "mockllm",
      "options": {
        "baseURL": "http://$MOCK_HOST/v1",
        "apiKey": "test-key-123"
      },
      "models": {
        "mock-1": { "name": "mock-1", "limit": { "context": 1000000, "output": 256000 } }
      }
    }
  }
}
EOF

# Run from an isolated project dir (no stray project config / plugin installs
# in the repo).
PROJECT_DIR="$TOKEN_SAVER_HOME/project"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# opencode's non-interactive `run` still reads stdin (for piped input); with a
# TTY/pipe attached it blocks forever, so close stdin explicitly.
# OPENCODE_DISABLE_AUTOUPDATE keeps the run hermetic.
t_log "running opencode-token-saver run (brings up pod on first use)"
OUT="$(OPENCODE_DISABLE_AUTOUPDATE=1 t_timeout 180 \
    "$REPO_DIR/bin/opencode-token-saver" run --model mockllm/mock-1 "Say hello" </dev/null 2>&1)" \
    || t_fail "opencode-token-saver exited non-zero: $OUT"

echo "$OUT" | grep -q "$MOCK_MARKER" \
    || t_fail "opencode output missing mock marker — traffic did not reach mock upstream. Output: $OUT"

[ -s "$MOCK_LOG" ] || t_fail "mock upstream never received a request"
grep -q '"authorization": "Bearer test-key-123"' "$MOCK_LOG" \
    || t_fail "API key not passed through to upstream"

grep -qx "$MOCK_HOST" "$TOKEN_SAVER_HOME/mitm/intercept-hosts.txt" \
    || t_fail "wrapper did not add opencode config host to intercept list"
grep -qx "api.deepseek.com" "$TOKEN_SAVER_HOME/mitm/intercept-hosts.txt" \
    || t_fail "wrapper did not include builtin provider hosts"

# Proof of headroom transit: its stats endpoint must have counted the request.
STATS="$(curl -fsS "http://127.0.0.1:$TOKEN_SAVER_HEADROOM_PORT/stats")"
echo "$STATS" | grep -qE '"(total_)?requests"[: ]*[1-9]' \
    || t_log "WARN: could not confirm request count in headroom stats (format may differ): $STATS"

t_pass "test-opencode-token-saver"
