#!/usr/bin/env python3
"""Last-minute ticket price-drop watcher.

Polls resale marketplaces for the events in config.json (add shows via the
web UI — ui.py — or discover.py) and sends tiered alerts when prices in
watched zones cross your thresholds. Built for the final hours before a
show, when prices move fast; polls politely (randomized ~15s cadence, one
lightweight request per source per event) and never automates purchases —
alerts deep-link to the marketplace's own buy page.

Architecture (three layers):
  sources   — scrapers returning {zone: Quote} (StubHub, Vivid, Gametime)
              plus event-level context minimums (TickPick, SeatGeek)
  engine    — tiered alert rules with cooldowns + session-low tracking,
              one engine per tracked event
  notifier  — macOS (terminal-notifier, clickable) + ntfy.sh phone push

Alert tiers (all-in prices, min_seats+ together):
  DEAL   🔥  price at/below the zone's "deal" threshold
  STEAL  🚨  price at/below the zone's "steal" threshold (auto-opens buy page)
  DROP   📉  watched zone hits a new session low >= DROP_ALERT_PCT below last poll

Usage:
    .venv/bin/python ui.py                      # web UI: search & track shows
    .venv/bin/python watcher.py                 # watch them (--once for a single check)
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

# ==================================================== behavior (event-agnostic)

POLL_SECONDS = 15
POLL_JITTER_S = 4             # randomized cadence; regular polling is a bot tell
CONTEXT_EVERY = 4             # TickPick/SeatGeek context every N cycles
SOURCE_FAIL_NOTIFY = 6        # consecutive failures before "source down" ping
DROP_ALERT_PCT = 5.0
REALERT_COOLDOWN_S = 900      # repeat a tier alert only after 15 min…
REALERT_DELTA = 5.0           # …or a further $5 drop
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")   # empty = phone push disabled;
                                                # use a long random topic name
SHOW_DIALOGS = False          # popup dialog windows for steal/system alerts
ZONE_LABELS = {"Lower": "100s", "Pit General Admission": "Pit GA"}

VIVID_LISTINGS_API = "https://www.vividseats.com/hermes/api/v1/listings?productionId={}"
GAMETIME_LISTINGS_API = "https://mobile.gametime.co/v1/listings?event_id={}&quantity={}"


# ======================================================== event config (json)

@dataclass(frozen=True)
class Config:
    """One event to watch. Written by the UI (or discover.py); safe to hand-edit."""
    event: str
    stop_at: datetime                          # stop watching this event here
    min_seats: int                             # only alert on N+ together
    zone_tiers: dict[str, dict]                # zone -> {deal, steal, currency}
    sources: dict[str, dict]                   # per-marketplace ids/urls


def _parse_tiers(zones: dict) -> dict[str, dict]:
    # accept legacy key names (good/screaming) from older configs;
    # "currency" records which currency the thresholds were entered in
    return {
        zone: {
            "deal": float(t.get("deal", t.get("good"))),
            "steal": float(t.get("steal", t.get("screaming"))),
            "currency": t.get("currency", "USD"),
        }
        for zone, t in zones.items()
    }


def _parse_event(raw: dict, path: Path) -> Config:
    missing = {"event", "stop_at", "zones", "sources"} - raw.keys()
    if missing:
        raise SystemExit(f"{path}: event missing keys: {', '.join(sorted(missing))}")
    return Config(
        event=raw["event"],
        stop_at=datetime.fromisoformat(raw["stop_at"]),
        min_seats=int(raw.get("min_seats", 2)),
        zone_tiers=_parse_tiers(raw["zones"]),
        sources=raw["sources"],
    )


def load_configs(path: Path) -> list[Config]:
    """All tracked events. Accepts {"events": [...]} or a legacy single-event
    object, so old configs keep working."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(
            f"No config at {path} — add a show via the web UI (ui.py), "
            f"or copy config.example.json"
        )
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")
    global NTFY_TOPIC
    # env var wins (launchd plist setups); otherwise the topic saved by the UI
    NTFY_TOPIC = NTFY_TOPIC or raw.get("ntfy_topic", "")
    events = raw["events"] if "events" in raw else [raw]
    if not events:
        raise SystemExit(f"{path} has no events — add a show via the web UI")
    return [_parse_event(e, path) for e in events]


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
    price: float          # all-in, in `currency`
    section: str
    listings: int
    pre_fee: float | None = None      # est. price shown in list views w/o fees
    currency: str = "USD"             # StubHub .ie reports intl events in local currency


CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€",
                    "CAD": "CA$", "AUD": "AU$", "MXN": "MX$"}


def cur_sym(code: str) -> str:
    return CURRENCY_SYMBOLS.get(code, code + " ")


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
    cur = re.search(r'"currencyCode":"([A-Z]{3})"', text)
    currency = cur.group(1) if cur else "USD"
    zones: dict[str, Quote] = {}
    for m in SECTION_STATS_RE.finditer(text):
        section, price, listings, zone = (
            m.group(1), float(m.group(2)), int(m.group(3)), m.group(4)
        )
        q = zones.get(zone)
        if q is None:
            zones[zone] = Quote(price, section, listings, currency=currency)
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


def fetch_stubhub_zones(url: str) -> dict[str, Quote]:
    return parse_stubhub_zones(_get(url).text)


def zone_from_section(section_name: str) -> str:
    """Normalize a marketplace section/group name to a zone key."""
    s = section_name.lower()
    if "pit" in s:
        return "Pit General Admission"
    if s.startswith("floor") or s.startswith("gafl") or "ga floor" in s:
        return "Floor"
    if "lower" in s:
        return "Lower"
    if "upper" in s:
        return "Upper"
    return "Other"


def parse_vivid_zones(payload: dict, min_seats: int) -> dict[str, Quote]:
    """Per-zone minimums from the hermes listings API ('aip' = all-in price).
    Only listings with quantity >= min_seats are considered."""
    zones: dict[str, Quote] = {}
    for t in payload.get("tickets", []):
        try:
            price, qty = float(t["aip"]), int(t.get("q", "0"))
        except (KeyError, ValueError):
            continue
        if qty < min_seats:
            continue
        zone = zone_from_section(t.get("s", ""))
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


def parse_gametime_zones(payload: dict) -> dict[str, Quote]:
    """Per-zone minimums from Gametime's listings API. Prices arrive in cents,
    all-in; the API itself filters by requested quantity, so listings here
    are pair-verified."""
    zones: dict[str, Quote] = {}
    for t in payload.get("listings", []):
        try:
            price = t["price"]["total"] / 100
            section = t["section"]
        except (KeyError, TypeError):
            continue
        zone = zone_from_section(t.get("section_group") or section)
        q = zones.get(zone)
        if q is None:
            zones[zone] = Quote(price, section, 1)
        else:
            q.listings += 1
            if price < q.price:
                q.price, q.section = price, section
    if not zones:
        raise ValueError("no listings parsed from Gametime API")
    return zones


def fetch_vivid_zones(production_id, min_seats: int) -> dict[str, Quote]:
    return parse_vivid_zones(
        _get(VIVID_LISTINGS_API.format(production_id)).json(), min_seats
    )


def fetch_gametime_zones(event_id: str, min_seats: int) -> dict[str, Quote]:
    return parse_gametime_zones(
        _get(GAMETIME_LISTINGS_API.format(event_id, min_seats)).json()
    )


def fetch_tickpick_min(url: str, event_id: str) -> float:
    m = re.search(
        r'stats\\?":\{\\?"event_id\\?":\\?"' + re.escape(str(event_id))
        + r'\\?",\\?"count\\?":(\d+),'
        r'\\?"max\\?":([\d.]+),\\?"min\\?":([\d.]+)',
        _get(url).text,
    )
    if not m:
        raise ValueError("stats not found in TickPick page")
    return float(m.group(3))


