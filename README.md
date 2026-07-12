# token-saver-wrappers

Token-saving wrappers for AI coding harnesses. `pi-token-saver`,
`hermes-token-saver`, `claude-token-saver`, `codex-token-saver`, and
`opencode-token-saver` behave exactly like `pi`, `hermes`, `claude` (Claude
Code), `codex` (OpenAI Codex), and `opencode`, except every LLM API request
passes through a local
[headroom](https://github.com/chopratejas/headroom) compression proxy —
**without touching any tool's real configuration**. Headroom compresses tool
outputs / context before they reach the provider (and keeps a local reversible
cache), typically cutting token spend significantly.

The exact headroom version used is vendored as a source tarball in
`vendor/headroom-src.tar.gz` (commit in `vendor/HEADROOM_COMMIT`) and built
into a local container image.

## Install

```sh
./build-and-install
```

This builds `localhost/headroom-token-saver:<commit>` from the vendored
tarball (first build compiles Rust — expect 10–30+ minutes), pulls the pinned
mitmproxy sidecar image, installs runtime libs to `~/.local/share/token-saver/`
and the commands `pi-token-saver`, `hermes-token-saver`, `token-saver-ctl` to
`~/.local/bin/`.

Requirements: `podman`, `python3`, `curl`, and whichever of the wrapped
commands you use (`pi`, `hermes`, `claude`, `codex`, `opencode`).
- **Linux:** rootless podman with `uidmap` installed (`sudo apt-get install -y
  uidmap`). On ZFS, also `fuse-overlayfs`. `build-and-install` checks for these.
- **macOS:** podman via Homebrew. `build-and-install` creates and starts a
  `podman machine` VM automatically if one isn't running. Behind a
  TLS-intercepting corporate proxy, it also prefetches the ONNX Runtime and
  trusts the corporate CA in the build (see below).

### Corporate TLS-intercepting proxies

Some corporate networks MITM outbound HTTPS. This breaks two
build-time downloads that don't use the system trust store: rustup's installer
and `ort`'s ONNX Runtime fetch. On macOS `build-and-install` handles both
automatically — it trusts the corporate CA (from the System keychain) inside
the build container and prefetches the ONNX Runtime on the host (which already
trusts the CA). On Linux behind such a proxy, pass `TOKEN_SAVER_CORP_CA=<pem>`
and `TOKEN_SAVER_ORT_PREFETCH=1`. Runtime LLM traffic is unaffected as long as
the provider endpoint isn't itself MITM'd.

## How it works

Every wrapper ensures a shared podman pod (`token-saver`) is running, then execs
the real command with environment-only overrides. The pod stays up after the
session ends (stop it with `token-saver-ctl stop`).

```
pod "token-saver"
├── headroom   127.0.0.1:8787  (built from vendor/headroom-src.tar.gz)
├── proxy      127.0.0.1:8786  (modular token reduction pipeline)
└── mitm       127.0.0.1:8790  (mitmproxy sidecar)
```

### Modular token reduction pipeline

The proxy container runs a **pluggable pipeline** of token reduction modules.
Each module is independently toggleable via `FEATURE_*` env vars. When the proxy
is enabled (`TOKEN_SAVER_TRANSLATE_ENABLED=1`), these modules are active by
default (except the heavy ones: summarizer, cache, router).

```
tool → mitm → proxy pipeline → headroom (compress) → upstream
                │
                ├── REQUEST pipeline:
                │   1. prompt_cache    SHA256(body) → stored response (off by default)
                │   2. summarize       collapse old history into summary (off by default)
                │   3. strip_system    dedup repeated instructions in system prompt
                │   4. minify_tools    strip descriptions/defaults from tool JSON Schema
                │   5. router          cheap model for simple queries (off by default)
                │   6. translate       pt-BR → EN
                │
                └── RESPONSE pipeline:
                    1. translate       EN → pt-BR
                    2. strip_response  remove unused metadata fields
                    3. prompt_cache    store response for future reuse
```

| Module | Default | Env var | What it does |
|---|---|---|---|
| `translate` | ON | `TOKEN_SAVER_TRANSLATE_ENABLED=1` | pt-BR ↔ EN via Google Translate |
| `strip_response` | ON | `FEATURE_STRIP_RESPONSE=1` | Remove `logprobs`, `system_fingerprint`, `usage` etc from responses |
| `strip_system` | ON | `FEATURE_STRIP_SYSTEM=1` | Dedup repeated "You MUST" / "Do not" blocks in system prompts |
| `minify_tools` | ON | `FEATURE_MINIFY_TOOLS=1` | Strip `description`, `default`, `examples` from tool JSON Schema |
| `summarize` | OFF | `FEATURE_SUMMARIZE=1` | When history > 15K chars: collapse old messages into a 2-line summary |
| `prompt_cache` | OFF | `FEATURE_PROMPT_CACHE=1` | Deterministic SHA256 cache — identical requests return instantly |
| `router` | OFF | `FEATURE_ROUTER=1` | Classify query complexity → cheap model for simple tasks |

**Enable the full pipeline:**
```sh
export TOKEN_SAVER_TRANSLATE_ENABLED=1
export FEATURE_SUMMARIZE=1
export FEATURE_PROMPT_CACHE=1
export FEATURE_ROUTER=1
hermes-token-saver "faça uma code review do arquivo X"
```

**Disable specific modules:**
```sh
export FEATURE_TRANSLATE=0        # disable translation only
export FEATURE_MINIFY_TOOLS=0     # keep tool descriptions
```

### Visibility

Every proxied response includes a header listing active modules:
```
X-Token-Saver-Modules: translate, strip_system, minify_tools, strip_response
```

**Dashboard:** `http://127.0.0.1:<proxy_port>/dashboard` — live HTML with:
- Active modules (green/red pills)
- Total requests, estimated tokens saved, cache hit rate
- Per-module activation counters

**Stats JSON:** `http://127.0.0.1:<proxy_port>/stats`

To check in hermes what's active:
```sh
curl -s http://127.0.0.1:8786/health | python3 -m json.tool
```

The `pi`, `hermes`, and `opencode` wrappers use the **same mechanism**: a
mitmproxy sidecar intercepts the known LLM API hosts and rewrites OpenAI-style
`…/chat/completions` requests to headroom, which compresses and forwards to the
real upstream — preserving your API key and any custom auth headers. (The
`claude` and `codex` wrappers instead redirect via a shadow config dir; see
their sections below.) The mitm approach is provider-agnostic: it works with
built-in providers (deepseek, openrouter, …) and with **custom endpoints**
(e.g. an internal GenAI platform configured via `baseUrl`/`base_url`) alike.

1. On each launch the wrapper regenerates an intercept-host list from the
   built-in openai-completions provider endpoints plus the hosts found in the
   tool's own config — `~/.pi/agent/models.json` for pi, `~/.hermes/config.yaml`
   for hermes, and opencode's `opencode.json`/`opencode.jsonc`
   (`provider.<name>.options.baseURL`) for opencode (loopback hosts excluded).
2. mitmproxy TLS-intercepts **only** those hosts; every other host is tunneled
   untouched (no TLS termination). The locally generated mitm CA is trusted per
   process via env only — nothing is installed system-wide.
3. The addon rewrites `…/chat/completions` (any base path) to headroom,
   carrying the real upstream in `x-headroom-base-url` / `x-headroom-original-
   path` headers, so one headroom instance serves any number of providers.
   Non-LLM paths on intercepted hosts (e.g. `/models`) pass through unmodified.

### pi-token-saver

Sets `HTTP(S)_PROXY` to the mitm sidecar and `NODE_EXTRA_CA_CERTS` to the mitm
CA (Node appends it to the system roots). pi routes all provider SDK traffic
through the global undici proxy dispatcher, so this covers every
openai-completions provider and pi's spawned subagents.

### hermes-token-saver

Sets `HTTP(S)_PROXY`/`ALL_PROXY` to the mitm sidecar. hermes builds an SSL
context from `HERMES_CA_BUNDLE`/`SSL_CERT_FILE` that *replaces* the system
roots, so the wrapper points those (and `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`)
at a combined bundle = system roots + any corporate bundle + mitm CA, sourced
from hermes's own `certifi`. This covers the main model client, moa
members/aggregator, and auxiliary calls in one shot.

Some hermes builds behind a corporate proxy pin `SSL_CERT_FILE` to their own
corporate CA bundle in `$HERMES_HOME/.env`, which is loaded *after* our
environment and so overrides it. When the wrapper detects such a pinned bundle
it adds the mitm CA into that
file inside a clearly-marked, idempotent block — the only way to reach hermes's
default clients — never removing the user's certs. `token-saver-ctl destroy`
removes that block again.

Caveats:
- `HTTP(S)_PROXY` is inherited by subprocesses the tools spawn (that's what
  makes their own subagents route correctly), so a `curl https://…` run inside
  a bash tool is also intercepted and would need `--cacert` to verify.
- Endpoints on `localhost`/`127.0.0.1` are intentionally **not** intercepted
  (excluded from the host list and via `NO_PROXY`); a local model server is
  used directly, uncompressed.

### opencode-token-saver

opencode is a Bun binary whose provider requests go through Bun's global
`fetch`, which honors `HTTP(S)_PROXY` and `NODE_EXTRA_CA_CERTS`. So — exactly
like pi — the wrapper sets `HTTP(S)_PROXY`/`ALL_PROXY` to the mitm sidecar and
`NODE_EXTRA_CA_CERTS` to the mitm CA, and the sidecar rewrites
`…/chat/completions` to headroom. This covers **custom openai-compatible
providers** (`provider.<name>` with `npm: "@ai-sdk/openai-compatible"` and an
`options.baseURL`, e.g. an internal GenAI platform) and any built-in
openai-completions provider. No config is rewritten, so a plain `opencode` is
unaffected and this works from any directory.

The wrapper finds the opencode binary on `PATH` or at
`~/.opencode/bin/opencode` (its installer's location), and regenerates the
intercept-host list from opencode's merged config: the global
`opencode.json`/`opencode.jsonc`, any `$OPENCODE_CONFIG`, and project-level
`./opencode.json(c)` and `./.opencode/opencode.json(c)`.

Note: only providers speaking the OpenAI `…/chat/completions` dialect are
compressed. opencode's built-in `openai` provider (Responses API) and
`anthropic` provider (`/v1/messages`) are tunneled untouched — matching the
scope of the pi wrapper.

### claude-token-saver

Claude Code applies its `settings.json` `env` block over the process
environment, so a plain `ANTHROPIC_BASE_URL` env var can't override a
`settings.json` that pins a custom endpoint. Like codex, the wrapper builds a
**shadow config dir** (via `CLAUDE_CONFIG_DIR`): it symlinks all real state
(and copies `.claude.json`), and replaces only `settings.json` with a copy
whose `env.ANTHROPIC_BASE_URL` points at headroom — preserving custom headers,
model mappings, `apiKeyHelper`, and hooks. The real `~/.claude` is never
modified. Headroom forwards `/v1/messages` to the real upstream, which the
wrapper reads from the real settings (`api.anthropic.com`, or a custom GenAI
platform) and sets as the pod's Anthropic target — recreating the pod if that
target changed (the Anthropic dialect has no per-request upstream override).
The API key / OAuth token / `apiKeyHelper` output passes through untouched.

Caveat (shared with codex): running from your home directory makes Claude read
the real `~/.claude/settings.json` as *project-level* config, which overrides
the shadow user settings and bypasses the proxy. Run from a project directory
(the normal case).

### codex-token-saver

Codex only honors a base-URL override from `config.toml`, not env vars. Editing
the real `~/.codex` would also redirect a plain `codex`, so the wrapper builds a
**shadow config dir** (via `CODEX_HOME`): every entry of the real dir is
symlinked — history, sessions, auth, memories, skills are all preserved and
written back — and only `config.toml` is replaced with a transformed copy
(`lib/codex_shadow_config.py`). The real `~/.codex` is never modified. Two
shapes are handled:

- **Built-in openai / ChatGPT subscription:** set the top-level
  `openai_base_url` to headroom. Headroom's `/backend-api/codex/*` routes handle
  the ChatGPT backend.
- **Custom provider** (`model_provider = "…"` with its own `base_url`, e.g. an
  internal GenAI platform on the Responses API): point that provider's
  `base_url` at headroom and inject `x-headroom-base-url` /
  `x-headroom-original-path` headers so headroom forwards to the real endpoint
  at the exact path, preserving the provider's auth (`env_key`) and headers.

## Commands

```sh
pi-token-saver       [any pi args...]
hermes-token-saver   [any hermes args...]
claude-token-saver   [any claude args...]
codex-token-saver    [any codex args...]
opencode-token-saver [any opencode args...]
token-saver-ctl      status|start|stop|restart|destroy|logs [mitm]|stats [--full-raw-json]
```

`token-saver-ctl stats` prints a readable token-savings summary (add
`--full-raw-json` for the raw payload). The headroom dashboard is at
http://127.0.0.1:<port>/dashboard while the pod runs (the port is shown by
`token-saver-ctl status`).

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN_SAVER_HOME` | `~/.local/share/token-saver` | state dir (CA, workspace, libs) |
| `TOKEN_SAVER_POD_NAME` | `token-saver` | podman pod name |
| `TOKEN_SAVER_HEADROOM_PORT` | `8787` | headroom host port (127.0.0.1) |
| `TOKEN_SAVER_MITM_PORT` | `8790` | mitm sidecar host port (127.0.0.1) |
| `TOKEN_SAVER_TRANSLATE_PORT` | `8786` | proxy host port (127.0.0.1) |
| `TOKEN_SAVER_TRANSLATE_ENABLED` | `0` | enable token reduction pipeline (set to `1`) |
| `TOKEN_SAVER_TRANSLATE_IMAGE` | `localhost/translate-token-saver:latest` | proxy container image |
| `TOKEN_SAVER_FEATURE_STRIP_RESPONSE` | `1` | strip unused metadata from responses |
| `TOKEN_SAVER_FEATURE_STRIP_SYSTEM` | `1` | dedup repeated system prompt blocks |
| `TOKEN_SAVER_FEATURE_MINIFY_TOOLS` | `1` | strip tool JSON Schema descriptions |
| `TOKEN_SAVER_FEATURE_SUMMARIZE` | `0` | collapse long conversation history |
| `TOKEN_SAVER_FEATURE_PROMPT_CACHE` | `0` | deterministic SHA256 response cache |
| `TOKEN_SAVER_FEATURE_ROUTER` | `0` | route simple queries to cheaper model |
| `TOKEN_SAVER_SUMMARIZE_MODEL` | `deepseek-chat` | model for conversation summarization |
| `TOKEN_SAVER_ROUTER_CHEAP_MODEL` | `deepseek-chat` | model for simple queries |
| `TOKEN_SAVER_OPENAI_UPSTREAM` | `https://api.deepseek.com` | fallback upstream for OpenAI requests that lack an `x-headroom-base-url` header (normal traffic always carries one) |
| `TOKEN_SAVER_PI_BIN` / `TOKEN_SAVER_HERMES_BIN` / `TOKEN_SAVER_OPENCODE_BIN` | from `PATH` (opencode also falls back to `~/.opencode/bin/opencode`) | real binary to exec |

Port/upstream changes take effect on pod (re)creation: `token-saver-ctl destroy`
then rerun a wrapper.

## Tests

```sh
tests/run-tests.sh
```

Fully isolated (own pod name, ports, temp `TOKEN_SAVER_HOME`, temp
`PI_CODING_AGENT_DIR`/`HERMES_HOME`); never touches your real config; spends no
real tokens — a mock OpenAI-compatible upstream on the host is reached from the
pod via `host.containers.internal`.

## Updating the vendored headroom

```sh
git -C ~/idm/headroom rev-parse HEAD > vendor/HEADROOM_COMMIT
git -C ~/idm/headroom archive --format=tar.gz --prefix=headroom/ \
    -o vendor/headroom-src.tar.gz HEAD
./build-and-install   # builds the new image tag
```

The built-in pi provider host list in `lib/gen_intercept_hosts.py` was
extracted from pi's `packages/ai/src/providers/*.ts` (api ==
"openai-completions"); refresh it when pi adds providers.
