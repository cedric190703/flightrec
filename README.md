# flightrec

**A black-box flight recorder for AI coding agents — works with any harness.**

Run any coding agent (Claude Code, Aider, Codex, OpenCode, ...) through `flightrec`
and get a complete, replayable recording of the session: every LLM call, every
file edit, every shell command — stitched into one timeline you can scrub
through in the browser and fork from any step.

```
flightrec run -- aider
flightrec run -- claude
flightrec view
```

## Why

After an agent works on your code for 20 minutes you get a pile of changed
files and no good answer to *what did it do, why, and where did it go wrong?*

`flightrec` does not depend on any harness's hooks or logs. It observes the
three things every agent must do on your machine:

| The agent...          | flightrec observes it via                   |
|-----------------------|---------------------------------------------|
| calls an LLM          | a local reverse proxy (`*_BASE_URL` override)|
| edits files           | a filesystem watcher + content snapshots     |
| runs shell commands   | a `PATH` shim in front of the real shell     |

Those streams are correlated into a single, harness-neutral event log.

## Status

Early development. See the roadmap in `docs/` (coming).