def fetch_seatgeek_min(url: str, event_id: str) -> float:
    text = _get(url).text
    # the event id appears many times (canonical links, og tags) with no data
    # nearby; find the occurrence that actually has stats after it
    for m in re.finditer(re.escape(f"/concert/{event_id}"), text):
        stats = re.search(r'"lowest_price":([\d.]+)', text[m.end(): m.end() + 3000])
        if stats:
            return float(stats.group(1))
    raise ValueError("lowest_price not found for SeatGeek event")


def build_sources(cfg: Config) -> tuple[list[Source], list[tuple[str, Callable[[], float]]]]:
    """Instantiate only the sources present in the config. Section sources
    drive zone alerts; context sources are event-level minimums for logging."""
    s = cfg.sources
    section: list[Source] = []
    context: list[tuple[str, Callable[[], float]]] = []
    if "gametime" in s:
        section.append(Source(
            "Gametime",
            lambda c=s["gametime"]: fetch_gametime_zones(c["event_id"], cfg.min_seats),
            s["gametime"]["buy_url"], pair_verified=True,
        ))
    if "vivid" in s:
        section.append(Source(
            "Vivid",
            lambda c=s["vivid"]: fetch_vivid_zones(c["production_id"], cfg.min_seats),
            s["vivid"]["buy_url"], pair_verified=True,
        ))
    if "stubhub" in s:
        # scrape the bot-tolerant .ie storefront; prices there are all-in
        section.append(Source(
            "StubHub",
            lambda c=s["stubhub"]: fetch_stubhub_zones(c["url"]),
            s["stubhub"].get("buy_url") or s["stubhub"]["url"],
            pair_verified=False,
        ))
    if "tickpick" in s:
        context.append((
            "TickPick",
            lambda c=s["tickpick"]: fetch_tickpick_min(c["url"], c["event_id"]),
        ))
    if "seatgeek" in s:
        context.append((
            "SeatGeek",
            lambda c=s["seatgeek"]: fetch_seatgeek_min(c["url"], c["event_id"]),
        ))
    if not section:
        raise SystemExit("config has no section sources (gametime/vivid/stubhub)")
    return section, context


# ================================================================ notifier

@dataclass(frozen=True)
class Style:
    emoji: str
    sound: str            # macOS sound name
    ntfy_priority: str    # min/low/default/high/urgent
    ntfy_tags: str        # emoji shortcodes shown on phone


