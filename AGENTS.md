# AGENTS.md — working notes for this repo

Guidance for AI agents (and humans) modifying **token-saver-wrappers**. Read
this before touching wrappers, the proxy pod, or the tests. `README.md` is the
user-facing doc; this file is the maintainer's map plus the non-obvious traps
that already cost real debugging time.

## What this repo is

Thin wrapper commands (`pi-token-saver`, `hermes-token-saver`,
`claude-token-saver`, `codex-token-saver`, `opencode-token-saver`) that run the
real AI-coding CLI but route its LLM API traffic through a locally-built
[headroom](https://github.com/chopratejas/headroom) compression proxy running in
a podman pod — **without editing the tool's real config**. `token-saver-ctl`
manages the pod. `build-and-install` builds the vendored headroom image and
installs everything to `~/.local/bin` + `~/.local/share/token-saver`.

Layout:
- `bin/` — the wrapper scripts + `token-saver-ctl`.
- `lib/` — shared bash (`common.sh`) and Python helpers, installed to
  `~/.local/share/token-saver/lib`. Wrappers `source` `common.sh` from there.
- `tests/` — isolated end-to-end + unit tests (`run-tests.sh`); a mock upstream
  stands in for real providers, so tests spend **no tokens**.
- `vendor/` — pinned headroom source tarball (`headroom-src.tar.gz`) +
  `HEADROOM_COMMIT`. **Do not modify the tarball** — it is an upstream artifact
  reproduced verbatim. (It contains its own vendor strings; leave them.)

## The pod

```
pod "token-saver"
├── headroom   127.0.0.1:<TS_HEADROOM_PORT>  (built from vendor/headroom-src.tar.gz)
└── mitm       127.0.0.1:<TS_MITM_PORT>      (mitmproxy sidecar; only for mitm wrappers)
```

headroom endpoints that matter: `/v1/chat/completions` (OpenAI), `/v1/responses`
(Codex/Responses), `/v1/messages` (Anthropic), `/backend-api/codex/*` (ChatGPT),
`/stats`, `/stats-history`, `/readyz`, `/dashboard`.

**Per-request upstream override:** headroom reads `x-headroom-base-url` (origin)
+ `x-headroom-original-path` (full path) and forwards there. This works for the
**OpenAI and Responses** handlers, so one headroom instance serves any number of
providers. It does **NOT** work for the **Anthropic** handler, which forwards to
a global `ANTHROPIC_TARGET_API_URL` fixed at pod creation — hence the claude
wrapper must recreate the pod when the Anthropic upstream changes.

## Two routing mechanisms — pick the right one

**1. mitm sidecar (pi, hermes, opencode).** Used when the tool honors
`HTTP(S)_PROXY` + a custom CA env var. The wrapper sets the proxy to the mitm
sidecar and points the tool's CA trust at the locally-generated mitm CA
(`NODE_EXTRA_CA_CERTS` for Node/Bun; `SSL_CERT_FILE`/`*_CA_BUNDLE` for
Python/httpx). mitmproxy TLS-intercepts **only** the hosts in
`intercept-hosts.txt` (regenerated per launch by `ts_refresh_intercept_hosts` →
`gen_intercept_hosts.py`); everything else is tunneled untouched. The addon
(`mitm_addon.py`) rewrites `…/chat/completions` → headroom, injecting the
`x-headroom-*` headers from the live request. **Provider-agnostic and captures
the upstream live** — no need to know endpoints ahead of time. Only the
`/chat/completions` dialect is rewritten; Responses/Anthropic on intercepted
hosts pass through.

**2. shadow config dir (claude, codex).** Used when the tool takes its endpoint
only from a config file, not env. The wrapper builds a shadow config dir
(`CLAUDE_CONFIG_DIR` / `CODEX_HOME`), symlinks all real state, and replaces only
the one config file with a transformed copy that points the base URL at headroom
(+ injects `x-headroom-*` for custom providers). The real dir is never touched.
Because claude/codex read config as *project-level* when run from `$HOME`, these
wrappers call `ts_warn_if_home` (the mitm wrappers don't need it — env routing
is cwd-independent).

When adding a new tool, prefer mechanism 1 if it respects `HTTP(S)_PROXY` +
a CA env var; fall back to mechanism 2 otherwise.

## Non-obvious traps (each of these already bit us)

- **opencode `run` blocks on stdin.** Non-interactive `opencode run "msg"` still
  reads stdin; with a TTY/pipe attached it hangs forever with no output. Always
  redirect `</dev/null` in tests/automation. (The interactive wrapper path is
  fine — this only affects headless invocation.)
- **opencode hard-errors on a provider missing from `enabled_providers`.**
  Released opencode (seen on 1.17.10/macOS) throws a generic "Unexpected server
  error" at server bootstrap — *before any LLM call, so the upstream is never
  hit* — when you select a model whose provider isn't listed in the merged
  config's `enabled_providers`. A synthetic test provider therefore needs an
  explicit `"enabled_providers": ["<name>"]`. This cost real debugging time: the
  same crash appears identical to (and was initially mistaken for) a
  proxy/CA/TLS failure. When opencode errors with a mock and the mock log is
  empty, suspect config-shape/enabled-providers before the routing layer.
- **opencode `run` kills its own process group on exit** (macOS), reaping a
  parent test script mid-run. The real wrapper is immune because it `exec`s
  opencode (no parent to reap); ad-hoc probe scripts must launch it in a fresh
  session (`python3 -c 'import os,subprocess,sys;os.setsid();...'`) or read
  result files written *before* opencode exits.
- **opencode uses the SAME mitm mechanism on Linux and macOS** — do not add
  OS-specific branches. An earlier belief that the proxy env destabilised
  opencode on macOS was wrong: it was the `enabled_providers` crash above, seen
  only because the test provider wasn't enabled. Verified working on macOS with a
  real provider (traffic counted by headroom `/stats`).
- **Never override `XDG_DATA_HOME`/`XDG_CONFIG_HOME` to isolate opencode.**
  Rootless **podman** stores images/containers under those same vars, so the
  override hides the headroom image ("image not found"). Isolate opencode with
  `OPENCODE_CONFIG=<file>` (config) + `OPENCODE_DB=:memory:` (session db)
  instead — see `tests/test-opencode-token-saver.sh`.
- **`set -e` + helper functions.** A shell function whose last command is a
  short-circuited `&&` returns non-zero, and as a bare call that aborts the
  script. E.g. `oc_add() { [ -f "$1" ] && arr+=("$1"); }` kills the wrapper on
  the first missing file. End such helpers with `; return 0`.
- **JSONC comment stripping must be string-aware.** A naive `//…` strip turns
  `"https://host"` into `"https:` and drops the host. `gen_intercept_hosts.py`
  tokenizes strings first (`_JSONC_TOKEN`) so `//` inside a URL survives.
- **undici/Bun keep-alive stalls process exit.** Node/Bun clients hold the
  idle keep-alive socket to the proxy open for minutes, keeping the event loop
  alive so a one-shot run never exits. The mitm addon sends
  `Connection: close` on every response to force sockets closed.
- **Empty-array expansion under `set -u`** (macOS bash 3.2): use
  `${arr[@]+"${arr[@]}"}`, not `"${arr[@]}"`.
- **hermes pins its own CA.** Some hermes builds set `SSL_CERT_FILE` in
  `$HERMES_HOME/.env`, loaded *after* our env, overriding it. `common.sh`
  injects the mitm CA into that pinned bundle inside a marked idempotent block
  (`ts_ensure_hermes_ca_trust`), removed by `token-saver-ctl destroy`.
- **Ports auto-select.** If 8787/8790 are busy (e.g. the user runs their own
  headroom), `ts_pick_free_ports` picks a free pair and writes `ports.env`.
  Don't hard-code ports; read `TS_HEADROOM_PORT`/`TS_MITM_PORT`.

## macOS specifics

- podman runs in a VM that bind-mounts **only the user's home dir** — mounts
  under `/tmp` or `/var/folders` fail. Tests put `TOKEN_SAVER_HOME` under
  `$HOME` for this reason.
- With `--userns=keep-id`, `HOME` inside the container is `/` and the container
  user cannot traverse the `0700 /home/<vm-user>`; mounting confdirs at a
  **top-level** path (e.g. `/mitm-confdir`, `/headroom-data`) plus setting
  `HOME`/confdir env avoids the CA-write `EACCES`.
- Behind a **TLS-intercepting corporate proxy**, two build-time downloads use
  their own trust store and break: rustup's installer and `ort`'s ONNX Runtime
  fetch. `build-and-install` handles both on macOS — `prefetch_ort()` downloads
  the ONNX lib on the host (which trusts the proxy CA) and links it offline via
  `ORT_LIB_LOCATION`; `resolve_corp_ca()`/`inject_corp_ca()` export the admin
  roots from the System keychain (vendor-neutral — do not re-add a vendor-name
  filter) and trust them inside the build. On Linux behind such a proxy, pass
  `TOKEN_SAVER_CORP_CA=<pem>` + `TOKEN_SAVER_ORT_PREFETCH=1`.

## Testing

`tests/run-tests.sh` runs everything sequentially (shared test pod name/ports).
Each test sources `tests/lib.sh`, which builds a fully isolated environment
(own `TOKEN_SAVER_HOME` under `$HOME`, own pod name, own ports) and starts
`mock_upstream.py` — a stand-in that speaks OpenAI chat, Anthropic Messages, and
the OpenAI Responses item-lifecycle SSE. A test's success proves real transit
because the mock is reachable only via a container-internal hostname
(`host.containers.internal`); if the tool bypassed the proxy it couldn't even
resolve it.

To add a wrapper test: copy the closest existing `test-*-token-saver.sh`,
configure a custom provider pointing at `$MOCK_HOST`, assert the reply contains
`$MOCK_MARKER`, the mock log shows the API key, and the intercept-hosts file
(mitm wrappers) or shadow config (config wrappers) is correct. Register it in
`run-tests.sh`. Requires the headroom image already built (`./build-and-install`
once).

## Checklist: adding a new wrapper

1. `bin/<tool>-token-saver` — source `common.sh`, resolve the real binary
   (env override + `PATH` + known fallback path), set up routing (mechanism 1
   or 2), `ts_ensure_pod [with-mitm]`, `exec`.
2. If mechanism 1: extend `gen_intercept_hosts.py` to parse the tool's config
   for provider hosts (add a dispatch branch keyed on filename/extension).
3. Add a `tests/test-<tool>-token-saver.sh` and register it in `run-tests.sh`;
   add unit coverage to `test-hosts-gen.sh` if you touched the host parser.
4. Add the binary to the `install` list and the "Commands available" line in
   `build-and-install`.
5. Document it in `README.md` (intro list, requirements, a `### ` section,
   Commands, the env-var table).

## Conventions

- No references to specific corporate vendors (proxy vendors, employers, internal
  tool names) anywhere in code or docs — keep everything generic. This repo is
  not official from any such party.
- `vendor/headroom-src.tar.gz` is upstream and stays byte-for-byte as vendored.
- Commit messages end with the project's `Co-Authored-By` trailer; branch off
  `main` before committing unless told otherwise.
