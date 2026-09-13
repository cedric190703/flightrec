# Roadmap

Milestones, in the order they were built. Everything through M6 is done.

- [x] **M1 — Record** `flightrec run -- <cmd>` captures fs + exec into JSONL.
- [x] **M2 — Snapshot** before/after content blobs on every file change.
- [x] **M3 — LLM proxy** capture Anthropic + OpenAI traffic (JSON & SSE).
- [x] **M4 — Correlate** stitch streams into steps with confidence scores.
- [x] **M5 — View** web timeline, step inspector, diffs, scrubber.
- [x] **M6 — Fork** rebuild the tree at any step + context summary.

## Next

The ordered backlog lives in [`next_path.md`](../next_path.md) at the repo
root, with a "start here" list of the three recommended next items (secret
redaction, full request capture, command output capture). This page only
tracks milestones.

- [ ] **M7 — Complete & safe**: redaction, full request bodies, command
      output, whole-tree snapshot (next_path Tier 1).
- [ ] **M8 — Live**: tailing viewer, search, cost, export (Tier 2).
- [ ] **M9 — Everywhere**: more wire formats, `doctor`, native adapters,
      Windows (Tier 3).
- [ ] **M10 — Replay**: deterministic replay and CI assertions (Tier 4).

## Evaluation ideas (for the report)

- *Capture fidelity*: % of tool calls correctly linked to their fs/exec
  side effects, measured on scripted sessions with known ground truth.
- *Human utility*: time for a person to locate the step where an agent
  went wrong, viewer vs. raw terminal log.
- *Overhead*: added latency per LLM call through the proxy.
