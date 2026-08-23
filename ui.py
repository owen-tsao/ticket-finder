#!/usr/bin/env python3
"""Local web UI for Ticket Finder.

    .venv/bin/python ui.py          # then open http://127.0.0.1:8321

One page: search concerts, pick one, set zone thresholds with live prices
in front of you, and start/stop the watcher. Plain stdlib http.server —
no build step, no extra dependencies; everything reuses discover.py and
watcher.py directly.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import discover
import watcher

HERE = Path(__file__).parent
INDEX = HERE / "static" / "index.html"
PORT = 8321

# last search results live here so /api/select can match without re-searching
_cache_lock = threading.Lock()
_last_search: dict = {"query": None, "results": None}


# ------------------------------------------------------------ watcher control

def watcher_pid() -> int | None:
    """The running watcher's pid, via its flock (no pgrep guesswork)."""
    try:
        fh = open(watcher.LOCK_FILE)
    except OSError:
        return None
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None                       # lockable = nobody holds it
        except OSError:
            try:
                return int(fh.read().strip() or 0) or None
            except ValueError:
                return None


def start_watcher() -> dict:
    if watcher_pid():
        return {"ok": True, "note": "already running"}
    subprocess.Popen(
        [sys.executable, str(HERE / "watcher.py")],
        cwd=HERE, start_new_session=True,     # survives the UI server exiting
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"ok": True}


def stop_watcher() -> dict:
    pid = watcher_pid()
    if not pid:
        return {"ok": True, "note": "not running"}
    os.kill(pid, signal.SIGTERM)
    return {"ok": True}


# --------------------------------------------------------------------- status

def read_status() -> dict:
    cfg = None
    if (HERE / "config.json").exists():
        try:
            raw = json.loads((HERE / "config.json").read_text())
            cfg = {k: raw.get(k) for k in ("event", "stop_at", "min_seats", "zones")}
            cfg["sites"] = sorted(raw.get("sources", {}))
        except (json.JSONDecodeError, OSError):
            pass

    prices = []
    if watcher.STATE_FILE.exists():
        try:
            state = json.loads(watcher.STATE_FILE.read_text())
            for key, zs in state.items():
                if key.startswith("_") or not isinstance(zs, dict):
                    continue
                src, _, zone = key.partition(":")
                prices.append({
                    "source": src, "zone": watcher.zone_label(zone),
                    "min": zs.get("min"), "floor": zs.get("floor"),
                    "section": zs.get("section"), "ts": zs.get("ts"),
                })
        except (json.JSONDecodeError, OSError):
            pass

    log_tail = []
    if watcher.LOG_FILE.exists():
        try:
            log_tail = watcher.LOG_FILE.read_text().splitlines()[-12:]
        except OSError:
            pass

    return {"running": watcher_pid() is not None, "config": cfg,
            "prices": prices, "log": log_tail}


# ------------------------------------------------------------------ API logic

def api_search(q: str) -> dict:
    results = discover.run_searches(q)
    with _cache_lock:
        _last_search["query"], _last_search["results"] = q, results
    anchors = discover.upcoming_anchors(results)
    return {"query": q, "events": [h.to_dict() for h in anchors[:15]]}


def api_select(body: dict) -> dict:
    with _cache_lock:
        cached = _last_search["query"] == body.get("query")
        results = _last_search["results"] if cached else None
    if results is None:
        results = discover.run_searches(body["query"])
    raw = body["anchor"]
    anchor = discover.Hit(
        raw["site"], raw["name"], raw["venue"],
        datetime.fromisoformat(raw["dt"]) if raw.get("dt") else None,
        raw.get("min_price"), raw.get("ids", {}),
    )
    sources, matches = discover.match_sources(anchor, results)
    zones = discover.snapshot_zones(sources)
    return {
        "anchor": anchor.to_dict(),
        "sources": sources,
        "matches": {site: h.to_dict() for site, h in matches.items()},
        "zones": [
            {"zone": z, "label": watcher.zone_label(z), "min": round(p, 2)}
            for z, p in sorted(zones.items(), key=lambda kv: kv[1]) if z != "Other"
        ],
    }


def api_save(body: dict) -> dict:
    anchor = body["anchor"]
    dt = datetime.fromisoformat(anchor["dt"])
    zones = {
        z: {"good": float(t["good"]), "screaming": float(t["screaming"])}
        for z, t in body.get("zones", {}).items()
    }
    if not zones:
        return {"ok": False, "error": "Set thresholds for at least one zone."}
    cfg = {
        "event": f"{anchor['name']} @ {anchor['venue']} ({dt:%a %b %-d, %-I:%M %p})",
        "stop_at": (dt + timedelta(minutes=90)).isoformat(timespec="seconds"),
        "min_seats": int(body.get("min_seats", 2)),
        "zones": zones,
        "sources": body["sources"],
    }
    (HERE / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    if body.get("start"):
        stop_watcher()          # replace any watcher running on the old config
        start_watcher()
    return {"ok": True, "event": cfg["event"]}


# ----------------------------------------------------------------- the server

class Handler(BaseHTTPRequestHandler):
    def _send(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            page = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif url.path == "/api/status":
            self._send(read_status())
        elif url.path == "/api/search":
            q = parse_qs(url.query).get("q", [""])[0].strip()
            if not q:
                self._send({"error": "empty query"}, 400)
                return
            try:
                self._send(api_search(q))
            except Exception as e:
                self._send({"error": f"{type(e).__name__}: {e}"}, 502)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        routes = {
            "/api/select": api_select,
            "/api/config": api_save,
            "/api/watcher/start": lambda _b: start_watcher(),
            "/api/watcher/stop": lambda _b: stop_watcher(),
        }
        fn = routes.get(urlparse(self.path).path)
        if not fn:
            self._send({"error": "not found"}, 404)
            return
        try:
            self._send(fn(self._body()))
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, 502)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[ui] {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Ticket Finder UI → http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
