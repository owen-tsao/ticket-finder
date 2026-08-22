# Ticket Finder

A little watcher that stares at resale ticket prices so you don't have to.

I wrote this the day of a sold-out show. Floor seats were swinging by
hundreds of dollars an hour, the good listings were selling in minutes, and I
was not about to spend the whole afternoon refreshing StubHub. So this ran on
my Mac instead — and by evening we had seats for $40 that had been listed at
several times that earlier in the day.

## What it does

Point it at a concert (there's a built-in search — see below) and every
~15 seconds it checks Gametime, Vivid Seats, and StubHub for the cheapest
listing in each zone you care about (floor, lower bowl, ...), for two or more
seats together. When a price crosses one of your thresholds, it tells you
immediately — everywhere:

- a clickable Mac notification that deep-links straight to the buy page
- a spoken alert (`say`), because you might not be looking at the screen
- a push notification on your phone via [ntfy.sh](https://ntfy.sh), with buy buttons

Alerts come in three flavors:

| | Alert | Trigger |
| --- | --- | --- |
| 🔥 | Good deal | price at or under your "good" threshold for that zone |
| 🚨 | Screaming deal | under your "screaming" threshold — also auto-opens the buy page |
| 📉 | Price drop | a zone hits a new session low, ≥5% below the last poll |

TickPick and SeatGeek event minimums are logged alongside as context, so you
always know how a deal compares across marketplaces.

The details are tuned from getting burned: alerts show all-in prices but
include the pre-fee estimate (so you can actually find the listing on the
site), a tier won't re-fire until 15 minutes pass or the price drops another
$5, and "price drop" only fires on a genuine new low — a cheap listing
selling out and a pricier one rotating in doesn't count.

## How it's built

One watcher (`watcher.py`), three layers:

- **sources** — scrapers that return the cheapest quote per zone. Plain
  HTTPS with browser impersonation (`curl_cffi`), parsing the price data
  embedded in event pages and public listing APIs. Gametime and Vivid quotes
  are pair-verified (only listings with your seat count together); StubHub
  section stats can't verify quantity, so alerts flag that. TickPick and
  SeatGeek are bot-shielded at the section level, so they contribute
  event-level minimums as context. The parsing is pure functions, so it's
  all unit-testable without network.
- **engine** — decides when a quote deserves an alert: tier thresholds,
  re-alert cooldowns, session-low tracking. State survives restarts via
  atomic writes to `state.json`.
- **notifier** — fans one alert out to Mac, voice, and phone. Everything is
  non-blocking; a stuck notification can never stall the poll loop.

## The paranoid part

This thing only matters for a few hours, and it must not die quietly in the
middle of them. So it defends itself in layers:

- **launchd** supervises it: auto-restart within seconds of any crash or
  kill, start at login, no idle sleep (`caffeinate`). The only way it stays
  stopped is its own clean exit at showtime.
- A **watchdog thread** force-restarts the process if the poll loop stalls,
  e.g. on a wedged native HTTP call.
- An **flock lock** makes duplicate instances impossible.
- A **heartbeat file** detects when a previous instance died uncleanly and
  tells you coverage blipped, rather than saying nothing.
- **`faulthandler`** captures tracebacks even for native crashes.
- If a marketplace stops responding, you get a "coverage degraded" ping
  after a few consecutive failures.

Was all of that necessary? The first version died silently twice. Yes.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
brew install terminal-notifier     # optional, for clickable Mac notifications
```

## Pick a concert

```bash
.venv/bin/python discover.py "weezer san francisco"
```

This searches all five marketplaces, shows you the upcoming shows that
match, and — once you pick one — finds the same concert on the other sites
(matching by date and venue), pulls the live per-zone minimums so you can
set thresholds with real numbers in front of you, and writes `config.json`:

```
Upcoming shows (via gametime):
   1. Wed Sep 9 2026 7:00 PM  Weezer — Chase Center, San Francisco  from $38

Watch which one? 1
  matched on vivid     Weezer — Chase Center, San Francisco
  matched on tickpick  Weezer, The Shins & Silversun Pickups — Chase Center, San Francisco
  ...
Current zone minimums (all-in, cheapest across sites):
  Upper   $37
  100s    $131
  Floor   $215
```

`config.json` is plain JSON and safe to hand-edit (see
`config.example.json`) — one config per event; run a second watcher with
`--config` to track two shows at once.

## Run

```bash
.venv/bin/python watcher.py --once    # single quiet check, no alerts
.venv/bin/python watcher.py           # watch in the foreground
```

For the real thing, run it under launchd so it survives closed terminals and
crashes: copy `ticket-watcher.plist.example` into `~/Library/LaunchAgents/`,
fill in your paths, and:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.YOU.ticket-watcher.plist   # start
launchctl bootout gui/$(id -u)/com.YOU.ticket-watcher                                  # stop
```

### Phone alerts (optional)

Install the ntfy app ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)),
subscribe to a long random topic name, and export it as `NTFY_TOPIC` (or set
it in the plist). Anyone who knows the topic can read and send to it, so
treat it like a password.

## Tests

The decision-making — parsers, alert tiers, cooldowns, momentum logic,
config loading, cross-site event matching, state persistence — is covered
by unit tests. No network required.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## A note on scraping

This is personal-use price monitoring: one lightweight request per
marketplace at a polite, jittered cadence — about what a person refreshing
the page would generate. It reads publicly visible prices only and never
automates purchases; every alert links to the marketplace's own checkout.
Not affiliated with any marketplace, and the scrapers depend on page
internals that may break as sites change.
