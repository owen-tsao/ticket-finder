#!/usr/bin/env python3
"""Last-minute ticket price-drop watcher.

Polls resale marketplaces for a single event and sends tiered alerts when
prices in watched zones cross your thresholds. Built for personal use in the
final hours before a show, when prices move fast; polls politely (randomized
~15s cadence, one lightweight request per source) and never automates
purchases — alerts deep-link to the marketplace's own buy page.

Architecture (single file, three layers):
  sources   — scrapers returning {zone: Quote} (StubHub, Vivid) plus
              event-level context minimums (TickPick, SeatGeek)
  engine    — tiered alert rules with cooldowns + session-low tracking
  notifier  — macOS (terminal-notifier, clickable) + ntfy.sh phone push

Alert tiers (all-in prices, MIN_SEATS+ seats together):
  GOOD   🔥  price at/below the zone's "good" threshold
  SCREAM 🚨  price at/below the zone's "screaming" threshold (auto-opens buy page)
  DROP   📉  watched zone hits a new session low >= DROP_ALERT_PCT below last poll

The config block below is set up for an example event (Daniel Caesar @ Chase
Center, Aug 2026); point the URLs/IDs and ZONE_TIERS at your own event.

Usage:
    .venv/bin/python watcher.py            # foreground loop
    .venv/bin/python watcher.py --once     # single quiet check
    # production: run under launchd (see ticket-watcher.plist.example)
"""

import argparse
import faulthandler
import fcntl
import json
import os
import random
import re
import subprocess
import threading
import urllib.request
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from curl_cffi import requests

# ================================================================= config

EVENT_NAME = "Daniel Caesar @ Chase Center (Fri Aug 21, 7:30 PM)"
MIN_SEATS = 2                 # only alert on listings buyable as N+ together
POLL_SECONDS = 15
POLL_JITTER_S = 4             # randomized cadence; regular polling is a bot tell
CONTEXT_EVERY = 4             # TickPick/SeatGeek context every N cycles
SHOW_END_ISO = "2026-08-21T21:00:00"
SOURCE_FAIL_NOTIFY = 6        # consecutive failures before "source down" ping
DROP_ALERT_PCT = 5.0
REALERT_COOLDOWN_S = 900      # repeat a tier alert only after 15 min…
REALERT_DELTA = 5.0           # …or a further $5 drop
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")   # empty = phone push disabled;
                                                # use a long random topic name
SHOW_DIALOGS = False          # popup dialog windows for good/screaming/system

# zone -> tier thresholds ($ all-in). Pit GA counts as floor.
ZONE_TIERS = {
    "Floor": {"good": 270.0, "screaming": 200.0},
    "Pit General Admission": {"good": 270.0, "screaming": 200.0},
    "Lower": {"good": 200.0, "screaming": 150.0},   # 100-level sections
}
ZONE_LABELS = {"Lower": "100s", "Pit General Admission": "Pit GA"}

TICKPICK_EVENT_ID = "7855144"
TICKPICK_URL = (
    "https://www.tickpick.com/buy-daniel-caesar-tickets-"
    f"chase-center-8-21-26-7pm/{TICKPICK_EVENT_ID}/"
)
SEATGEEK_ARTIST_URL = "https://seatgeek.com/daniel-caesar-tickets"
SEATGEEK_EVENT_ID = "18162225"
STUBHUB_URL = (
    "https://www.stubhub.ie/daniel-caesar-san-francisco-tickets-"
    "8-21-2026/event/107202109/"
)
# Scrape the .ie storefront (bot-tolerant, all-in prices); buy on .com (the
# US catalog uses a different event id — 160818773 — and deep-links to the
# StubHub app on mobile). .com list view shows pre-fee prices; alerts include
# the pre-fee estimate so the numbers are findable.
STUBHUB_BUY_URL = (
    "https://www.stubhub.com/daniel-caesar-san-francisco-tickets-"
    "8-21-2026/event/160818773/?quantity=2"
)
VIVID_API_URL = "https://www.vividseats.com/hermes/api/v1/listings?productionId=6867163"
VIVID_BUY_URL = (
    "https://www.vividseats.com/daniel-caesar-tickets-san-francisco-"
    "chase-center-8-21-2026/production/6867163"
)

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watcher.log"
CRASH_LOG = HERE / "crash.log"
LOCK_FILE = HERE / ".watcher.lock"
HEARTBEAT_FILE = HERE / ".heartbeat"
WATCHDOG_STALL_S = 300        # force-restart if no completed cycle in 5 min
RESTART_DETECT_S = 600        # heartbeat younger than this at boot = restart
TERMINAL_NOTIFIER = "/opt/homebrew/bin/terminal-notifier"


