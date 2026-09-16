# Development and troubleshooting

## Supported Python versions

The CI contract is Python **3.11–3.13**. Use one of those versions for normal
development and release verification:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

`flightrec` remains usable with newer Python versions. On macOS and Python
3.14+, it deliberately selects watchdog's polling observer instead of the
native FSEvents observer: this avoids crashes in native watchdog builds that
have not yet caught up with a new Python runtime. Polling checks every 100 ms,
so it is a safe compatibility path with a small increase in filesystem-watch
overhead.

## Test suite

The tests are split by component and require no API credentials or outbound
network access; proxy and server tests bind temporary localhost ports. A full
run exercises the CLI, event schema, session store, filesystem watcher, command
shims, proxy/server routes, wire parsers, correlation and forking.

```bash
.venv/bin/python -m pytest -q
```

To investigate a single area, use a file or test selector:

```bash
.venv/bin/python -m pytest -q tests/test_fswatch_extra.py
.venv/bin/python -m pytest -q -k lifecycle
```

## Recorder failure behavior

A session always starts with `session_start` and is finalized with
`session_end`, including when the harness cannot launch. Expected exit codes:

| Situation | Exit code |
| --- | ---: |
| Command is not found | 127 |
| Command cannot be executed | 126 |
| Recorder setup fails | 125 |

In each case, inspect `flightrec show <session-id>` for a `note` explaining
the failure. Partial observer startup is cleaned up before the session closes.

## Event log recovery

The session store skips malformed JSONL records by default; use
`session.events(strict=True)` to report the first malformed record with its
line number. Existing event sequences are never renumbered. When reopening a
log, the next sequence is the greater of the nonblank line count and one past
the highest stored nonnegative integer sequence. This prevents collisions
after lines are removed or reordered, while retaining sequence slots for
damaged records. Invalid sequence values do not influence that maximum.
A missing final newline is repaired before appending the next event.

Recovery does not repair preexisting duplicate sequences, and concurrent
writers through separate `Session` instances are not supported.
