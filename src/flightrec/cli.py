"""flightrec command line.

    flightrec run [--cwd DIR] [--ignore PATTERN]... -- <harness command...>
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


def cmd_run(args: argparse.Namespace) -> int:
    from .recorder import Recorder

    if not args.command:
        print("flightrec run: missing harness command after '--'", file=sys.stderr)
        return 2
    rec = Recorder(args.command, Path(args.cwd), root=Path(args.home),
                   ignores=args.ignore, harness=args.harness, proxy=not args.no_proxy)
    print(f"[flightrec] recording session {rec.session.id} -> {rec.session.dir}", file=sys.stderr)
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
        print(f"{st.index:>3} {_fmt_ts(st.ts)} {tag:<5} {st.title[:70]}{dur}{conf}"
              f"  tok={st.cumulative_tokens}")
        for f in st.fs:
            print(f"{'':>18}  {f['op']:<7} {f['path']}")
        for x in st.execs:
            rc = "" if x.get("exit_code") is None else f" -> {x['exit_code']}"
            print(f"{'':>18}  $ {x.get('command', '')[:70]}{rc}")


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
                desc = f"{p['op']:<7} {p['path']} ({p['size']} B)"
            case Kind.EXEC:
                if p.get("phase") == "end":
                    desc = f"  -> exit {p['exit_code']} in {p['duration']}s"
                else:
                    desc = f"$ {p['command']}"
            case Kind.LLM_REQUEST:
                desc = f"-> {p.get('provider')} {p.get('model')} ({p.get('n_messages')} msgs)"
            case Kind.LLM_RESPONSE:
                u = p.get("usage") or {}
                desc = (f"<- {p.get('stop_reason')} in={u.get('input')} out={u.get('output')} "
                        f"{p.get('latency')}s  {str(p.get('text', ''))[:60]!r}")
            case Kind.USER_MESSAGE:
                desc = f"user: {p['text'][:80]!r}"
            case Kind.TOOL_CALL:
                desc = f"call {p['name']} {str(p.get('input'))[:80]}"
            case Kind.TOOL_RESULT:
                desc = f"result{' ERROR' if p.get('is_error') else ''}: {str(p.get('content'))[:70]!r}"
            case Kind.SESSION_START:
                desc = f"start {' '.join(p['command'])} in {p['cwd']}"
            case Kind.SESSION_END:
                desc = f"end (exit {p['exit_code']})"
            case _:
                desc = str(p)[:120]
        print(f"{e.seq:>4} {_fmt_ts(e.ts)} {e.kind:<13} {e.source:<10} {desc}")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    import webbrowser

    from .server import make_server

    srv = make_server(Path(args.home), port=args.port)
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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