def zone_label(zone: str) -> str:
    return ZONE_LABELS.get(zone, zone)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ================================================================= sources

@dataclass
class Quote:
    """Cheapest purchasable option in a zone."""
    price: float          # all-in, USD
    section: str
    listings: int
    pre_fee: float | None = None      # est. price shown in list views w/o fees


@dataclass(frozen=True)
class Source:
    name: str
    fetch: Callable[[], dict[str, Quote]]
    buy_url: str
    pair_verified: bool           # True if MIN_SEATS quantity is confirmed


# StubHub embeds one JSON object per section; keep all fields within a single
# object (no braces/brackets between) so a missing field can't mis-pair sections
SECTION_STATS_RE = re.compile(
    r'"sectionName":"([^"]+)","minTicketPrice":([\d.]+),'
    r'[^{}\[\]]*?"totalListings":(\d+),"zoneId":\d+,"zoneName":"([^"]+)"'
)


def _get(url: str) -> "requests.Response":
    r = requests.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    return r


def parse_stubhub_zones(text: str) -> dict[str, Quote]:
    """Per-zone minimums from section stats embedded in the event page.
    Section stats cannot verify quantity, so minimums may be single seats."""
    zones: dict[str, Quote] = {}
    for m in SECTION_STATS_RE.finditer(text):
        section, price, listings, zone = (
            m.group(1), float(m.group(2)), int(m.group(3)), m.group(4)
        )
        q = zones.get(zone)
        if q is None:
            zones[zone] = Quote(price, section, listings)
        else:
            q.listings += listings
            if price < q.price:
                q.price, q.section = price, section
    if not zones:
        raise ValueError("no section stats parsed from StubHub page")
    # event-level fee factor lets alerts show the pre-fee price users see in
    # list views (all-in $159 ≈ $124 listed — otherwise "can't find the ticket")
    fee = re.search(r'"minPrice":([\d.]+),"maxPrice":[\d.]+,"minListPrice":([\d.]+)', text)
    if fee and float(fee.group(1)) > 0:
        factor = float(fee.group(2)) / float(fee.group(1))
        for q in zones.values():
            q.pre_fee = q.price * factor
    return zones


def fetch_stubhub_zones() -> dict[str, Quote]:
    return parse_stubhub_zones(_get(STUBHUB_URL).text)


def _vivid_zone(section_name: str) -> str:
    s = section_name.lower()
    if "pit" in s:
        return "Pit General Admission"
    if s.startswith("floor"):
        return "Floor"
    if "lower" in s:
        return "Lower"
    if "upper" in s:
        return "Upper"
    return "Other"


def parse_vivid_zones(payload: dict) -> dict[str, Quote]:
    """Per-zone minimums from the hermes listings API ('aip' = all-in price).
    Only listings with quantity >= MIN_SEATS are considered."""
    zones: dict[str, Quote] = {}
    for t in payload.get("tickets", []):
        try:
            price, qty = float(t["aip"]), int(t.get("q", "0"))
        except (KeyError, ValueError):
            continue
        if qty < MIN_SEATS:
            continue
        zone = _vivid_zone(t.get("s", ""))
        q = zones.get(zone)
        if q is None:
            zones[zone] = Quote(price, t["s"], 1)
        else:
            q.listings += 1
            if price < q.price:
                q.price, q.section = price, t["s"]
    if not zones:
        raise ValueError("no tickets parsed from Vivid hermes API")
    return zones


