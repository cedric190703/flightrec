# next_path — where flightrec goes from here

M1–M6 (record, snapshot, proxy, correlate, view, fork) are done. This file is
the ordered backlog of what comes next. Each item says *why* it matters, what
it touches, and how we know it is done. Rough order is by value ÷ effort;
items within a tier are independent and can be picked up in any order.

Legend: `[S]` ≤ 1 day · `[M]` a few days · `[L]` a week or more.

---

## Tier 1 — make recordings trustworthy and safe to share

### 1.1 Secret redaction `[S]`
The proxy records request headers and bodies; `x-api-key`, `Authorization`
and bearer tokens in prompts land in `events.jsonl` in clear text. Nobody can
share a recording until this is fixed.
- `wire.py` / `proxy.py`: scrub known auth headers before `record()`; regex
  pass over payload strings for `sk-…`, `ghp_…`, AWS keys, JWTs.
- `flightrec run --redact PATTERN` for project-specific secrets.
- Done when: a session recorded with a real key contains no key material
  (`grep` test on the JSONL), and the viewer shows `«redacted»`.

### 1.2 Capture commands run by absolute path `[M]`
`/bin/sh -c …` bypasses the `PATH` shim, so harnesses that spawn shells by
absolute path (several do) leave holes in the exec stream.
- Linux: `LD_PRELOAD` shim on `execve`; macOS: `DYLD_INSERT_LIBRARIES` where
  SIP allows, otherwise fall back to `fs_usage`/`proc` polling.
- Alternative cheap win: watch `/proc/<pid>/task/*/children` (Linux) and
  `ps -o ppid` polling (macOS) for the harness's process tree and log argv.
- Done when: `flightrec run -- bash -c '/bin/sh -c "echo hi"'` records the
  inner command.

### 1.3 Snapshot the *whole* tree at session start `[M]`
`fork` can only rebuild files flightrec saw change; an untouched file that the
agent depends on is missing from the forked directory.
- Option A: `git stash`-style — if the cwd is a git repo, record `HEAD` +
  `git diff` at start, and let `fork` start from `git worktree add`.
- Option B: content-address the full tree at start (bounded by size, respecting
  ignores). Blobs dedupe, so repeated sessions on one repo are cheap.
- Done when: `flightrec fork` produces a directory that passes the project's
  test suite without manual copying.

### 1.4 Session retention & garbage collection `[S]`
`~/.flightrec/sessions` grows without bound; blobs are per-session so nothing
is shared.
- `flightrec gc --keep-last N --older-than 30d`; `flightrec rm <id>`.
- Move blobs to a root-level store (`~/.flightrec/blobs/`) with refcounts, or
  hard-link across sessions.
- Done when: `list` shows sizes and `gc` reclaims them.

---

## Tier 2 — see more, faster

### 2.1 Live tailing in the viewer `[M]`
Today you record, *then* view. Watching a session as it happens is the
killer demo and the main debugging workflow.
- `server.py`: SSE endpoint `/api/sessions/<id>/tail` that streams new events;
  `Session` gets a `follow()` generator.
- Viewer: auto-scroll timeline, "LIVE" badge, pause on scrub.
- Done when: `flightrec run` + `flightrec view` in a second terminal shows
  steps appearing within ~200 ms.

### 2.2 Search, filter and jump `[S]`
- Filter steps by kind / confidence / path / exit code ≠ 0 / `is_error`.
- Full-text search across tool inputs, results and assistant text.
- Keyboard: `/` to search, `n`/`p` next/prev match, `e` next error.
- Done when: finding "the step that broke the tests" takes one keystroke.

### 2.3 Cost and token accounting `[S]`
`cumulative_tokens` exists; turn it into money and cache efficiency.
- Per-model price table (overridable via `~/.flightrec/prices.json`).
- Show `$` per step and per session; cache-read/cache-write ratio; a
  sparkline of tokens over time.
- `flightrec list` gains a COST column; `flightrec show --steps` prints `$`.

### 2.4 Session diff `[M]`
Compare two recordings of the same task (e.g. two models, or before/after a
prompt change): steps side by side, files touched in one but not the other,
total tokens/cost/duration, first divergent step.
- `flightrec diff <a> <b>` (text) and `/diff/<a>/<b>` in the viewer.

