# flightrec project instructions

## Purpose

`flightrec` is a local, harness-agnostic recorder for AI coding-agent
sessions. It records LLM traffic, filesystem changes and top-level commands
into a portable on-disk session that can be inspected, replayed in the viewer,
or forked.

## Stack and architecture

- Python 3.11–3.13; package source is under `src/flightrec`.
- `watchdog` observes filesystem changes. On macOS with Python 3.14 and newer
  it uses watchdog's polling observer as a safe fallback for the native
  FSEvents extension.
- The standard library provides the HTTP proxy, server, CLI and storage.
- Sessions are append-only JSONL plus content-addressed blobs under
  `~/.flightrec/sessions` by default.

See [docs/architecture.md](docs/architecture.md) for the component model and
[docs/development.md](docs/development.md) for local setup and test commands.

## Quality bar

- Preserve the append-only event format and tolerate malformed external input.
- Add regression tests for every behavioral fix, including cleanup paths.
- Keep runtime dependencies minimal; do not add a framework for a small task.
- Run the complete pytest suite with a supported Python version before merging.
- Never put secrets, real recordings, or user session data in the repository.

## Delivery

There is no hosted service or deployment environment. GitHub Actions runs the
test suite on Python 3.11, 3.12 and 3.13. Releases and PyPI publishing are out
of scope until packaging is explicitly planned.