def fetch_vivid_zones() -> dict[str, Quote]:
    return parse_vivid_zones(_get(VIVID_API_URL).json())


def fetch_tickpick_min() -> float:
    m = re.search(
        r'stats\\?":\{\\?"event_id\\?":\\?"' + re.escape(TICKPICK_EVENT_ID)
        + r'\\?",\\?"count\\?":(\d+),'
        r'\\?"max\\?":([\d.]+),\\?"min\\?":([\d.]+)',
        _get(TICKPICK_URL).text,
    )
    if not m:
        raise ValueError("stats not found in TickPick page")
    return float(m.group(3))


def fetch_seatgeek_min() -> float:
    text = _get(SEATGEEK_ARTIST_URL).text
    idx = text.find(f"/concert/{SEATGEEK_EVENT_ID}")
    if idx == -1:
        raise ValueError("event not found on SeatGeek artist page")
    m = re.search(r'"lowest_price":([\d.]+)', text[idx: idx + 3000])
    if not m:
        raise ValueError("lowest_price not found for SeatGeek event")
    return float(m.group(1))


SECTION_SOURCES = [
    Source("Vivid", fetch_vivid_zones, VIVID_BUY_URL, pair_verified=True),
    Source("StubHub", fetch_stubhub_zones, STUBHUB_BUY_URL, pair_verified=False),
]
CONTEXT_SOURCES = [("TickPick", fetch_tickpick_min), ("SeatGeek", fetch_seatgeek_min)]


# ================================================================ notifier

@dataclass(frozen=True)
class Style:
    emoji: str
    sound: str            # macOS sound name
    ntfy_priority: str    # min/low/default/high/urgent
    ntfy_tags: str        # emoji shortcodes shown on phone


STYLES = {
    "screaming": Style("🚨", "Sosumi", "urgent", "rotating_light,tickets"),
    "good": Style("🔥", "Glass", "high", "fire,tickets"),
    "drop": Style("📉", "Purr", "default", "chart_with_downwards_trend"),
    "system": Style("⚠️", "Basso", "default", "warning"),
}

_children: list[subprocess.Popen] = []


def _reap_children() -> None:
    """Reap finished notifier/speech processes without signal-handler tricks."""
    global _children
    _children = [p for p in _children if p.poll() is None]
    # safety valve: if children pile up (wedged audio/notifier), cull the oldest
    while len(_children) > 15:
        p = _children.pop(0)
        try:
            p.terminate()
        except Exception:
            pass


def _mac_dialog(title: str, body: str, url: str | None) -> None:
    """Guaranteed-visible floating dialog (no notification permission needed).
    Non-blocking; auto-dismisses after 90s. Clicking the buy button opens url."""
    def as_str(s):  # AppleScript can't parse \uXXXX escapes; keep raw UTF-8
        return json.dumps(s, ensure_ascii=False)
    if url:
        script = (
            f'set r to display dialog {as_str(body)} with title {as_str(title)} '
            f'buttons {{"Dismiss", "Open buy page"}} default button 2 '
            f'with icon caution giving up after 90\n'
            f'if button returned of r is "Open buy page" then '
            f'open location {as_str(url)}'
        )
    else:
        script = (
            f'display dialog {as_str(body)} with title {as_str(title)} '
            f'buttons {{"OK"}} default button 1 with icon caution giving up after 90'
        )
    _children.append(subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))


