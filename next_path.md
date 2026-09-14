# next_path — where flightrec goes from here

M1–M6 (record, snapshot, proxy, correlate, view, fork) are done, and the
recorder has since been hardened against damaged and untrusted input (see
"Recently landed"). This file is the ordered backlog of what comes next.

Every item states *why* it matters, what it **touches**, how it is **tested**,
and when it is **done**. Rough order inside a tier is value ÷ effort; items
within a tier are independent unless a "Needs" line says otherwise.

Legend: `[S]` ≤ 1 day · `[M]` a few days · `[L]` a week or more.

---

## Start here — the recommended next two

1. **1.2 Capture the full request** — the proxy currently records only
   `model`, `stream`, `n_messages` and tool *names*. The system prompt,
   sampling parameters and the request body itself are lost, which makes
   replay (4.1), session diff (2.4) and "why did it say that" impossible.
   Small change, unlocks a whole tier. (Now safe to do: 1.1 redaction has
   landed, so a captured body no longer reintroduces key material.)
2. **1.3 Capture command output** — `exec` events carry argv and exit code but
   not stdout/stderr. "Which step broke the tests" still requires a guess.

Both are `[S]`, touch different files, and can ship together.

---

## Tier 1 — make recordings complete and safe to share

### 1.2 Capture the full request `[S]`
- **Touches:** `wire.py` (`extract()` adds `system`, `temperature`,
  `max_tokens`, `tool_choice`, a `request_sha256` over the canonical body,
  and — behind `--capture-bodies` — the clipped raw body), `cli.py` flag,
  viewer request panel.
- **Design:** `request_sha256` is computed over the body with volatile fields
  (`stream`, `metadata.user_id`) removed, so identical turns hash the same
  across runs. This is the key 4.1 replay and 2.4 diff will use.
- **Tested:** fixture bodies for all three providers; hash stability across
  key order and the `stream` flag.
- **Done when:** `show` prints the system prompt (clipped) on `llm_request`
  and two recordings of the same turn share a `request_sha256`.
- **Needs:** 1.1 first, or the captured body reintroduces key material.

### 1.3 Capture command output `[S]`
- **Touches:** `shim.py` template (`tee` stdout/stderr to
  `$FLIGHTREC_EXEC_LOG.d/<id>.out`/`.err` while still passing through — the
  agent must see its output unchanged and unbuffered), `ExecCollector`
  (attach a bounded tail, default 64 KiB, as a blob), `timeline`/viewer/`show
  --steps` (render the tail under the command).
- **Constraints:** interactive commands (TTY) must not be broken — only tee
  when stdout is not a TTY; never buffer.
- **Tested:** a shimmed `bash -c 'echo out; echo err >&2; exit 3'` yields an
  exec end event with both tails and exit 3; a TTY check via `script -q`.
- **Done when:** `show --steps` prints the failing pytest summary under the
  `$ pytest` line.

### 1.4 Snapshot the *whole* tree at session start `[M]`
`fork` can only rebuild files flightrec saw change; an untouched file that the
agent depends on is missing from the forked directory.
- **Touches:** `fswatch._seed_index` already hashes every file — the blobs are
  in the store, only the *index* is not persisted. Write `tree.json`
  (`{path: sha}`) at `session_start`; `fork.materialize` starts from it.
- **Also:** if `cwd` is a git repo, record `HEAD` and `git status --porcelain`
  in `meta.json` so a fork can alternatively begin from `git worktree add`.
- **Tested:** fork a session recorded on a small repo with an untouched
  module; the fork's test suite passes without manual copying.
- **Done when:** that test is green.

### 1.5 Session retention and garbage collection `[S]`
`~/.flightrec/sessions` grows without bound; blobs are per-session so nothing
is shared (1.4 makes this worse: every session re-stores the whole tree).
- **Touches:** `store.py` (root-level `blobs/` with per-session hard links,
  or a refcount file), `cli.py` (`flightrec rm <id>`, `flightrec gc
  --keep-last N --older-than 30d --dry-run`), `list` gains a SIZE column.
- **Tested:** `gc` on a fixture home reclaims exactly the expected sessions
  and never removes a blob still referenced.
- **Done when:** `list` shows sizes and `gc --dry-run` matches what `gc` does.

### 1.6 Capture commands run by absolute path `[M]`
`/bin/sh -c …` bypasses the `PATH` shim, so harnesses that spawn shells by
absolute path leave holes in the exec stream.
- **Cheap win first:** poll the harness's process tree (`ps -o pid,ppid,args`
  on macOS, `/proc/<pid>/task/*/children` on Linux) every 100 ms and log new
  argv as `exec` events with `source: "ptrace"`-style confidence `timing`.
