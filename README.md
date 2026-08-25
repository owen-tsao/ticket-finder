# Doors

A little watcher that stares at resale ticket prices so you don't have to.

I wrote this the day of a sold-out show. Floor seats were swinging by
hundreds of dollars an hour, the good listings were selling in minutes, and I
was not about to spend the whole afternoon refreshing StubHub. So this ran on
my Mac instead — and by evening we had seats for $40 that had been listed at
several times that earlier in the day.

## What it does

Search for a show in the web UI, set your prices, and every ~15 seconds the
watcher checks Gametime, Vivid Seats, and StubHub for the cheapest listing in
each zone you care about (floor, lower bowl, ...), for two or more seats
together. It can track several shows at once. When a price crosses one of
your thresholds, it tells you immediately — everywhere:

- a clickable Mac notification that deep-links straight to the buy page
- a push notification on your phone via [ntfy.sh](https://ntfy.sh), with buy buttons
- for steals only: a spoken alert (`say`) and the buy page opening itself

Alerts come in three flavors:

| | Alert | Trigger |
| --- | --- | --- |
| 🔥 | Deal | price at or under your "deal" threshold for that zone |
| 🚨 | Steal | under your "steal" threshold — also auto-opens the buy page |
| 📉 | Price drop | a zone hits a new session low, ≥5% below the last poll |

TickPick and SeatGeek event minimums are logged alongside as context, so you
always know how a deal compares across marketplaces.

The details are tuned from getting burned: alerts show all-in prices but
include the pre-fee estimate (so you can actually find the listing on the
site), a tier won't re-fire until 15 minutes pass or the price drops another
$5, and "price drop" only fires on a genuine new low — a cheap listing
selling out and a pricier one rotating in doesn't count. International shows
are priced in their own currency, and a £66 ticket is never mistaken for
being cheaper than a $90 threshold.

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
  re-alert cooldowns, session-low tracking. One engine per tracked show;
  state survives restarts via atomic writes to `state.json`.
- **notifier** — fans one alert out to Mac, phone, and (for steals) voice.
  Everything is non-blocking; a stuck notification can never stall the poll
  loop.

Plus a local web UI (`ui.py`): a stdlib `http.server` serving one static
page that reuses the discovery and watcher code directly. Zero extra
dependencies, by choice — at this size, a React/Node frontend would double
the install burden of the whole project to render one page.

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

Requires macOS and Python 3.10+ (`brew install python` if your system one
is older).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
brew install terminal-notifier     # optional, for clickable Mac notifications
```

## Track a show

The easiest way is the built-in UI:

```bash
.venv/bin/python ui.py     # → http://127.0.0.1:8321
```

One page, served straight from Python — no Node, no build step, nothing
extra to install. Search an artist (the five marketplaces are queried in
parallel and merged, so one search covers all of them), click the show, and
it matches the concert across every site, shows you the live per-zone
minimums, and lets you set deal/steal thresholds and hit **Save & start
watching**. Tracked shows appear as cards with live cheapest-per-zone
prices; add as many as you like, remove them when you're done.

Phone pushes are set up from the UI too — the **Phone alerts** chip in the
header walks you through installing ntfy, subscribing to a generated private
topic, and sending yourself a test. The topic works like a password (anyone
who knows it can read your alerts), which is why it's long and random.

The same flow also works entirely in the terminal:

```bash
.venv/bin/python discover.py "weezer san francisco"
```

Either way you end up with a `config.json` — plain JSON, one entry per
tracked show, and safe to hand-edit (see `config.example.json`).

<details>
<summary>What the CLI flow looks like</summary>

```
Upcoming shows (merged across marketplaces):
   1. Wed Sep 9 2026 7:00 PM  Weezer — Chase Center, San Francisco  from $38

Watch which one? 1
  matched on vivid     Weezer — Chase Center, San Francisco
  matched on tickpick  Weezer, The Shins & Silversun Pickups — Chase Center, San Francisco
  ...
Current zone minimums (all-in, cheapest across sites, before fees):
  Upper   $37
  100s    $131
  Floor   $215
```

</details>

## Run

The UI starts and stops the watcher for you. To run it by hand:

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

The watcher exits cleanly ~90 minutes after the last tracked show starts.
One honest limitation: the watcher runs on your machine, so a sleeping
laptop sends no alerts — keep it awake (or run it on something always-on)
during the hours you care about.

## Tests

The decision-making — parsers, alert tiers, cooldowns, momentum logic,
config loading, cross-site event matching, currency handling, state
persistence — is covered by unit tests. No network required.

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
