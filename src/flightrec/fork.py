"""Fork a session: reconstruct the working tree as it was at a chosen step
and hand it back so the agent can be restarted from there.

Because flightrec is harness-agnostic it cannot truly resume another tool's
internal transcript. Instead it does the universal thing: it materializes
the exact file state at step N into a fresh directory and writes a
``FORK.md`` summary of what happened up to that point (the user's request,
the steps taken, and the tool results), which can be pasted into any
harness as the starting context. This is the only fork mechanism that
works for every agent.
"""

from __future__ import annotations

from pathlib import Path

from .store import Session
from .timeline import build_steps, files_at


def _original_tree(session: Session) -> dict[str, str]:
    """Best-effort original content: the 'before' of the first time we saw each path."""
    origin: dict[str, str] = {}
    seen: set[str] = set()
    for e in sorted(session.events(), key=lambda e: e.seq or 0):
        for s in e.snapshots:
            if s.path in seen:
                continue
            seen.add(s.path)
            # A first sighting with no "before" is a file the session created;
            # it must not be treated as part of the original tree.
            if s.before is not None:
                origin[s.path] = s.before
    return origin


def materialize(session: Session, upto_seq: int, dest: Path) -> list[str]:
    """Write the working tree as of event ``upto_seq`` into ``dest``.

    Returns the list of files written. Only files flightrec observed being
    touched are reconstructed; files that existed but were never modified
    cannot be recovered from the recording (documented limitation).
    """
    dest.mkdir(parents=True, exist_ok=True)
    state = _original_tree(session)
    state.update(files_at(session.events(), upto_seq))

    written: list[str] = []
    for path, sha in sorted(state.items()):
        if sha is None or not session.blobs.has(sha):
            continue  # deleted by this point, or content not captured
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(session.blobs.get(sha))
        written.append(path)
    return written


def summary_markdown(session: Session, upto_seq: int) -> str:
    meta = session.read_meta()
    seq_of = {e.id: (e.seq or 0) for e in session.events()}
    steps = [s for s in build_steps(session.events())
             if s.event_ids and max(seq_of.get(i, 0) for i in s.event_ids) <= upto_seq]

    lines = [f"# Forked from flightrec session `{session.id}`", "",
             f"- Harness: **{meta.get('harness', '?')}**",
             f"- Original command: `{' '.join(meta.get('command', []))}`",
             f"- Forked at step {upto_seq}", "",
             "## What happened before the fork", ""]
    for st in steps:
        if st.kind == "user":
            lines.append(f"- **User asked:** {st.text}")
        elif st.kind == "assistant":
            lines.append(f"- **Assistant:** {st.text}")
        elif st.kind == "tool":
            files = ", ".join(f["path"] for f in st.fs) or "—"
            err = " _(error)_" if st.result and st.result.get("is_error") else ""
            lines.append(f"- **{st.call['name']}** → {files}{err}")
        elif st.kind == "exec":
            for x in st.execs:
                lines.append(f"- **$ {x.get('command')}** (exit {x.get('exit_code')})")
    lines += ["", "## Continue from here", "",
              "The working tree in this directory reflects the state at the fork point. "
              "Give your agent a new instruction to continue."]
    return "\n".join(lines) + "\n"


def fork(session: Session, upto_seq: int, dest: Path) -> dict:
    written = materialize(session, upto_seq, dest)
    (dest / "FORK.md").write_text(summary_markdown(session, upto_seq))
    return {"dest": str(dest), "files": written, "step": upto_seq}