- **Later:** `LD_PRELOAD` shim on `execve` (Linux); `DYLD_INSERT_LIBRARIES`
  where SIP allows (macOS).
- **Done when:** `flightrec run -- bash -c '/bin/sh -c "echo hi"'` records
  the inner command.

---

## Tier 2 — see more, faster

### 2.1 Live tailing in the viewer `[M]`
Today you record, *then* view. Watching a session as it happens is the
killer demo and the main debugging workflow.
- **Touches:** `store.Session.follow()` (generator over new lines; reuse the
  partial-line logic from `ExecCollector.drain`), `server.py` SSE endpoint
  `/api/sessions/<id>/tail?since=<seq>`, viewer: auto-scroll, "LIVE" badge,
  pause on scrub, incremental `build_steps` (append-only recompute of the
  last open step only).
- **Tested:** append events to a session while an SSE client is connected;
  assert delivery order and latency < 200 ms; disconnect mid-stream must not
  leak a thread.
- **Done when:** `flightrec run` + `flightrec view` in a second terminal shows
  steps appearing within ~200 ms.

### 2.2 Search, filter and jump `[S]`
- Filter steps by kind / confidence / path / `exit_code ≠ 0` / `is_error`.
- Full-text search across tool inputs, results, command output (1.3) and
  assistant text; `flightrec show --grep PATTERN` for the terminal.
- Keyboard: `/` search, `n`/`p` next/prev match, `e` next error, `f` next
  file change.
- **Done when:** finding "the step that broke the tests" takes one keystroke.

### 2.3 Cost and token accounting `[S]`
`cumulative_tokens` exists; turn it into money and cache efficiency.
- **Touches:** new `prices.py` with a per-model table (overridable via
  `~/.flightrec/prices.json`), `timeline.Step.cost`, `list` COST column,
  `show --steps` prints `$`, viewer shows `$`/step, cache-read ratio and a
  token sparkline.
- **Tested:** table lookup with model aliases and unknown models (cost
  `None`, never a crash); cache tokens priced at their discounted rate.
- **Done when:** `list` shows a session's total cost.

### 2.4 Session diff `[M]`
Compare two recordings of the same task (two models, or before/after a prompt
change): steps side by side, files touched in one but not the other, total
tokens/cost/duration, first divergent step.
- **Needs:** 1.2 (`request_sha256` is how "same turn" is defined).
- `flightrec diff <a> <b>` (text) and `/diff/<a>/<b>` in the viewer.

### 2.5 Export `[S]`
- `flightrec export <id> --format markdown|json|har|junit`.
- Markdown is the shareable "what the agent did" report (superset of
  `FORK.md`); HAR lets people open the LLM traffic in browser devtools; JUnit
  turns exec failures into CI-readable results.
- **Needs:** 1.1 — export is the moment a recording leaves the machine.

### 2.6 Analysis: loops, waste and "where did it go wrong" `[M]`
- Detect repeated identical tool calls, edit→test→revert cycles, responses
  with zero side effects, and the first `is_error`/non-zero exit after which
  the session never recovered.
- Surface as `note` events (`source: "analysis"`) so the format is unchanged
  and the viewer shows them inline as a heatmap strip on the scrubber.

---

## Tier 3 — cover every harness and provider

### 3.1 More wire formats `[S each]`
`wire.py` parses Anthropic Messages and OpenAI Chat/Responses. Add, in this
order: Google Gemini (`generateContent` / `streamGenerateContent`), Ollama /
llama.cpp (`/api/chat`, NDJSON streaming), AWS Bedrock (`converse`,
event-stream framing), then Mistral/Cohere/xAI where they diverge from the
OpenAI shape. Each needs a `detect_provider` rule, a parser, and a
fixture-based test in `tests/test_wire_*.py`. Route via `/to/<host>` already
captures the raw traffic for all of them.

### 3.2 `flightrec doctor` `[S]`
Before native adapters: tell the user *why* a recording is empty.
- Checks: which `*_BASE_URL` vars the harness honours (a probe request
  through the proxy), whether the shell resolves to the shim dir, whether the
  watcher backend is native or polling, whether the cwd is under an ignore.
- **Done when:** running `doctor` on a harness that pins TLS says so.

### 3.3 Native adapters `[M each]`
The proxy/fs/shim trio is the universal floor; native hooks add fidelity
(exact tool names, user prompts, subagent boundaries) when available.
- Claude Code: hooks (`PreToolUse`/`PostToolUse`/`Stop`) → `source:
  "adapter:claude"` events, correlated with the proxy stream by `tool_use_id`.
