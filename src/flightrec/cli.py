"""flightrec command line.

    flightrec run [--cwd DIR] [--ignore PATTERN]... [--redact PATTERN]... -- <harness command...>
    flightrec list
    flightrec show <session-id> [--kind KIND]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .events import Kind
from .store import DEFAULT_ROOT, list_sessions, open_session


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int((ts % 1) * 1000):03d}"


def _one_line(text: str, width: int) -> str:
    """Collapse whitespace so a multi-line message stays on one table row."""
    return " ".join(text.split())[:width]


def cmd_run(args: argparse.Namespace) -> int:
    from .recorder import Recorder

    if not args.command:
        print("flightrec run: missing harness command after '--'", file=sys.stderr)
        return 2
    if not Path(args.cwd).is_dir():
        print(f"flightrec run: --cwd {args.cwd!r} is not a directory", file=sys.stderr)
        return 2
    rec = Recorder(args.command, Path(args.cwd), root=Path(args.home),
                   ignores=args.ignore, harness=args.harness, proxy=not args.no_proxy,
                   redact=args.redact)
    print(f"[flightrec] recording session {rec.session.id} -> {rec.session.dir}", file=sys.stderr)
    if not rec.redaction_enabled:
        print("[flightrec] WARNING: secret redaction is OFF (FLIGHTREC_NO_REDACT); "
              "API keys and tokens will be stored in clear text.", file=sys.stderr)
    rc = rec.run()
    n = len(rec.session)
    print(f"[flightrec] session {rec.session.id} ended (exit {rc}, {n} events)", file=sys.stderr)
    print(f"[flightrec] inspect: flightrec show --steps {rec.session.id}   "
          f"|   flightrec view {rec.session.id}", file=sys.stderr)
    return rc


def cmd_list(args: argparse.Namespace) -> int:
    sessions = list_sessions(Path(args.home))
    if not sessions:
        print("no sessions recorded yet")
        return 0
    print(f"{'SESSION':<22} {'HARNESS':<12} {'EVENTS':>6}  {'EXIT':>4}  COMMAND")
    for s in sessions:
        m = s.read_meta()
        cmd = " ".join(m.get("command", []))
        print(f"{s.id:<22} {m.get('harness', '?'):<12} {len(s):>6}  "
              f"{str(m.get('exit_code', '')):>4}  {cmd[:60]}")
    return 0


def _show_steps(s) -> None:
    from .timeline import build_steps

    for st in build_steps(s.events()):
        tag = {"tool": "TOOL", "exec": "EXEC", "fs": "FS", "user": "USER",
               "assistant": "AI", "session": "--"}[st.kind]
        conf = "" if st.confidence == "n/a" else f" [{st.confidence}]"
        dur = f" {st.duration}s" if st.duration is not None else ""
        print(f"{st.index:>3} {_fmt_ts(st.ts)} {tag:<5} {_one_line(st.title, 70)}{dur}{conf}"
              f"  tok={st.cumulative_tokens}")
        for f in st.fs:
            print(f"{'':>18}  {f['op']:<7} {f['path']}")
        for x in st.execs:
            rc = "" if x.get("exit_code") is None else f" -> {x['exit_code']}"
            print(f"{'':>18}  $ {_one_line(x.get('command') or '', 70)}{rc}")


def cmd_show(args: argparse.Namespace) -> int:
    try:
        s = open_session(args.session, Path(args.home))
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    if args.steps:
        _show_steps(s)
        return 0
    for e in s.events():
        if args.kind and e.kind not in args.kind:
            continue
        p = e.payload
        match e.kind:
            case Kind.FS_CHANGE:
                desc = f"{p.get('op', '?'):<7} {p.get('path')} ({p.get('size', 0)} B)"
            case Kind.EXEC:
                if p.get("phase") == "end":
                    desc = f"  -> exit {p.get('exit_code')} in {p.get('duration')}s"
                else:
                    desc = f"$ {p.get('command') or ''}"
            case Kind.LLM_REQUEST:
                desc = f"-> {p.get('provider')} {p.get('model')} ({p.get('n_messages')} msgs)"
            case Kind.LLM_RESPONSE:
                u = p.get("usage") or {}
                desc = (f"<- {p.get('stop_reason')} in={u.get('input')} out={u.get('output')} "
                        f"{p.get('latency')}s  {str(p.get('text', ''))[:60]!r}")
            case Kind.USER_MESSAGE:
                desc = f"user: {str(p.get('text') or '')[:80]!r}"
            case Kind.TOOL_CALL:
                desc = f"call {p.get('name')} {str(p.get('input'))[:80]}"
            case Kind.TOOL_RESULT:
                desc = f"result{' ERROR' if p.get('is_error') else ''}: {str(p.get('content'))[:70]!r}"
            case Kind.SESSION_START:
                desc = f"start {' '.join(map(str, p.get('command') or []))} in {p.get('cwd')}"
            case Kind.SESSION_END:
                desc = f"end (exit {p.get('exit_code')})"
            case _:
                desc = str(p)[:120]
        print(f"{e.seq:>4} {_fmt_ts(e.ts)} {e.kind:<13} {e.source:<10} {desc}")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    import webbrowser

    from .server import make_server

    try:
        srv = make_server(Path(args.home), port=args.port)
    except OSError as exc:
        print(f"flightrec view: cannot listen on port {args.port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{srv.server_port}/" + (f"#{args.session}" if args.session else "")
    print(f"[flightrec] viewer at {url}  (Ctrl-C to stop)", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    from .fork import fork

    try:
        s = open_session(args.session, Path(args.home))
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()) and not args.force:
        print(f"flightrec fork: {dest} is not empty (use --force)", file=sys.stderr)
        return 1
    info = fork(s, args.at, dest)
    print(f"[flightrec] forked session {s.id} at step {args.at} -> {dest}", file=sys.stderr)
    print(f"[flightrec] wrote {len(info['files'])} files + FORK.md", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="flightrec",
                                 description="Flight recorder for AI coding agents.")
    ap.add_argument("--version", action="version", version=f"flightrec {__version__}")
    ap.add_argument("--home", default=str(DEFAULT_ROOT),
                    help="where sessions are stored (default: ~/.flightrec)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="record a harness session")
    run.add_argument("--cwd", default=".", help="project directory to watch and run in")
    run.add_argument("--ignore", action="append", default=[], help="extra glob to ignore")
    run.add_argument("--harness", help="label for this harness (default: command name)")
    run.add_argument("--no-proxy", action="store_true",
                     help="do not intercept LLM API traffic (fs + exec only)")
    run.add_argument("--redact", action="append", default=[], metavar="PATTERN",
                     help="extra secret to redact from recorded bodies (regex or "
                          "literal; repeatable). Built-in keys/tokens are always redacted "
                          "unless FLIGHTREC_NO_REDACT=1.")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="harness command, after '--'")
    run.set_defaults(fn=cmd_run)

    ls = sub.add_parser("list", help="list recorded sessions")
    ls.set_defaults(fn=cmd_list)

    show = sub.add_parser("show", help="print a session's events")
    show.add_argument("session")
    show.add_argument("--kind", action="append", help="only these event kinds")
    show.add_argument("--steps", action="store_true",
                      help="show correlated steps instead of raw events")
    show.set_defaults(fn=cmd_show)
    view = sub.add_parser("view", help="open the web viewer")
    view.add_argument("session", nargs="?", help="session to open first")
    view.add_argument("--port", type=int, default=7357)
    view.add_argument("--no-browser", action="store_true")
    view.set_defaults(fn=cmd_view)

    fk = sub.add_parser("fork", help="rebuild the working tree at a step into a new dir")
    fk.add_argument("session")
    fk.add_argument("--at", type=int, required=True, help="event seq to fork at")
    fk.add_argument("--dest", required=True, help="destination directory")
    fk.add_argument("--force", action="store_true", help="write into a non-empty directory")
    fk.set_defaults(fn=cmd_fork)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