### 2.5 Export `[S]`
- `flightrec export <id> --format markdown|json|har|junit`.
- Markdown is the shareable "what the agent did" report (superset of
  `FORK.md`); HAR lets people open the LLM traffic in browser devtools.

---

## Tier 3 — cover every harness and provider

### 3.1 More wire formats `[S each]`
`wire.py` parses Anthropic Messages and OpenAI Chat/Responses. Add:
- Google Gemini (`generateContent` / `streamGenerateContent`)
- AWS Bedrock (`invoke` / `converse`, event-stream framing)
- Ollama / llama.cpp (`/api/chat`, NDJSON streaming)
- Mistral, Cohere, xAI where they diverge from the OpenAI shape.
Each needs: a `detect_provider` rule, a parser, and a fixture-based test in
`tests/test_wire_*.py`. Route via `/to/<host>` already works for capture.

### 3.2 Native adapters `[M each]`
The proxy/fs/shim trio is the universal floor; native hooks add fidelity
(exact tool names, user prompts, subagent boundaries) when available.
- Claude Code: hooks (`PreToolUse`/`PostToolUse`/`Stop`) → `Source
  "adapter:claude"` events, correlated with the proxy stream by `tool_use_id`.
- Aider: parse `.aider.chat.history.md`.
- Codex / OpenCode: their session JSONL.
- Done when: a native-adapted session shows `strong` confidence on every
  tool step and user messages arrive even when the proxy is off.

### 3.3 Harnesses that pin TLS or ignore `*_BASE_URL` `[L]`
- Optional MITM mode with a locally generated CA (`flightrec ca install`),
  `HTTPS_PROXY` injection, explicit opt-in and loud warning.
- Document per-harness env vars that route traffic (`CLAUDE_CODE_…`,
  `OPENAI_BASE_URL`, `GEMINI_API_BASE`, …) in a `harnesses.md` matrix.

### 3.4 Windows support `[M]`
Shims are POSIX `sh`; generate `.cmd` wrappers and use `where` for
resolution. `fswatch` and the proxy already work. CI matrix gains
`windows-latest`.

---

## Tier 4 — replay and regression

### 4.1 Deterministic replay `[L]`
Re-run a harness against the *recorded* LLM responses instead of the live
API: the proxy answers from `events.jsonl` keyed by request hash.
- `flightrec replay <id> -- <cmd>`; mismatched requests fall through to the
  real API (and are recorded as a divergence).
- Turns any recording into a fixture: agent behaviour can be tested in CI
  without spending tokens.

### 4.2 Assertions on recordings `[M]`
`flightrec check <id> --no-exec-failures --max-cost 2.00 --touched-only src/`
returns non-zero on violation. Pairs with replay to make agent changes
gate-able in CI.

### 4.3 Annotations `[S]`
`flightrec note <id> --at <seq> "this is where it went wrong"` and inline
notes in the viewer (stored as `note` events, so the format doesn't change).

---

## Tier 5 — engineering hygiene

- **Packaging**: publish to PyPI; `pipx install flightrec`; `uv tool`.
  Homebrew formula once the CLI surface is stable.
- **Schema versioning**: `meta.json` gets `"format": 1`; `Event.from_json`
  already tolerates unknown fields — add a migration hook for breaking
  changes.
- **Type-checking & linting in CI**: `ruff` + `mypy --strict` on `src/`.
- **Property tests** for `build_steps` (Hypothesis: any event permutation
  yields monotonically indexed steps, every event id in at most one step).
- **Large-session performance**: `session_detail` serialises every event;
  paginate `/api/sessions/<id>/events` and stream `build_steps` for
  >50k-event sessions.
- **Viewer as a real package**: split `index.html` into modules with a tiny
  build step (esbuild), keep "single file, no runtime deps" as the artefact.

---

## Explicitly not planned

- Editing recordings in place — the log is append-only by design; use notes.
- A hosted service — flightrec is local-first; export covers sharing.
- Replacing a harness's own transcript/resume — `fork` is deliberately the
  universal, harness-neutral mechanism.