- Aider: parse `.aider.chat.history.md`. Codex / OpenCode: session JSONL.
- **Done when:** a native-adapted session shows `strong` confidence on every
  tool step and user messages arrive even with `--no-proxy`.

### 3.4 Harnesses that pin TLS or ignore `*_BASE_URL` `[L]`
- Optional MITM mode with a locally generated CA (`flightrec ca install`),
  `HTTPS_PROXY` injection, explicit opt-in and a loud warning.
- Document per-harness routing env vars in `docs/harnesses.md`.

### 3.5 Windows support `[M]`
Shims are POSIX `sh`; generate `.cmd` wrappers and use `where` for
resolution. `fswatch` and the proxy already work. CI matrix gains
`windows-latest`.

---

## Tier 4 — replay and regression

### 4.1 Deterministic replay `[L]`
Re-run a harness against the *recorded* LLM responses instead of the live
API: the proxy answers from `events.jsonl` keyed by `request_sha256`.
- **Needs:** 1.2 (bodies + hash). Streamed responses are re-emitted as SSE
  with the original chunking so client code paths stay identical.
- `flightrec replay <id> -- <cmd>`; a request with no recorded match falls
  through to the real API and is recorded as a `divergence` note.
- Turns any recording into a fixture: agent behaviour can be tested in CI
  without spending tokens.

### 4.2 Assertions on recordings `[M]`
`flightrec check <id> --no-exec-failures --max-cost 2.00 --touched-only src/
--no-secrets` returns non-zero on violation. Pairs with replay to gate agent
changes in CI.

### 4.3 Annotations `[S]`
`flightrec note <id> --at <seq> "this is where it went wrong"` and inline
notes in the viewer, stored as `note` events (format unchanged).

---

## Tier 5 — engineering hygiene

- **Schema versioning** `[S]`: `meta.json` gets `"format": 1` and
  `Session.open` refuses newer formats with a clear message; `from_json`
  already tolerates unknown fields — add a migration hook for breaking
  changes. Do this *before* 1.2 adds new payload fields.
- **Lint and types in CI** `[S]`: `ruff` + `mypy --strict` on `src/`;
  `Kind`/`Source` should be the annotated types on `Event`, not `str`.
- **Property tests** `[S]`: Hypothesis over `build_steps` — any permutation
  of a valid event list yields monotonically indexed steps, every event id
  appears in at most one step, `cumulative_tokens` is non-decreasing; and
  over `Event.from_json(to_json())` round-trips.
- **Large-session performance** `[M]`: `session_detail` serialises every
  event; paginate `/api/sessions/<id>/events`, cache `build_steps` keyed by
  `(mtime, size)` of `events.jsonl`, and measure with a 100k-event fixture.
- **Packaging** `[S]`: publish to PyPI; `pipx install flightrec`; `uv tool`.
  Homebrew formula once the CLI surface is stable.
- **Viewer as a real package** `[M]`: split `index.html` into modules with a
  tiny build step (esbuild), keep "single file, no runtime deps" as the
  artefact.

---

## Recently landed

- Secret redaction (1.1): new `redact.py` scrubs auth headers, `sk-…`/`sk-ant-…`
  keys, GitHub/Slack/Google tokens, AWS keys, JWTs, bearer tokens and PEM
  private-key blocks from request/response bodies *before* `extract()` parses
  them, so no event can carry a key. `flightrec run --redact PATTERN`
  (repeatable regex/literal) adds project secrets; `FLIGHTREC_NO_REDACT=1` is
  a loudly-warned escape hatch. The watcher now ignores `.env*`, `*.pem`,
  `*.key`, `id_rsa*`, `.npmrc`, `.netrc` and similar credential files by
  default. Covered by `test_redact*.py` (per-pattern units plus an end-to-end
  proxy grep). 1.2 was gated on this and is now unblocked.
- Hardening pass (2026-09-14): session ids and blob hashes validated before
  path joins (closed an arbitrary-file read in the viewer's diff endpoint);
  `fork` refuses paths escaping the destination; atomic `meta.json`; readers
  skip malformed event lines and repair a crash-truncated last line; watcher
  and exec-collector threads survive per-item errors; `.git` no longer walked
  at seed; proxy/server answer 400/500 JSON instead of tracebacks; Ctrl-C
  escalates SIGTERM → SIGKILL.

## Explicitly not planned

- Editing recordings in place — the log is append-only by design; use notes.
- A hosted service — flightrec is local-first; export covers sharing.
- Replacing a harness's own transcript/resume — `fork` is deliberately the
  universal, harness-neutral mechanism.
