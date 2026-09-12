# Architecture

flightrec records an AI coding agent by observing the *side effects* every
agent must produce on the machine, rather than relying on any harness's
hooks or logs. That is what makes it harness-agnostic.

```
                 ┌──────────── any harness ───────────┐
                 │  claude / codex / aider / opencode  │
                 └──┬───────────┬───────────┬──────────┘
        LLM traffic │  fs events │   exec    │
                    ▼            ▼           ▼
             ┌──────────┐ ┌──────────┐ ┌─────────┐
             │  proxy   │ │ fswatch  │ │  shim   │
             └────┬─────┘ └────┬─────┘ └────┬────┘
                  └────────────┴─────┬──────┘
                                     ▼
                         events.jsonl + blobs/      (store.py)
                                     ▼
                         build_steps()              (timeline.py)
                                     ▼
                   CLI  (show --steps)   /   Web viewer (server.py)
                                     ▼
                             fork  (fork.py)
```

## Observers

| Module        | Observes        | Mechanism |
|---------------|-----------------|-----------|
| `proxy.py`    | LLM API calls   | Local reverse proxy injected via `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`. Streams responses through untouched, parses them afterwards. No TLS interception. |
| `fswatch.py`  | File changes    | `watchdog` on the project root + content-addressed snapshots (before/after). |
| `shim.py`     | Shell commands  | `PATH` shims in front of common shells and dev tools; log argv + exit code. |

Any observer can be absent (e.g. `--no-proxy`, or a harness whose traffic we
cannot intercept) and the rest still produce a usable recording.

## The event model (`events.py`)

One small, harness-neutral vocabulary: `session_start/end`, `user_message`,
`llm_request/response`, `tool_call`, `tool_result`, `fs_change`, `exec`,
`note`. Every event carries `ts`, `seq`, `source`, `links` (causal ids) and
optional `snapshots`. This is the project's core contribution: a single
schema that describes "what an agent did" regardless of which tool produced
it.

## Correlation (`timeline.py`)

The observers are independent, so `build_steps()` stitches their events into
a human-readable list of *steps*: it opens a step at each `tool_call` and
attributes the `fs_change` / `exec` events that occur before the matching
`tool_result` to it, scoring each attribution as **strong** (the call input
mentions the path/command) or **timing** (co-occurrence only). Observers
stamp events with the time a change was *first seen* (not when the fs
debounce fired), and a change landing up to `ATTRIBUTION_GRACE_S` after
the `tool_result` is still credited to that call. Recordings made without
the proxy fall back to one step per top-level command.

## Storage (`store.py`)

Plain files, no database: `~/.flightrec/sessions/<id>/` holds `meta.json`,
an append-only `events.jsonl`, and a content-addressed `blobs/` directory
for file snapshots. Easy to inspect, diff and ship to the viewer.

## Known limitations

- Commands invoked by absolute path (`/bin/sh -c ...`) bypass the PATH shim;
  their file side effects are still captured by the watcher.
- Harnesses that pin TLS certificates or ignore `*_BASE_URL` are not
  intercepted; fs + exec recording still works.
- Files that existed but were never touched cannot be reconstructed by
  `fork` (only observed files are in the blob store).
- Correlation is heuristic (time + content), not guaranteed causality.
