#!/usr/bin/env python3
"""Local web UI for Doors — the last-minute ticket price watcher.

    .venv/bin/python ui.py          # then open http://127.0.0.1:8321

One page: search concerts, add them to your tracked list with per-zone
deal/steal thresholds, and start/stop the watcher. Plain stdlib
http.server — no build step, no extra dependencies; everything reuses
discover.py and watcher.py directly.
"""

from __future__ import annotations   # keeps 3.10+ type syntax parseable below

import sys

if sys.version_info < (3, 10):       # macOS system python3 can be 3.9
    sys.exit(f"Doors needs Python 3.10+ (you have {sys.version.split()[0]}) "
             f"— try: brew install python")

import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
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


def stop_watcher(wait_s: float = 0.0) -> dict:
    """SIGTERM the watcher; optionally block until its flock is released.
    Callers that restart MUST wait — starting while the old process still
    holds the lock either no-ops (pid check) or makes the new watcher exit."""
    pid = watcher_pid()
    if not pid:
        return {"ok": True, "note": "not running"}
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait_s
    while watcher_pid() and time.time() < deadline:
        time.sleep(0.1)
    return {"ok": True}


# --------------------------------------------------------------------- events

def load_events() -> list[dict]:
    """Raw tracked events from config.json (new list shape or legacy single)."""
    raw = _load_config_raw()
    return raw["events"] if "events" in raw else ([raw] if raw else [])


