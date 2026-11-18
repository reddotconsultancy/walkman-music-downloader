#!/usr/bin/env python3
"""Local web UI: paste a playlist link, watch it download."""
from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from walkman_dl.core import Options, run_playlist

APP_DIR = Path(__file__).parent
DEFAULT_OUT = Path.home() / "Desktop" / "Walkman Music"

app = Flask(__name__, template_folder=str(APP_DIR / "templates"),
            static_folder=str(APP_DIR / "static"))
# Pick up template edits without a restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# One job at a time - keeps YouTube happy and the UI simple.
STATE: dict = {
    "running": False,
    "results": [],
    "started": 0.0,
    "total": 0,
    "done": 0,
    "history": [],        # every event of the current job, for replay
    "seq": 0,
}

_SUBS: set[queue.Queue] = set()      # one queue per open browser tab
_LOCK = threading.Lock()


def _emit(kind: str, **payload) -> None:
    """Broadcast to every connected tab and keep a replayable copy.

    A single shared queue would hand each event to whichever tab popped it
    first, so a second tab (or a reconnect) silently ate half the log.
    """
    with _LOCK:
        STATE["seq"] += 1
        ev = {"seq": STATE["seq"], "kind": kind, **payload}
        STATE["history"].append(ev)
        del STATE["history"][:-3000]
        subs = list(_SUBS)
    for q in subs:
        q.put(ev)


def _worker(url: str, opts: Options) -> None:
    STATE["running"] = True
    STATE["results"] = []
    STATE["done"] = 0
    STATE["total"] = 0
    STATE["started"] = time.time()
    with _LOCK:
        STATE["history"] = []
    try:
        def log(msg: str) -> None:
            if msg.endswith("item(s) found"):
                try:
                    STATE["total"] = int(msg.split()[0])
                except ValueError:
                    pass
            _emit("log", msg=msg, total=STATE["total"], done=STATE["done"])

        def on_result(r) -> None:
            STATE["results"].append(r)
            STATE["done"] += 1
            _emit("track", track=asdict(r), total=STATE["total"],
                  done=STATE["done"])

        run_playlist(url, opts, log, on_result)
        ok = [r for r in STATE["results"] if r.status == "ok"]
        _emit("done",
              ok=len(ok),
              skipped=sum(1 for r in STATE["results"] if r.status == "skipped"),
              failed=sum(1 for r in STATE["results"] if r.status == "error"),
              total_mb=round(sum(r.size_mb for r in ok), 1),
              out_dir=str(opts.out_dir),
              seconds=round(time.time() - STATE["started"]))
    except Exception as exc:
        _emit("fatal", msg=str(exc)[:500])
    finally:
        STATE["running"] = False


@app.get("/")
def index():
    return render_template("index.html", default_out=str(DEFAULT_OUT))


@app.post("/api/start")
def start():
    if STATE["running"]:
        return jsonify({"error": "a download is already running"}), 409
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if len(url) < 8:
        return jsonify({"error": "paste a YouTube Music album or playlist "
                                 "link, or a list id"}), 400

    out = (data.get("out") or "").strip() or str(DEFAULT_OUT)
    opts = Options(
        out_dir=Path(out).expanduser(),
        max_mb=float(data.get("max_mb") or 10),
        target_bitrate=int(data.get("bitrate") or 320),
        min_bitrate=int(data.get("min_bitrate") or 128),
        art_px=int(data.get("art_px") or 500),
        country=(data.get("country") or "US").upper()[:2],
        folders=bool(data.get("folders", True)),
        number_files=bool(data.get("number_files", True)),
        overwrite=bool(data.get("overwrite", False)),
        limit=int(data.get("limit") or 0),
        cookies_from_browser=(data.get("cookies") or "").strip(),
        album_mode=(data.get("album_mode") or "auto"),
    )
    threading.Thread(target=_worker, args=(url, opts), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/stream")
def stream():
    q: queue.Queue = queue.Queue()
    with _LOCK:
        backlog = list(STATE["history"])
        _SUBS.add(q)

    def gen():
        try:
            yield "retry: 3000\n\n"
            for ev in backlog:          # a reconnecting tab catches up
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            with _LOCK:
                _SUBS.discard(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.get("/api/status")
def status():
    return jsonify({
        "running": STATE["running"],
        "done": STATE["done"],
        "total": STATE["total"],
        "results": [asdict(r) for r in STATE["results"]],
    })


if __name__ == "__main__":
    port = 7788
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
