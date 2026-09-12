import json
import threading
import urllib.request
from pathlib import Path

from flightrec.cli import main
from flightrec.server import make_server
from flightrec.store import list_sessions


def test_api_serves_sessions_steps_and_diffs(tmp_path: Path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "f.txt").write_text("one\n")
    main(["--home", str(home), "run", "--cwd", str(proj), "--no-proxy", "--",
          "bash", "-c", "printf 'one\\ntwo\\n' > f.txt; sleep 0.3"])
    (s,) = list_sessions(home)

    srv = make_server(home)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        get = lambda p: urllib.request.urlopen(base + p, timeout=5)
        assert b"<title>flightrec" in get("/").read()
        sessions = json.load(get("/api/sessions"))
        assert sessions[0]["id"] == s.id
        detail = json.load(get(f"/api/sessions/{s.id}"))
        assert detail["meta"]["exit_code"] == 0
        assert any(st["kind"] == "exec" and st["fs"] for st in detail["steps"])
        fs = next(f for st in detail["steps"] for f in st["fs"] if f["path"] == "f.txt")
        d = json.load(get(f"/api/sessions/{s.id}/diff?before={fs['before']}&after={fs['after']}&path=f.txt"))
        assert d["added"] == 1 and d["removed"] == 0 and "+two" in d["diff"]
        assert get(f"/api/sessions/{s.id}/blob/{fs['after']}").read() == b"one\ntwo\n"
        try:
            get("/api/sessions/nope")
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
