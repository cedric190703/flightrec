# Repository guide for contributors and agents

## Commands

Create a Python 3.11–3.13 environment, install the package in editable mode,
and run the tests:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

On macOS/Python 3.14+, filesystem integration tests use the polling observer
fallback. See `docs/development.md` for details.

## Conventions

- Keep public CLI behavior backwards compatible unless a change is documented.
- Use the existing standard-library-first approach and type annotations.
- Treat HTTP bodies, JSONL, filenames, and subprocess failures as untrusted.
- Cleanup methods must be safe when startup only partially completed; recorder
  failures must still leave a closed session with a useful note.
- Add focused pytest coverage next to the component being changed.
- Update README and the relevant `docs/` page when user-visible behavior or
  requirements change.

## Prohibited actions

- Do not commit API keys, recordings, session directories, or generated caches.
- Do not weaken the proxy's failure isolation or the filesystem ignore rules.
- Do not rewrite unrelated files or delete user work to make a test pass.