def _load_config_raw() -> dict:
    try:
        return json.loads((HERE / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_events(events: list[dict]) -> None:
    # keep sibling top-level settings (ntfy_topic) intact
    cfg: dict = {"events": events}
    topic = _load_config_raw().get("ntfy_topic")
    if topic:
        cfg["ntfy_topic"] = topic
    (HERE / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def event_id(ev: dict) -> str:
    return ev.get("id") or ev.get("event", "?")


def _restart_watcher_if_running() -> None:
    """The watcher reads config at startup, so config changes need a bounce."""
    if watcher_pid():
        stop_watcher(wait_s=5)
        start_watcher()


def itunes_artwork(artist: str) -> str | None:
    """Free, keyless artist artwork fallback (600x600 album art)."""
    term = artist.split(",")[0].split(" & ")[0].split(" with ")[0].strip()
    url = ("https://itunes.apple.com/search?entity=album&limit=1&term="
           + urllib.parse.quote(term))
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            results = json.load(r).get("results", [])
        art = results[0].get("artworkUrl100") if results else None
        return art.replace("100x100", "600x600") if art else None
    except Exception:
        return None


# --------------------------------------------------------------------- status

def read_status() -> dict:
    # prices per event, from the shared per-event state file
    state_events: dict = {}
    if watcher.STATE_FILE.exists():
        try:
            raw = json.loads(watcher.STATE_FILE.read_text())
            if "_event" in raw:                     # legacy single-event shape
                state_events = {raw["_event"]: {
                    k: v for k, v in raw.items() if not k.startswith("_")}}
            else:
                state_events = raw.get("events") or {}
        except (json.JSONDecodeError, OSError):
            pass

    events = []
    for ev in load_events():
        zone_cfg = ev.get("zones") or {}
        prices = []
        for key, zs in state_events.get(ev.get("event", ""), {}).items():
            if not isinstance(zs, dict):
                continue
            src, _, zone = key.partition(":")
            prices.append({
                "source": src, "zone": watcher.zone_label(zone),
                "min": zs.get("min"), "floor": zs.get("floor"),
                "section": zs.get("section"), "ts": zs.get("ts"),
                "currency": zone_cfg.get(zone, {}).get("currency", "USD"),
            })
        tiers = {
            z: {"deal": t.get("deal", t.get("good")),
                "steal": t.get("steal", t.get("screaming")),
                "currency": t.get("currency", "USD")}
            for z, t in zone_cfg.items()
        }
        events.append({
            "id": event_id(ev),
            "event": ev.get("event"),
            "name": ev.get("name") or (ev.get("event") or "?").split(" @ ")[0],
            "venue": ev.get("venue"),
            "dt": ev.get("dt"),
            "time_known": ev.get("time_known", True),
            "image": ev.get("image"),
            "stop_at": ev.get("stop_at"),
            "min_seats": ev.get("min_seats", 2),
            "zones": tiers,
            "sites": sorted(ev.get("sources", {})),
            "prices": prices,
        })

    return {"running": watcher_pid() is not None, "events": events,
            "ntfy_topic": os.environ.get("NTFY_TOPIC")
                          or _load_config_raw().get("ntfy_topic", "")}


# ------------------------------------------------------------------ API logic

def api_search(q: str) -> dict:
    results = discover.run_searches(q)
    with _cache_lock:
        _last_search["query"], _last_search["results"] = q, results
    anchors = discover.upcoming_anchors(results)
    return {"query": q, "events": [h.to_dict() for h in anchors]}


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
        time_known=raw.get("time_known", True),
    )
    sources, matches = discover.match_sources(anchor, results)
    zones = discover.snapshot_zones(sources)
    image = (
        anchor.ids.get("image")
        or (sources.get("gametime") or {}).get("image")
        or itunes_artwork(anchor.name)
    )
    return {
        "anchor": anchor.to_dict(),
        "sources": sources,
        "image": image,
        "matches": {site: h.to_dict() for site, h in matches.items()},
        "zones": [
            {"zone": z, "label": watcher.zone_label(z),
             "min": round(p, 2), "currency": cur}
            for z, (p, cur) in sorted(zones.items(), key=lambda kv: kv[1][0])
            if z != "Other"
        ],
    }


def api_save(body: dict) -> dict:
    anchor = body["anchor"]
    dt = datetime.fromisoformat(anchor["dt"])
    zones = {
        z: {"deal": float(t["deal"]), "steal": float(t["steal"]),
            "currency": t.get("currency", "USD")}
        for z, t in body.get("zones", {}).items()
    }
    if not zones:
        return {"ok": False, "error": "Set thresholds for at least one zone."}
    # the image travels in gametime's ids during matching; it's not a source key
    sources = {
        site: {k: v for k, v in ids.items() if k != "image"}
        for site, ids in body["sources"].items()
    }
    # StubHub-only anchors carry a viewer-timezone-shifted time; keep the
    # date but don't present the time as fact
    time_known = anchor.get("time_known", True)
    when = f"{dt:%a %b %-d, %-I:%M %p}" if time_known else f"{dt:%a %b %-d}"
    ev = {
        "id": f"{anchor['name']}|{anchor['dt']}",
        "event": f"{anchor['name']} @ {anchor['venue']} ({when})",
        "name": anchor["name"],
        "venue": anchor["venue"],
        "dt": anchor["dt"],
        "time_known": time_known,
        "image": body.get("image"),
        "stop_at": (dt + timedelta(minutes=90)).isoformat(timespec="seconds"),
        "min_seats": int(body.get("min_seats", 2)),
        "zones": zones,
        "sources": sources,
    }
    events = [e for e in load_events() if event_id(e) != ev["id"]] + [ev]
    save_events(events)
    if body.get("start"):
        stop_watcher(wait_s=5)  # replace any watcher running on the old config
        start_watcher()
    else:
        _restart_watcher_if_running()
    return {"ok": True, "event": ev["event"], "id": ev["id"]}


def api_remove(body: dict) -> dict:
    events = [e for e in load_events() if event_id(e) != body.get("id")]
    save_events(events)
    if not events:
        stop_watcher()
    else:
        _restart_watcher_if_running()
    return {"ok": True, "remaining": len(events)}


# ------------------------------------------------------------------ phone push

def api_ntfy_save(body: dict) -> dict:
    """Persist the ntfy topic in config.json (env var NTFY_TOPIC still wins
    for launchd setups). Empty topic = disable phone push."""
    topic = (body.get("topic") or "").strip()
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", topic):
        return {"ok": False,
                "error": "Topic must be 8-64 letters, digits, - or _."}
    cfg: dict = {"events": load_events()}
    if topic:
        cfg["ntfy_topic"] = topic
    (HERE / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    _restart_watcher_if_running()   # watcher reads the topic at startup
    return {"ok": True, "topic": topic}


def api_ntfy_test(body: dict) -> dict:
    """Fire a test push so the phone setup can be verified end to end."""
    topic = (body.get("topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "No topic set."}
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data="If you can read this, steal alerts will reach your phone.".encode(),
        headers={"Title": "Doors test notification", "Tags": "door",
                 "Priority": "default"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"ntfy.sh unreachable: {type(e).__name__}"}


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
            "/api/event/remove": api_remove,
            "/api/ntfy/save": api_ntfy_save,
            "/api/ntfy/test": api_ntfy_test,
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
    print(f"Doors → http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
