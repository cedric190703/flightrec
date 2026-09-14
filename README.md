# flightrec

[![CI](https://github.com/cedric190703/flightrec/actions/workflows/ci.yml/badge.svg)](https://github.com/cedric190703/flightrec/actions/workflows/ci.yml)

**A black-box flight recorder for AI coding agents — works with any harness.**

Run any coding agent (Claude Code, Aider, Codex, OpenCode, …) through
`flightrec` and get a complete, replayable recording of the session: every
LLM call, every file edit, every shell command — stitched into one timeline
you can scrub through in the browser and fork from any step.

```bash
flightrec run -- aider          # record
flightrec run -- claude
flightrec view                  # replay in the browser
```

## Why

After an agent works on your code for 20 minutes you get a pile of changed
files and no good answer to *what did it do, why, and where did it go wrong?*
flightrec answers that — for **any** agent, because it does not depend on a
harness's hooks or logs. It observes the three things every agent must do:

| The agent…           | flightrec observes it via                       |
|----------------------|-------------------------------------------------|
| calls an LLM         | a local reverse proxy (`*_BASE_URL` override)   |
| edits files          | a filesystem watcher + content snapshots        |
| runs shell commands  | a `PATH` shim in front of the real shell        |

Those streams are correlated into a single, harness-neutral event log.
See [`docs/architecture.md`](docs/architecture.md).

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

### Record a session

```bash
flightrec run -- <your agent command>
# e.g.
flightrec run -- claude
flightrec run --cwd ./myproject -- aider --model sonnet
flightrec run --no-proxy -- aider      # files + commands only, no API capture
```

flightrec sets `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` so the agent's API
calls route through its proxy, watches the project directory, and shims the
shell — then launches your command untouched. Recordings land in
`~/.flightrec/sessions/<id>/`.

Ctrl-C is forwarded to the agent. If it is still running half a second
later, flightrec sends `SIGTERM`; a second Ctrl-C escalates to `SIGKILL`.
Either way the session is closed with the exit code in `meta.json`.

### Inspect from the terminal

```bash
flightrec list                         # all recorded sessions
flightrec show <id>                    # raw events
flightrec show --steps <id>            # correlated steps (recommended)
```

```
  2 16:46:43 TOOL  Read
  3 16:46:45 TOOL  Edit [strong]          tok=2750
                   modify  calc.py
  5 16:46:50 TOOL  Bash [strong]          tok=6310
                   $ bash -c pytest -q -> 0
```

### Replay in the browser

```bash
flightrec view                         # opens http://127.0.0.1:7357
flightrec view <id>                    # open a specific session
```

Timeline on the left (colour-coded by step kind), inspector in the middle
(tool input, commands with exit codes, per-file diffs, tool result), and the
working tree at the selected moment on the right. Use the slider or the
arrow keys to scrub through time.

### Fork from a step

```bash
flightrec fork <id> --at <seq> --dest ./retry
```

Reconstructs the exact file contents as of that step and writes a `FORK.md`
summary of the request and steps so far — paste it into any agent to
continue from that point without redoing the earlier work.

## Secrets

Recordings are meant to be shared, so credentials are stripped before
anything is written to disk. The proxy scrubs request and response bodies
*before* they are parsed, so no event can carry a key: auth headers, `sk-…`
/ `sk-ant-…` keys, GitHub/Slack/Google tokens, AWS access keys, JWTs, bearer
tokens and PEM private-key blocks are replaced with `«redacted»`. The
watcher also skips credential files by default (`.env*`, `*.pem`, `*.key`,
`id_rsa*`, `.npmrc`, `.netrc`, …), so they are never snapshotted.

```bash
flightrec run --redact 'CORP-[0-9]+' -- claude   # redact a project-specific secret
FLIGHTREC_NO_REDACT=1 flightrec run -- claude     # escape hatch (loudly warned)
```

## Supported providers

Anthropic Messages, OpenAI Chat Completions and OpenAI Responses are parsed
into structured `tool_call` / `tool_result` events (streaming and
non-streaming). Any other provider is recorded verbatim and can be routed
through `/to/<host>`; add a parser in `wire.py` to structure it.

## Development

```bash
.venv/bin/python -m pytest -q  # full suite
```

No runtime dependencies beyond `watchdog`; the proxy, server and viewer use
only the standard library. The viewer is a single dependency-free HTML file.

Development and CI are supported on Python 3.11–3.13. See
[`docs/development.md`](docs/development.md) for setup, targeted test commands,
and the macOS/Python 3.14 filesystem-observer fallback.

## License

MIT — see [LICENSE](LICENSE).