def _mac_notify(title: str, subtitle: str, body: str, sound: str, url: str | None) -> None:
    if Path(TERMINAL_NOTIFIER).exists():
        cmd = [
            TERMINAL_NOTIFIER,
            "-title", title,
            "-subtitle", subtitle,
            "-message", body,
            "-sound", sound,
            "-group", "ticket-watcher",
            "-timeout", "600",
            "-ignoreDnD",
        ]
        if url:
            cmd += ["-open", url]
        # Popen: terminal-notifier blocks awaiting click; never stall the loop
        _children.append(subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
    else:
        def as_str(s):  # AppleScript can't parse \uXXXX escapes; keep raw UTF-8
            return json.dumps(s, ensure_ascii=False)
        script = (
            f'display notification {as_str(body)} with title {as_str(title)} '
            f'subtitle {as_str(subtitle)} sound name {as_str(sound)}'
        )
        subprocess.run(["osascript", "-e", script], check=False)


def _ntfy_push(title: str, body: str, style: Style,
               url: str | None, alt_url: str | None) -> None:
    """Phone push via ntfy.sh (fire-and-forget thread; must never block).
    Tapping the notification opens `url`; action buttons make the
    deal source vs. the fee-free-alternative site unmistakable."""
    if not NTFY_TOPIC:
        return
    def send() -> None:
        try:
            headers = {
                # ntfy headers must be latin-1 safe; emoji arrive via Tags
                "Title": title.encode("ascii", "ignore").decode().strip(),
                "Priority": style.ntfy_priority,
                "Tags": style.ntfy_tags,
            }
            if url:
                headers["Click"] = url
                actions = [f"view, Buy here, {url}"]
                if alt_url:
                    actions.append(f"view, Compare on TickPick, {alt_url}")
                headers["Actions"] = "; ".join(actions)
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode(), headers=headers
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # phone push is best-effort; Mac notification already fired
    threading.Thread(target=send, daemon=True).start()


def notify(kind: str, title: str, subtitle: str, body: str,
           spoken: str, url: str | None = None,
           alt_url: str | None = None) -> None:
    """One alert everywhere: Mac banner + dialog + voice + phone push."""
    style = STYLES[kind]
    _reap_children()
    _mac_notify(f"{style.emoji} {title}", subtitle, body, style.sound, url)
    if SHOW_DIALOGS and kind in ("screaming", "good", "system"):
        # banners require notification permission the user may not have
        # granted; a dialog window always shows
        _mac_dialog(f"{style.emoji} {title}", f"{subtitle}\n\n{body}", url)
    _children.append(subprocess.Popen(
        ["say", "-v", "Samantha", spoken],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))
    _ntfy_push(title, f"{subtitle}\n{body}", style, url, alt_url)


# ================================================================== engine

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log(f"state.json unreadable ({e}); starting fresh")
    return {}


def save_state(state: dict) -> None:
    # atomic write: a kill mid-write must not corrupt the state file
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


class Engine:
    """Evaluates quotes against tier/momentum rules and fires notifications."""

    def __init__(self, state: dict):
        self.state = state
        self.fail_counts: dict[str, int] = {}

    # ---------------------------------------------------------- evaluation

    def _tier_hit(self, zs: dict, price: float, tiers: dict, now: float) -> str | None:
        """Deepest qualifying tier that is off cooldown, else None."""
        for tier in ("screaming", "good"):
            if price > tiers[tier]:
                continue
            t = zs.get(f"tier_{tier}", {})
            fresh = (now - t.get("ts", 0)) > REALERT_COOLDOWN_S
            lower = t.get("price") is None or price <= t["price"] - REALERT_DELTA
            return tier if (fresh or lower) else None
        return None

    def evaluate(self, src: Source, zone: str, q: Quote, quiet: bool) -> None:
        now = time.time()
        key = f"{src.name}:{zone}"
        zs = self.state.get(key, {})
        prev = zs.get("min")
        old_floor = zs.get("floor")
        is_new_low = old_floor is None or q.price < old_floor
        floor = q.price if is_new_low else old_floor
        tiers = ZONE_TIERS[zone]

        tier = self._tier_hit(zs, q.price, tiers, now)
        # momentum only on a genuine session low, so listing churn can't ping us
        drop_pct = ((prev - q.price) / prev * 100) if prev else 0.0
        momentum = is_new_low and drop_pct >= DROP_ALERT_PCT

        if (tier or momentum) and not quiet:
            # stamp cooldown only when the alert actually goes out; a quiet
            # --once run must never suppress a later real alert
            if tier:
                zs[f"tier_{tier}"] = {"price": q.price, "ts": now}
            self._fire(src, zone, q, tier, momentum, drop_pct, prev, floor)

        zs.update(
            min=q.price, floor=floor, section=q.section,
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        self.state[key] = zs

    # ---------------------------------------------------------- alerting

    def _fire(self, src: Source, zone: str, q: Quote, tier: str | None,
              momentum: bool, drop_pct: float, prev: float | None,
              floor: float) -> None:
        label = zone_label(zone)
        kind = tier or "drop"
        threshold = ZONE_TIERS[zone].get(tier) if tier else None

        headline = {
            "screaming": "SCREAMING DEAL",
            "good": "Good deal",
            "drop": "Price drop",
        }[kind]
        title = f"{label} ${q.price:.0f} on {src.name} — {headline}"

        facts = [f"{q.section}"]
        if src.pair_verified:
            facts += [f"{q.listings} pair listings", f"{MIN_SEATS} together ✓"]
        else:
            facts += [f"{q.listings} listings", f"⚠ verify {MIN_SEATS} seats"]
        subtitle = " · ".join(facts)

        parts = []
        if momentum and prev:
            parts.append(f"↓{drop_pct:.0f}% (${prev:.0f} → ${q.price:.0f})")
        if threshold:
            parts.append(f"under your ${threshold:.0f} target")
        if q.pre_fee:
            parts.append(f"shows as ~${q.pre_fee:.0f} in list view (pre-fee)")
        parts.append(f"session low ${floor:.0f}")
        body = f"This deal is on {src.name} — tap to open. " + " · ".join(parts)

        spoken = {
            "screaming": f"Screaming deal! {label} at {q.price:.0f} dollars on {src.name}",
            "good": f"Good deal: {label} at {q.price:.0f} dollars on {src.name}",
            "drop": f"{label} dropped to {q.price:.0f} dollars on {src.name}",
        }[kind]

        notify(kind, title, subtitle, body, spoken,
               url=src.buy_url, alt_url=TICKPICK_URL)
        if kind == "screaming":
            # no time to click — open the buy page immediately
            subprocess.run(["open", src.buy_url], check=False)
        log(f"ALERT [{kind}] {src.name} {label}: ${q.price:.0f} ({q.section}) "
            + (f"↓{drop_pct:.0f}% " if momentum else "")
            + (f"<= ${threshold:.0f}" if threshold else ""))

    # ---------------------------------------------------------- polling

    def _source_failed(self, src: Source, err: Exception, quiet: bool) -> None:
        log(f"{src.name}: ERROR {type(err).__name__}: {str(err)[:120]}")
        self.fail_counts[src.name] = self.fail_counts.get(src.name, 0) + 1
        if self.fail_counts[src.name] == SOURCE_FAIL_NOTIFY and not quiet:
            notify(
                "system",
                f"{src.name} unreachable",
                f"{SOURCE_FAIL_NOTIFY} consecutive failures",
                "Price coverage degraded — check watcher.log.",
                spoken=f"Warning: {src.name} source is down",
            )

    def check_once(self, quiet: bool = False, with_context: bool = True) -> None:
        context = []
        if with_context:
            for name, fn in CONTEXT_SOURCES:
                try:
                    context.append(f"{name} min ${fn():.0f} (any qty)")
                except Exception as e:
                    context.append(f"{name} err:{type(e).__name__}")
        if context:
            log("Context  " + " | ".join(context))

        for src in SECTION_SOURCES:
            try:
                zones = src.fetch()
                self.fail_counts[src.name] = 0
            except Exception as e:
                self._source_failed(src, e, quiet)
                continue

            log(f"{src.name:8s}" + " | ".join(
                f"{zone_label(z)}: ${q.price:.0f} ({q.section}, {q.listings} lst)"
                for z, q in sorted(zones.items(), key=lambda kv: kv[1].price)
            ))
            for zone, q in zones.items():
                if zone in ZONE_TIERS:
                    self.evaluate(src, zone, q, quiet)

        save_state(self.state)


# ==================================================================== main

# faulthandler needs the file object to stay alive for the process lifetime;
# if GC'd, its fd closes and a crash traceback would write into whatever
# file happens to reuse that descriptor
_crash_fh = None
_lock_fh = None
_last_cycle = time.time()


def _acquire_single_instance() -> None:
    """Exit cleanly (no launchd restart) if another watcher already runs.
    The OS releases the flock automatically when the process dies."""
    global _lock_fh
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except OSError:
        log("another watcher instance holds the lock — exiting")
        raise SystemExit(0)


def _watchdog() -> None:
    """Hard-restart if the main loop stalls (e.g. wedged native HTTP call).
    os._exit skips cleanup on purpose: a stalled process can't be trusted
    to unwind; the non-zero code makes launchd respawn us immediately."""
    while True:
        time.sleep(30)
        if time.time() - _last_cycle > WATCHDOG_STALL_S:
            log(f"WATCHDOG: no completed cycle in {WATCHDOG_STALL_S}s — forcing restart")
            os._exit(70)


def _announce_if_restart() -> None:
    """If the heartbeat is fresh, a previous instance died recently —
    tell the user coverage blipped instead of failing silently."""
    try:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    except OSError:
        return
    if age < RESTART_DETECT_S:
        log(f"detected unclean predecessor (heartbeat {age:.0f}s old) — auto-restarted")
        notify(
            "system",
            "Watcher auto-restarted",
            f"Previous instance died ~{age:.0f}s ago",
            "launchd brought it back automatically — coverage restored.",
            spoken="Ticket watcher restarted itself",
        )


def main() -> None:
    global _crash_fh, _last_cycle
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    args = ap.parse_args()

    # if the process dies from a native fault (C extension), capture a traceback
    _crash_fh = open(CRASH_LOG, "a")
    faulthandler.enable(file=_crash_fh)

    engine = Engine(load_state())
    if args.once:
        engine.check_once(quiet=True)
        return

    _acquire_single_instance()
    _announce_if_restart()
    threading.Thread(target=_watchdog, daemon=True).start()

    log(f"Watching: {EVENT_NAME} — {MIN_SEATS}+ seats together (pid {os.getpid()})")
    tiers_desc = "; ".join(
        f"{zone_label(z)} good<=${t['good']:.0f} scream<=${t['screaming']:.0f}"
        for z, t in ZONE_TIERS.items()
    )
    log(
        f"Tiers: {tiers_desc}. New-low drops >={DROP_ALERT_PCT:.0f}%. "
        f"Vivid+StubHub every ~{args.interval}s, context every {CONTEXT_EVERY} "
        f"cycles. Phone: {'ntfy.sh/' + NTFY_TOPIC if NTFY_TOPIC else 'off'}. "
        f"Auto-stop {SHOW_END_ISO[11:16]}."
    )

    show_end = datetime.fromisoformat(SHOW_END_ISO)
    cycle = 0
    while datetime.now() < show_end:
        try:
            engine.check_once(with_context=(cycle % CONTEXT_EVERY == 0))
        except Exception as e:
            log(f"loop error: {type(e).__name__}: {e}")
        _last_cycle = time.time()
        HEARTBEAT_FILE.touch()
        cycle += 1
        time.sleep(max(5, args.interval + random.uniform(-POLL_JITTER_S, POLL_JITTER_S)))
    # the ONLY intended clean exit; launchd (SuccessfulExit=false) stays stopped
    HEARTBEAT_FILE.unlink(missing_ok=True)
    log("Show has started/ended — stopping watcher.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        # any unhandled failure must exit non-zero so launchd restarts us
        log(f"FATAL: {type(e).__name__}: {e}")
        raise SystemExit(1)
