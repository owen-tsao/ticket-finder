#!/usr/bin/env python3
"""Find a concert across ticket marketplaces and write config.json for watcher.py.

Usage:
    .venv/bin/python discover.py "weezer san francisco"

Searches Gametime, Vivid Seats, StubHub, TickPick, and SeatGeek; shows the
upcoming shows that match; and after you pick one, matches the same concert
on the other sites (by date + venue), shows the live per-zone minimums, and
prompts for your alert thresholds.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi import requests

import watcher
from watcher import zone_label

UA_GET = lambda url: requests.get(url, impersonate="chrome", timeout=25)


@dataclass
class Hit:
    """One event as seen by one marketplace."""
    site: str
    name: str
    venue: str
    dt: datetime | None
    min_price: float | None
    ids: dict = field(default_factory=dict)   # site-specific ids/urls


def _junescape(s: str) -> str:
    """Decode JSON string escapes (\\u0026 etc.) in regex-extracted text."""
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2}


def looks_like(anchor: Hit, hit: Hit) -> bool:
    """Same concert? Same local date + overlapping venue or artist tokens."""
    if anchor.dt and hit.dt and anchor.dt.date() != hit.dt.date():
        return False
    venue_ok = bool(_tokens(anchor.venue) & _tokens(hit.venue))
    name_ok = bool(_tokens(anchor.name) & _tokens(hit.name))
    return venue_ok and name_ok


# ------------------------------------------------------------------ searchers

def search_gametime(q: str) -> list[Hit]:
    d = UA_GET(f"https://mobile.gametime.co/v1/search?q={q}").json()
    hits = []
    for item in d.get("events", []):
        e = item.get("event", {})
        v = item.get("venue") or {}
        total = (e.get("min_price") or {}).get("total") or 0
        hits.append(Hit(
            "gametime", e.get("name", "?"),
            f"{v.get('name', '?')}, {v.get('city', '?')}",
            datetime.fromisoformat(e["datetime_local"]) if e.get("datetime_local") else None,
            total / 100 or None,
            {"event_id": e.get("id"), "buy_url": e.get("seo_url")},
        ))
    return hits


def search_vivid(q: str) -> list[Hit]:
    t = UA_GET(f"https://www.vividseats.com/search?searchTerm={q}").text
    i = t.find('"initialAllProductionListData"')
    i = t.find('"items":[', i)
    if i == -1:
        return []
    start = i + len('"items":')
    depth = 0
    for j in range(start, len(t)):
        if t[j] == "[":
            depth += 1
        elif t[j] == "]":
            depth -= 1
            if depth == 0:
                break
    items = json.loads(t[start:j + 1])
    hits = []
    for p in items:
        v = p.get("venue", {})
        dt = p.get("localDate", "")
        hits.append(Hit(
            "vivid", p.get("name", "?"),
            f"{v.get('name', '?')}, {v.get('city', '?')}",
            datetime.fromisoformat(dt[:19]) if dt else None,
            p.get("minAipPrice") or None,
            {"production_id": p["id"],
             "buy_url": "https://www.vividseats.com" + (p.get("webPath") or f"/production/{p['id']}")},
        ))
    return hits


def search_stubhub(q: str) -> list[Hit]:
    t = UA_GET(f"https://www.stubhub.ie/find/s/?q={q}").text
    hits = []
    for block in re.split(r'class="Panel Panel-Border EventItem"', t)[1:]:
        ts = re.search(r'data-timestamp="(\d+)"', block)
        link = re.search(r'href="(/[^"]*?/event/(\d+)/)"[^>]*>.*?<div>([^<]+)</div>', block)
        if not (ts and link):
            continue
        venue = re.search(r'VenueInfo[^>]*>(?:<[^>]+>|</[^>]+>)*([^<]{3,})', block)
        hits.append(Hit(
            "stubhub", link.group(3),
            venue.group(1).strip() if venue else "?",
            datetime.fromtimestamp(int(ts.group(1)) / 1000),
            None,
            {"url": "https://www.stubhub.ie" + link.group(1)},
        ))
    return hits


def search_tickpick(q: str) -> list[Hit]:
    # multi-word queries often return performer pages only; the first word
    # (usually the artist) surfaces the embedded event list reliably
    t = UA_GET(f"https://www.tickpick.com/search?q={q}").text.replace('\\"', '"')
    if '"event_id"' not in t and "%20" in q:
        t = UA_GET(
            f"https://www.tickpick.com/search?q={q.split('%20')[0]}"
        ).text.replace('\\"', '"')
    hits = []
    for m in re.finditer(r'"event_id":"(\d+)","event_name":"([^"]+)"', t):
        window = t[m.start(): m.start() + 1200]
        slug = re.search(r'"slug":"(/buy-[^"]+)"', window)
        if not slug:
            continue
        venue = re.search(r'"venue_name":"([^"]+)"', window)
        city = re.search(r'"city":"([^"]+)"', window)
        date = re.search(r'"event_date(?:time)?":"([^"]+)"', window)
        price = re.search(r'"min_price":([\d.]+)', window)
        dt = None
        if date:
            try:
                dt = datetime.fromisoformat(date.group(1)[:19])
            except ValueError:
                pass
        hits.append(Hit(
            "tickpick", _junescape(m.group(2)),
            f"{venue.group(1) if venue else '?'}, {city.group(1) if city else '?'}",
            dt, float(price.group(1)) if price else None,
            {"event_id": m.group(1),
             "url": "https://www.tickpick.com" + slug.group(1)},
        ))
    return hits


def search_seatgeek(q: str) -> list[Hit]:
    t = UA_GET(f"https://seatgeek.com/search?search={q}").text
    hits, seen = [], set()
    # anchor on event objects ("short_title" ... "/concert/<id>" nearby) rather
    # than bare urls; multi-word searches can land on an event page directly,
    # where the head is full of canonical/og urls with no data around them
    for m in re.finditer(r'"short_title":"([^"]+)"', t):
        window = t[max(0, m.start() - 3000): m.start() + 3000]
        url = re.search(r'"url":"(https://seatgeek\.com/[^"]+/concert/(\d+))"', window)
        if not url or url.group(2) in seen:
            continue
        seen.add(url.group(2))
        dt = re.search(r'"datetime_local":"([^"]+)"', window)
        price = re.search(r'"lowest_price":([\d.]+)', window)
        venue = re.search(r'"venue":\{[^{}]*?"name":"([^"]+)"', window)
        hits.append(Hit(
            "seatgeek", m.group(1),
            venue.group(1) if venue else "?",
            datetime.fromisoformat(dt.group(1)[:19]) if dt else None,
            float(price.group(1)) if price else None,
            {"event_id": url.group(2), "url": url.group(1)},
        ))
    return hits


SEARCHERS = {
    "gametime": search_gametime,
    "vivid": search_vivid,
    "stubhub": search_stubhub,
    "tickpick": search_tickpick,
    "seatgeek": search_seatgeek,
}


# ----------------------------------------------------------------- interaction

def run_searches(query: str) -> dict[str, list[Hit]]:
    q = requests.utils.quote(query) if hasattr(requests, "utils") else query.replace(" ", "%20")
    results: dict[str, list[Hit]] = {}
    for site, fn in SEARCHERS.items():
        try:
            hits = [h for h in fn(q) if "parking" not in h.name.lower()]
            if not hits:   # marketplaces occasionally whiff; one retry is cheap
                hits = [h for h in fn(q) if "parking" not in h.name.lower()]
            results[site] = hits
            print(f"  {site:9s} {len(hits)} events")
        except Exception as e:
            results[site] = []
            print(f"  {site:9s} search failed ({type(e).__name__})")
    return results


def pick_event(results: dict[str, list[Hit]]) -> Hit:
    """Numbered list from the anchor site (Gametime, else Vivid, else any)."""
    for site in ("gametime", "vivid", "stubhub", "seatgeek", "tickpick"):
        anchors = [h for h in results[site] if h.dt and h.dt > datetime.now()]
        if anchors:
            break
    if not anchors:
        sys.exit("No upcoming events found — try a different search.")
    anchors.sort(key=lambda h: h.dt)
    print(f"\nUpcoming shows (via {anchors[0].site}):")
    for i, h in enumerate(anchors[:15], 1):
        price = f"from ${h.min_price:.0f}" if h.min_price else ""
        print(f"  {i:2d}. {h.dt:%a %b %-d %Y %-I:%M %p}  {h.name} — {h.venue}  {price}")
    while True:
        raw = input("\nWatch which one? ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(anchors[:15]):
            return anchors[int(raw) - 1]


def snapshot_zones(sources: dict) -> dict[str, float]:
    """Live per-zone minimums from whichever section sources matched."""
    mins: dict[str, float] = {}
    fetchers = {
        "gametime": lambda c: watcher.fetch_gametime_zones(c["event_id"], 2),
        "vivid": lambda c: watcher.fetch_vivid_zones(c["production_id"], 2),
        "stubhub": lambda c: watcher.fetch_stubhub_zones(c["url"]),
    }
    for site, fetch in fetchers.items():
        if site not in sources:
            continue
        try:
            for zone, q in fetch(sources[site]).items():
                if zone not in mins or q.price < mins[zone]:
                    mins[zone] = q.price
        except Exception as e:
            print(f"  ({site} zone snapshot failed: {type(e).__name__})")
    return mins


def prompt_tiers(zone_mins: dict[str, float]) -> dict[str, dict[str, float]]:
    print("\nCurrent zone minimums (all-in, cheapest across sites):")
    for zone, price in sorted(zone_mins.items(), key=lambda kv: kv[1]):
        print(f"  {zone_label(zone):25s} ${price:.0f}")
    print("\nSet alert thresholds per zone — enter 'good screaming' (e.g. '200 150'),")
    print("or press Enter to skip a zone.")
    tiers: dict[str, dict[str, float]] = {}
    for zone in sorted(zone_mins, key=zone_mins.get):
        if zone == "Other":
            continue
        raw = input(f"  {zone_label(zone)} (now ${zone_mins[zone]:.0f}): ").strip()
        parts = raw.split()
        if len(parts) == 2:
            good, screaming = sorted((float(parts[0]), float(parts[1])), reverse=True)
            tiers[zone] = {"good": good, "screaming": screaming}
    return tiers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help='e.g. "weezer san francisco"')
    ap.add_argument("--config", type=Path, default=Path(__file__).parent / "config.json")
    args = ap.parse_args()

    print(f"Searching marketplaces for {args.query!r}…")
    results = run_searches(args.query)
    anchor = pick_event(results)

    # match the pick on every other site
    sources: dict[str, dict] = {anchor.site: anchor.ids}
    for site, hits in results.items():
        if site == anchor.site:
            continue
        match = next((h for h in hits if looks_like(anchor, h)), None)
        if match:
            sources[site] = match.ids
            print(f"  matched on {site:9s} {match.name} — {match.venue}")
        else:
            print(f"  no match on {site} (that source will be skipped)")

    print("\nFetching live zone prices…")
    zone_mins = snapshot_zones(sources)
    tiers = prompt_tiers(zone_mins) if zone_mins else {}
    if not tiers:
        sys.exit("No zones selected — nothing to watch, config not written.")

    seats = input("\nSeats needed together [2]: ").strip()
    cfg = {
        "event": f"{anchor.name} @ {anchor.venue} ({anchor.dt:%a %b %-d, %-I:%M %p})",
        "stop_at": (anchor.dt + timedelta(minutes=90)).isoformat(timespec="seconds"),
        "min_seats": int(seats) if seats.isdigit() else 2,
        "zones": tiers,
        "sources": sources,
    }
    if args.config.exists():
        if input(f"{args.config} exists — overwrite? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")
    args.config.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"\nWrote {args.config} — start watching with:\n  .venv/bin/python watcher.py")


if __name__ == "__main__":
    main()
