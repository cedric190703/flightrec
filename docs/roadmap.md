# Roadmap

Milestones, in the order they were built. Everything through M6 is done.

- [x] **M1 — Record** `flightrec run -- <cmd>` captures fs + exec into JSONL.
- [x] **M2 — Snapshot** before/after content blobs on every file change.
- [x] **M3 — LLM proxy** capture Anthropic + OpenAI traffic (JSON & SSE).
- [x] **M4 — Correlate** stitch streams into steps with confidence scores.
- [x] **M5 — View** web timeline, step inspector, diffs, scrubber.
- [x] **M6 — Fork** rebuild the tree at any step + context summary.

## Next

- [ ] **Native adapters** that enrich the recording when a harness offers
      more than the wire shows (Claude Code hooks, Aider chat history,
      Codex session files). The recorder already works without them; an
      adapter just adds exact ids and the human-typed prompt.
- [ ] **Analysis layer**: detect loops (same tool call repeated), wasted
      turns, cost-per-outcome; a "where did it go wrong" heatmap.
- [ ] **Align the event schema** with OpenTelemetry GenAI semantic
      conventions so traces interoperate with other tooling.
- [ ] **Gemini / Vertex** wire parser via the `/to/<host>` route.
- [ ] True resume for harnesses that support it (`claude --resume`, etc.).

## Evaluation ideas (for the report)

- *Capture fidelity*: % of tool calls correctly linked to their fs/exec
  side effects, measured on scripted sessions with known ground truth.
- *Human utility*: time for a person to locate the step where an agent
  went wrong, viewer vs. raw terminal log.
- *Overhead*: added latency per LLM call through the proxy.