STYLES = {
    "steal": Style("🚨", "Sosumi", "urgent", "rotating_light,tickets"),
    "deal": Style("🔥", "Glass", "high", "fire,tickets"),
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
    """One alert everywhere: Mac banner + phone push for everything;
    dialog + voice are reserved for drop-everything moments."""
    style = STYLES[kind]
    _reap_children()
    _mac_notify(f"{style.emoji} {title}", subtitle, body, style.sound, url)
    if SHOW_DIALOGS and kind in ("steal", "system"):
        # banners require notification permission the user may not have
        # granted; a dialog window always shows
        _mac_dialog(f"{style.emoji} {title}", f"{subtitle}\n\n{body}", url)
    if kind == "steal":
        _children.append(subprocess.Popen(
            ["say", "-v", "Samantha", spoken],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    _ntfy_push(title, f"{subtitle}\n{body}" if body else subtitle,
               style, url, alt_url)


# ================================================================== engine

def load_state(cfgs: list[Config]) -> dict:
    """Prior state for the tracked events, keyed per event so session lows
    and cooldowns never bleed across concerts. Untracked events' state is
    dropped; a legacy single-event file migrates transparently."""
    tracked = {c.event for c in cfgs}
    state: dict = {"events": {}}
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text())
            if "_event" in raw:                       # legacy single-event shape
                if raw["_event"] in tracked:
                    state["events"][raw["_event"]] = {
                        k: v for k, v in raw.items() if not k.startswith("_")
                    }
            else:
                for event, es in (raw.get("events") or {}).items():
                    if event in tracked and isinstance(es, dict):
                        state["events"][event] = es
                    elif event not in tracked:
                        log(f"dropping state for {event!r} (no longer tracked)")
        except (json.JSONDecodeError, OSError) as e:
            log(f"state.json unreadable ({e}); starting fresh")
    for c in cfgs:
        state["events"].setdefault(c.event, {})
    return state


def save_state(state: dict) -> None:
    # atomic write: a kill mid-write must not corrupt the state file
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


class Engine:
    """Evaluates one event's quotes against tier/momentum rules and fires
    notifications. `state` is this event's slice of the shared state file."""

    def __init__(self, cfg: Config, state: dict):
        self.cfg = cfg
        self.state = state
        self.tag = cfg.event.split(" @ ")[0][:24]
        self.fail_counts: dict[str, int] = {}
        self.section_sources, self.context_sources = build_sources(cfg)
        tp = cfg.sources.get("tickpick")
        self.compare_url: str | None = tp["url"] if tp else None

    # ---------------------------------------------------------- evaluation

    def _tier_hit(self, zs: dict, price: float, tiers: dict, now: float) -> str | None:
        """Deepest qualifying tier that is off cooldown, else None."""
        for tier in ("steal", "deal"):
            if price > tiers[tier]:
                continue
            t = zs.get(f"tier_{tier}", {})
            fresh = (now - t.get("ts", 0)) > REALERT_COOLDOWN_S
            lower = t.get("price") is None or price <= t["price"] - REALERT_DELTA
            return tier if (fresh or lower) else None
        return None

    def evaluate(self, src: Source, zone: str, q: Quote, quiet: bool) -> None:
        now = time.time()
        tiers = self.cfg.zone_tiers[zone]
        if q.currency != tiers.get("currency", "USD"):
            # threshold was entered in a different currency; comparing (or
            # tracking session lows) across currencies would be meaningless
            return
        key = f"{src.name}:{zone}"
        zs = self.state.get(key, {})
        prev = zs.get("min")
        old_floor = zs.get("floor")
        is_new_low = old_floor is None or q.price < old_floor
        floor = q.price if is_new_low else old_floor

        tier = self._tier_hit(zs, q.price, tiers, now)
        # momentum only on a genuine session low, so listing churn can't ping us
        drop_pct = ((prev - q.price) / prev * 100) if prev else 0.0
        momentum = is_new_low and drop_pct >= DROP_ALERT_PCT

        if (tier or momentum) and not quiet:
            # stamp cooldown only when the alert actually goes out; a quiet
            # --once run must never suppress a later real alert
            if tier:
                zs[f"tier_{tier}"] = {"price": q.price, "ts": now}
            self._fire(src, zone, q, tier, momentum, drop_pct, prev)

        zs.update(
            min=q.price, floor=floor, section=q.section,
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        self.state[key] = zs

    # ---------------------------------------------------------- alerting

    def _fire(self, src: Source, zone: str, q: Quote, tier: str | None,
              momentum: bool, drop_pct: float, prev: float | None) -> None:
        label = zone_label(zone)
        kind = tier or "drop"
        threshold = self.cfg.zone_tiers[zone].get(tier) if tier else None
        s = cur_sym(q.currency)

        # title = severity + artist; subtitle = what you'd actually buy;
        # body = at most two facts that earn their place, plus any warning
        headline = {"steal": "Steal", "deal": "Deal", "drop": "Price drop"}[kind]
        title = f"{headline} — {self.tag}"
        subtitle = f"{label} {s}{q.price:.0f} on {src.name}"

        facts = []
        if threshold:
            facts.append(f"Under your {s}{threshold:.0f} target")
        if momentum and prev:
            facts.append(f"↓{drop_pct:.0f}% from {s}{prev:.0f}")
        if q.pre_fee:
            # StubHub list views hide fees; without this hint the alert price
            # is nowhere to be found on their page
            facts.append(f"listed as ~{s}{q.pre_fee:.0f}")
        parts = facts[:2]
        if not src.pair_verified:
            parts.append(f"⚠ single seats — verify {self.cfg.min_seats} together")
        body = " · ".join(parts)

        unit = "dollars" if q.currency == "USD" else q.currency
        spoken = f"Steal alert! {label} at {q.price:.0f} {unit} on {src.name}"

        notify(kind, title, subtitle, body, spoken,
               url=src.buy_url, alt_url=self.compare_url)
        if kind == "steal":
            # no time to click — open the buy page immediately
            subprocess.run(["open", src.buy_url], check=False)
        log(f"ALERT [{kind}] {self.tag} {src.name} {label}: {s}{q.price:.0f} ({q.section}) "
            + (f"↓{drop_pct:.0f}% " if momentum else "")
            + (f"<= {s}{threshold:.0f}" if threshold else ""))

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
            for name, fn in self.context_sources:
                try:
                    context.append(f"{name} min ${fn():.0f} (any qty)")
                except Exception as e:
                    context.append(f"{name} err:{type(e).__name__}")
        if context:
            log(f"[{self.tag}] Context  " + " | ".join(context))

        for src in self.section_sources:
            try:
                zones = src.fetch()
                self.fail_counts[src.name] = 0
            except Exception as e:
                self._source_failed(src, e, quiet)
                continue

            log(f"[{self.tag}] {src.name:9s}" + " | ".join(
                f"{zone_label(z)}: {cur_sym(q.currency)}{q.price:.0f} ({q.section}, {q.listings} lst)"
                for z, q in sorted(zones.items(), key=lambda kv: kv[1].price)
            ))
            for zone, q in zones.items():
                if zone in self.cfg.zone_tiers:
                    self.evaluate(src, zone, q, quiet)


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
    ap.add_argument("--config", type=Path, default=HERE / "config.json")
    args = ap.parse_args()

    # if the process dies from a native fault (C extension), capture a traceback
    _crash_fh = open(CRASH_LOG, "a")
    faulthandler.enable(file=_crash_fh)

    cfgs = load_configs(args.config)
    state = load_state(cfgs)
    engines = [Engine(c, state["events"][c.event]) for c in cfgs]
    if args.once:
        for eng in engines:
            eng.check_once(quiet=True)
        save_state(state)
        return

    _acquire_single_instance()
    _announce_if_restart()
    threading.Thread(target=_watchdog, daemon=True).start()

    for eng in engines:
        sources = "+".join(s.name for s in eng.section_sources)
        tiers_desc = "; ".join(
            f"{zone_label(z)} deal<={cur_sym(t['currency'])}{t['deal']:.0f} "
            f"steal<={cur_sym(t['currency'])}{t['steal']:.0f}"
            for z, t in eng.cfg.zone_tiers.items()
        )
        log(f"Watching: {eng.cfg.event} — {eng.cfg.min_seats}+ seats together "
            f"via {sources}. {tiers_desc}. Auto-stop {eng.cfg.stop_at:%a %H:%M}.")
    log(
        f"{len(engines)} event(s), pid {os.getpid()}. New-low drops "
        f">={DROP_ALERT_PCT:.0f}%, ~{args.interval}s cadence, context every "
        f"{CONTEXT_EVERY} cycles. "
        f"Phone: {'ntfy.sh/' + NTFY_TOPIC if NTFY_TOPIC else 'off'}."
    )

    cycle = 0
    last_stop = max(c.stop_at for c in cfgs)
    while datetime.now() < last_stop:
        for eng in engines:
            if datetime.now() >= eng.cfg.stop_at:
                continue
            try:
                eng.check_once(with_context=(cycle % CONTEXT_EVERY == 0))
            except Exception as e:
                log(f"[{eng.tag}] loop error: {type(e).__name__}: {e}")
        save_state(state)
        _last_cycle = time.time()
        HEARTBEAT_FILE.touch()
        cycle += 1
        time.sleep(max(5, args.interval + random.uniform(-POLL_JITTER_S, POLL_JITTER_S)))
    # the ONLY intended clean exit; launchd (SuccessfulExit=false) stays stopped
    HEARTBEAT_FILE.unlink(missing_ok=True)
    log("All shows have started/ended — stopping watcher.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        # any unhandled failure must exit non-zero so launchd restarts us
        log(f"FATAL: {type(e).__name__}: {e}")
        raise SystemExit(1)
