"""Unit tests for the pure logic in watcher.py and discover.py: parsers,
alert engine, config loading, event matching, state.

Network, notifications, and process supervision are exercised on show day;
everything decision-making is tested here.
"""

import json
from datetime import datetime, timedelta

import pytest

import discover
import ui
import watcher
from watcher import Config, Engine, Quote, Source


@pytest.fixture(autouse=True)
def sandbox_files(tmp_path, monkeypatch):
    """Keep log/state writes inside the test tmp dir."""
    monkeypatch.setattr(watcher, "LOG_FILE", tmp_path / "watcher.log")
    monkeypatch.setattr(watcher, "STATE_FILE", tmp_path / "state.json")


CFG = Config(
    event="Test Event",
    stop_at=datetime(2026, 9, 9, 20, 30),
    min_seats=2,
    zone_tiers={
        "Lower": {"deal": 200.0, "steal": 150.0},
        "Floor": {"deal": 270.0, "steal": 200.0},
    },
    sources={"vivid": {"production_id": 1, "buy_url": "https://example.com"}},
)


@pytest.fixture()
def engine():
    return Engine(CFG, state={})


@pytest.fixture()
def fired(engine, monkeypatch):
    """Record (zone, price, tier, momentum) for every alert instead of notifying."""
    calls = []

    def record(src, zone, q, tier, momentum, drop_pct, prev):
        calls.append((zone, q.price, tier, momentum))

    monkeypatch.setattr(engine, "_fire", record)
    return calls


SRC = Source("Test", fetch=lambda: {}, buy_url="https://example.com", pair_verified=True)


def quote(price: float) -> Quote:
    return Quote(price=price, section="Test 101", listings=3)


# ---------------------------------------------------------------- tier alerts
# "Lower" thresholds: deal <= $200, steal <= $150


class TestTiers:
    def test_deal_tier_fires(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        assert fired == [("Lower", 180, "deal", False)]

    def test_steal_beats_deal(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(140), quiet=False)
        assert fired[0][2] == "steal"

    def test_threshold_is_inclusive(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(200), quiet=False)
        assert fired[0][2] == "deal"

    def test_above_threshold_is_silent(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(201), quiet=False)
        assert fired == []

    def test_cooldown_suppresses_near_identical_repeat(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        engine.evaluate(SRC, "Lower", quote(179), quiet=False)
        assert len(fired) == 1

    def test_further_drop_realerts_within_cooldown(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        engine.evaluate(SRC, "Lower", quote(170), quiet=False)
        assert [f[1] for f in fired] == [180, 170]

    def test_mismatched_currency_never_compares(self, engine, fired):
        # £140 must not trip a $150 steal threshold (or record a session low)
        gbp = Quote(price=140, section="Test 101", listings=3, currency="GBP")
        engine.evaluate(SRC, "Lower", gbp, quiet=False)
        assert fired == []
        assert engine.state == {}


class TestNotificationFormat:
    """Alerts: severity+artist title, buyable subtitle, max-two-facts body."""

    def _sent(self, engine, monkeypatch, src=SRC, price=140.0):
        sent = {}
        monkeypatch.setattr(
            watcher, "notify",
            lambda kind, title, subtitle, body, spoken, url=None, alt_url=None:
            sent.update(kind=kind, title=title, subtitle=subtitle, body=body),
        )
        monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: None)
        engine.evaluate(src, "Lower", quote(price), quiet=False)
        return sent

    def test_steal_alert_is_clean(self, engine, monkeypatch):
        sent = self._sent(engine, monkeypatch)
        assert sent["title"] == "Steal — Test Event"
        assert sent["subtitle"] == "100s $140 on Test"
        assert sent["body"] == "Under your $150 target"

    def test_unverified_source_gets_warning(self, engine, monkeypatch):
        loose = Source("Test", fetch=lambda: {}, buy_url="https://example.com",
                       pair_verified=False)
        sent = self._sent(engine, monkeypatch, src=loose)
        assert sent["body"].endswith("⚠ single seats — verify 2 together")

    def test_realerts_after_cooldown_expires(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        engine.state["Test:Lower"]["tier_deal"]["ts"] -= (
            watcher.REALERT_COOLDOWN_S + 1
        )
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        assert len(fired) == 2

    def test_quiet_run_never_poisons_cooldown(self, engine, fired):
        # a quiet --once check must not stamp the cooldown and swallow
        # the real alert that follows
        engine.evaluate(SRC, "Lower", quote(180), quiet=True)
        assert fired == []
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        assert len(fired) == 1


# ------------------------------------------------------------ momentum alerts


class TestMomentum:
    def test_big_drop_to_new_session_low_fires(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(300), quiet=False)
        engine.evaluate(SRC, "Lower", quote(250), quiet=False)
        assert fired == [("Lower", 250, None, True)]

    def test_small_drop_is_silent(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(300), quiet=False)
        engine.evaluate(SRC, "Lower", quote(290), quiet=False)  # -3.3%
        assert fired == []

    def test_listing_churn_does_not_fire(self, engine, fired):
        # cheap listing sells, price rebounds, then falls back — but not
        # below the session floor. That's churn, not momentum.
        engine.evaluate(SRC, "Lower", quote(250), quiet=False)
        engine.evaluate(SRC, "Lower", quote(400), quiet=False)
        engine.evaluate(SRC, "Lower", quote(260), quiet=False)  # -35% but > floor
        assert fired == []

    def test_session_floor_tracks_lowest_seen(self, engine, fired):
        for price in (300, 250, 400):
            engine.evaluate(SRC, "Lower", quote(price), quiet=False)
        assert engine.state["Test:Lower"]["floor"] == 250


# ------------------------------------------------------------ StubHub parser


def sh_section(section: str, price: float, listings: int, zone: str) -> str:
    return (
        f'{{"sectionName":"{section}","minTicketPrice":{price},'
        f'"totalListings":{listings},"zoneId":1,"zoneName":"{zone}"}}'
    )


class TestStubHubParser:
    def test_keeps_cheapest_section_and_sums_listings(self):
        text = (
            sh_section("Lower 109", 353.0, 6, "Lower")
            + sh_section("Lower 108", 300.5, 4, "Lower")
            + sh_section("Floor 2", 494.0, 4, "Floor")
        )
        zones = watcher.parse_stubhub_zones(text)
        assert zones["Lower"] == Quote(300.5, "Lower 108", 10)
        assert zones["Floor"].price == 494.0

    def test_incomplete_section_cannot_mispair_with_neighbor(self):
        # first object lacks totalListings; the regex must not reach across
        # the object boundary and pair A's price with B's zone
        text = (
            '{"sectionName":"A","minTicketPrice":100.0,"zoneId":1,"zoneName":"Z1"}'
            + sh_section("B", 200.0, 3, "Z2")
        )
        zones = watcher.parse_stubhub_zones(text)
        assert "Z1" not in zones
        assert zones["Z2"] == Quote(200.0, "B", 3)

    def test_pre_fee_estimate_from_event_fee_factor(self):
        text = (
            sh_section("Lower 109", 200.0, 6, "Lower")
            + '"minPrice":100.0,"maxPrice":600.0,"minListPrice":78.0'
        )
        zones = watcher.parse_stubhub_zones(text)
        assert zones["Lower"].pre_fee == pytest.approx(156.0)

    def test_no_sections_raises(self):
        with pytest.raises(ValueError):
            watcher.parse_stubhub_zones("<html>no data</html>")

    def test_currency_detected_and_tagged(self):
        # StubHub .ie reports international events in local currency
        text = ('"currencyCode":"GBP"'
                + sh_section("Floor Standing", 66.0, 12, "Floor Standing"))
        zones = watcher.parse_stubhub_zones(text)
        assert zones["Floor Standing"].currency == "GBP"

    def test_currency_defaults_to_usd(self):
        zones = watcher.parse_stubhub_zones(sh_section("Lower 109", 200.0, 6, "Lower"))
        assert zones["Lower"].currency == "USD"


# -------------------------------------------------------------- Vivid parser


class TestVividParser:
    PAYLOAD = {
        "tickets": [
            {"aip": "655.00", "q": "1", "s": "GA Pit"},          # single seat: skip
            {"aip": "300.00", "q": "4", "s": "Floor 1"},
            {"aip": "287.00", "q": "2", "s": "Floor 2"},
            {"aip": "113.00", "q": "2", "s": "Upper Level 203"},
            {"s": "malformed listing"},                           # skip
        ]
    }

    def test_filters_singles_and_keeps_zone_minimums(self):
        zones = watcher.parse_vivid_zones(self.PAYLOAD, min_seats=2)
        assert zones["Floor"] == Quote(287.0, "Floor 2", 2)
        assert zones["Upper"].price == 113.0
        assert "Pit General Admission" not in zones

    def test_empty_payload_raises(self):
        with pytest.raises(ValueError):
            watcher.parse_vivid_zones({"tickets": []}, min_seats=2)

    @pytest.mark.parametrize(
        "section, zone",
        [
            ("GA Pit", "Pit General Admission"),
            ("Floor 3", "Floor"),
            ("GAFL", "Floor"),
            ("GA Floor", "Floor"),
            ("Lower Level 109", "Lower"),
            ("Upper Level 222", "Upper"),
            ("Suite 12", "Other"),
        ],
    )
    def test_section_to_zone_mapping(self, section, zone):
        assert watcher.zone_from_section(section) == zone


# ----------------------------------------------------------- state & labels


class TestState:
    def test_roundtrip_same_event(self):
        watcher.save_state(
            {"events": {CFG.event: {"Test:Lower": {"min": 250.0}}}}
        )
        state = watcher.load_state([CFG])
        assert state["events"][CFG.event]["Test:Lower"] == {"min": 250.0}

    def test_untracked_events_state_is_discarded(self):
        # session lows/cooldowns from a removed concert must not linger
        watcher.save_state(
            {"events": {"Some Other Show": {"Test:Lower": {"min": 250.0}}}}
        )
        state = watcher.load_state([CFG])
        assert "Some Other Show" not in state["events"]
        assert state["events"][CFG.event] == {}

    def test_two_events_keep_separate_state(self):
        other = Config(
            event="Other Event", stop_at=CFG.stop_at, min_seats=2,
            zone_tiers=CFG.zone_tiers, sources=CFG.sources,
        )
        watcher.save_state({"events": {
            CFG.event: {"Test:Lower": {"min": 250.0}},
            other.event: {"Test:Lower": {"min": 90.0}},
        }})
        state = watcher.load_state([CFG, other])
        assert state["events"][CFG.event]["Test:Lower"]["min"] == 250.0
        assert state["events"][other.event]["Test:Lower"]["min"] == 90.0

    def test_legacy_single_event_state_migrates(self):
        watcher.STATE_FILE.write_text(json.dumps(
            {"_event": CFG.event, "Test:Lower": {"min": 250.0}}
        ))
        state = watcher.load_state([CFG])
        assert state["events"][CFG.event]["Test:Lower"] == {"min": 250.0}

    def test_corrupt_file_starts_fresh(self):
        watcher.STATE_FILE.write_text("{not json")
        assert watcher.load_state([CFG]) == {"events": {CFG.event: {}}}


def test_zone_labels():
    assert watcher.zone_label("Lower") == "100s"
    assert watcher.zone_label("Pit General Admission") == "Pit GA"
    assert watcher.zone_label("Floor") == "Floor"


# ------------------------------------------------------------ Gametime parser


class TestGametimeParser:
    PAYLOAD = {
        "listings": [
            {"price": {"total": 16400}, "section": "102", "section_group": "Lower"},
            {"price": {"total": 15100}, "section": "117", "section_group": "Lower"},
            {"price": {"total": 21500}, "section": "FLR4", "section_group": "Floor"},
            {"section": "no price"},
        ]
    }

    def test_groups_by_section_group_in_dollars(self):
        zones = watcher.parse_gametime_zones(self.PAYLOAD)
        assert zones["Lower"] == Quote(151.0, "117", 2)
        assert zones["Floor"].price == 215.0

    def test_empty_payload_raises(self):
        with pytest.raises(ValueError):
            watcher.parse_gametime_zones({"listings": []})


# ------------------------------------------------------------- config loading


class TestConfig:
    RAW = {
        "event": "Test",
        "stop_at": "2026-09-09T20:30:00",
        "zones": {"Lower": {"deal": 200, "steal": 150}},
        "sources": {"vivid": {"production_id": 1, "buy_url": "u"}},
    }

    def write(self, tmp_path, raw):
        p = tmp_path / "config.json"
        p.write_text(json.dumps(raw))
        return p

    def test_loads_and_defaults_min_seats(self, tmp_path):
        cfg = watcher.load_configs(self.write(tmp_path, self.RAW))[0]
        assert cfg.min_seats == 2
        assert cfg.zone_tiers["Lower"]["steal"] == 150.0
        assert cfg.stop_at == datetime(2026, 9, 9, 20, 30)

    def test_events_list_shape_loads_all(self, tmp_path):
        raw = {"events": [self.RAW, {**self.RAW, "event": "Test 2"}]}
        cfgs = watcher.load_configs(self.write(tmp_path, raw))
        assert [c.event for c in cfgs] == ["Test", "Test 2"]

    def test_legacy_tier_names_still_load(self, tmp_path):
        raw = {**self.RAW, "zones": {"Lower": {"good": 200, "screaming": 150}}}
        cfg = watcher.load_configs(self.write(tmp_path, raw))[0]
        assert cfg.zone_tiers["Lower"] == {"deal": 200.0, "steal": 150.0, "currency": "USD"}

    def test_missing_key_is_a_clear_error(self, tmp_path):
        raw = {k: v for k, v in self.RAW.items() if k != "zones"}
        with pytest.raises(SystemExit, match="zones"):
            watcher.load_configs(self.write(tmp_path, raw))

    def test_missing_file_points_at_ui(self, tmp_path):
        with pytest.raises(SystemExit, match="ui.py"):
            watcher.load_configs(tmp_path / "nope.json")

    def test_sources_gate_what_gets_built(self, tmp_path):
        cfg = watcher.load_configs(self.write(tmp_path, self.RAW))[0]
        section, context = watcher.build_sources(cfg)
        assert [s.name for s in section] == ["Vivid"]
        assert context == []


# ----------------------------------------------------- cross-site matching


class TestEventMatching:
    ANCHOR = discover.Hit(
        "gametime", "Weezer", "Chase Center, San Francisco",
        datetime(2026, 9, 9, 19, 0), 38.0,
    )

    def test_same_show_different_billing_matches(self):
        hit = discover.Hit(
            "tickpick", "Weezer, The Shins & Silversun Pickups",
            "Chase Center, San Francisco", datetime(2026, 9, 9, 19, 0), 37.0,
        )
        assert discover.looks_like(self.ANCHOR, hit)

    def test_same_tour_next_night_does_not_match(self):
        hit = discover.Hit(
            "stubhub", "Weezer Sacramento", "Golden 1 Center",
            datetime(2026, 9, 8, 19, 0), None,
        )
        assert not discover.looks_like(self.ANCHOR, hit)

    def test_different_artist_same_venue_does_not_match(self):
        hit = discover.Hit(
            "vivid", "Daniel Caesar", "Chase Center, San Francisco",
            datetime(2026, 9, 9, 20, 0), None,
        )
        assert not discover.looks_like(self.ANCHOR, hit)

    def test_missing_date_falls_back_to_name_and_venue(self):
        hit = discover.Hit(
            "tickpick", "Weezer", "Chase Center, San Francisco", None, None,
        )
        assert discover.looks_like(self.ANCHOR, hit)

    def test_json_escapes_decoded(self):
        assert discover._junescape("Shins \\u0026 Pickups") == "Shins & Pickups"

    def test_html_entities_decoded(self):
        # StubHub names arrive with raw HTML entities ("Grupo Frontera &amp; Ozuna")
        assert discover._junescape("Grupo Frontera &amp; Ozuna") == "Grupo Frontera & Ozuna"

    def test_anchors_merge_shows_missing_from_first_site(self):
        # Gametime's search API caps at 10 results, so a show it omits must
        # still surface from another marketplace's results.
        future = (datetime.now() + timedelta(days=30)).replace(
            hour=19, minute=0, second=0, microsecond=0)
        gametime_hit = discover.Hit(
            "gametime", "Bruno Mars", "MetLife Stadium, East Rutherford",
            future, 205.0,
        )
        seatgeek_only = discover.Hit(
            "seatgeek", "Bruno Mars", "Levi's Stadium, Santa Clara",
            future + timedelta(days=45), 150.0,
        )
        anchors = discover.upcoming_anchors({
            "gametime": [gametime_hit],
            "vivid": [], "stubhub": [], "tickpick": [],
            "seatgeek": [seatgeek_only],
        })
        assert [h.venue for h in anchors] == [
            "MetLife Stadium, East Rutherford", "Levi's Stadium, Santa Clara",
        ]

    def test_anchors_dedupe_same_show_across_sites(self):
        future = (datetime.now() + timedelta(days=30)).replace(
            hour=19, minute=0, second=0, microsecond=0)
        gt = discover.Hit("gametime", "Bruno Mars",
                          "MetLife Stadium, East Rutherford", future, 205.0)
        sg = discover.Hit("seatgeek", "Bruno Mars - Live",
                          "MetLife Stadium", future.replace(hour=20), 199.0)
        anchors = discover.upcoming_anchors({
            "gametime": [gt], "vivid": [], "stubhub": [], "tickpick": [],
            "seatgeek": [sg],
        })
        assert len(anchors) == 1
        assert anchors[0].site == "gametime"   # richest ids win

    def test_anchors_keep_matinee_and_evening_shows_separate(self):
        # same artist, venue, and date — but 1 PM vs 7:30 PM are two events
        future = (datetime.now() + timedelta(days=30)).replace(
            hour=13, minute=0, second=0, microsecond=0)
        matinee = discover.Hit("gametime", "Bruno Mars", "SoFi Stadium",
                               future, 205.0)
        evening = discover.Hit("gametime", "Bruno Mars", "SoFi Stadium",
                               future.replace(hour=19, minute=30), 250.0)
        anchors = discover.upcoming_anchors({
            "gametime": [matinee, evening], "vivid": [], "stubhub": [],
            "tickpick": [], "seatgeek": [],
        })
        assert len(anchors) == 2

    def test_placeholder_noon_time_still_matches_real_showtime(self):
        # StubHub lists "12:00 PM" when the time is TBD; that must not stop
        # it from matching the same show listed at 7 PM elsewhere
        real = discover.Hit(
            "gametime", "Bruno Mars", "Levi's Stadium",
            datetime(2026, 10, 10, 19, 0), 205.0,
        )
        placeholder = discover.Hit(
            "stubhub", "Bruno Mars Santa Clara", "Levi's Stadium",
            datetime(2026, 10, 10, 12, 0), None,
        )
        assert discover.looks_like(real, placeholder)

    def test_viewer_shifted_time_still_matches(self):
        # StubHub epochs render in the viewer's tz: a 6:30 PM London show
        # reads 11:30 AM from California. time_known=False must disable the
        # 3-hour window so the duplicate merges instead of listing twice.
        gametime = discover.Hit(
            "gametime", "Daniel Caesar", "O2 Arena - London, London",
            datetime(2026, 9, 2, 18, 30), 115.0,
        )
        stubhub = discover.Hit(
            "stubhub", "Daniel Caesar London", "The O2 Arena",
            datetime(2026, 9, 2, 11, 30), None, time_known=False,
        )
        assert discover.looks_like(gametime, stubhub)

    def test_anchor_with_unknown_time_adopts_duplicates_real_time(self):
        # if StubHub is the first/only rich source, its shifted time would be
        # shown to the user; a later duplicate with a real local time fixes it
        future = (datetime.now() + timedelta(days=30)).replace(
            hour=11, minute=30, second=0, microsecond=0)
        sh = discover.Hit("stubhub", "Daniel Caesar London", "The O2 Arena",
                          future, None, time_known=False)
        tp = discover.Hit("tickpick", "Daniel Caesar", "O2 Arena, London",
                          future.replace(hour=18, minute=30), 110.0)
        anchors = discover.upcoming_anchors({
            "gametime": [], "vivid": [], "stubhub": [sh],
            "seatgeek": [], "tickpick": [tp],
        })
        assert len(anchors) == 1
        assert anchors[0].site == "stubhub"          # ids kept
        assert anchors[0].dt.hour == 18              # time upgraded
        assert anchors[0].time_known is True

    def test_anchor_without_price_adopts_duplicates_price(self):
        # StubHub search results carry no prices; when another site knows the
        # same show, its "from" price should fill the blank
        future = (datetime.now() + timedelta(days=30)).replace(
            hour=19, minute=0, second=0, microsecond=0)
        sh = discover.Hit("stubhub", "Bruno Mars Vancouver", "BC Place Stadium",
                          future, None, time_known=False)
        tp = discover.Hit("tickpick", "Bruno Mars", "BC Place Stadium, Vancouver",
                          future, 145.0)
        anchors = discover.upcoming_anchors({
            "gametime": [], "vivid": [], "stubhub": [sh],
            "seatgeek": [], "tickpick": [tp],
        })
        assert len(anchors) == 1
        assert anchors[0].site == "stubhub"      # anchor unchanged
        assert anchors[0].min_price == 145.0     # price adopted

    def test_snapshot_never_min_merges_across_currencies(self, monkeypatch):
        # £66 is not "cheaper" than $90: a USD quote must not be replaced by a
        # numerically lower quote in another currency
        monkeypatch.setattr(watcher, "fetch_gametime_zones",
                            lambda eid, n: {"Upper": Quote(90.0, "303", 5)})
        monkeypatch.setattr(watcher, "fetch_stubhub_zones", lambda url: {
            "Upper": Quote(66.0, "Upper 312", 9, currency="GBP"),
            "Floor Standing": Quote(66.0, "Floor Standing", 12, currency="GBP"),
        })
        mins = discover.snapshot_zones({
            "gametime": {"event_id": "x"}, "stubhub": {"url": "u"},
        })
        assert mins["Upper"] == (90.0, "USD")            # kept, not "beaten" by £66
        assert mins["Floor Standing"] == (66.0, "GBP")   # new zones keep their currency


# ------------------------------------------------------------------ UI server


class TestWatcherRestart:
    def test_restart_waits_for_old_lock_before_starting(self, monkeypatch):
        """SIGTERM is async: starting before the old flock frees either
        no-ops (pid check still sees it) or the new watcher exits."""
        calls = []
        pids = iter([111, 111, 111, None])   # gate check, stop check, one poll, freed
        monkeypatch.setattr(ui, "watcher_pid", lambda: next(pids, None))
        monkeypatch.setattr(ui.os, "kill", lambda pid, sig: calls.append(("kill", pid)))
        monkeypatch.setattr(ui.time, "sleep", lambda s: None)
        monkeypatch.setattr(ui, "start_watcher",
                            lambda: calls.append(("start",)) or {"ok": True})
        ui._restart_watcher_if_running()
        assert calls == [("kill", 111), ("start",)]


class TestUiSave:
    BODY = {
        "anchor": {
            "site": "gametime", "name": "Weezer",
            "venue": "Chase Center, San Francisco",
            "dt": "2026-09-09T19:00:00", "min_price": 35.0, "ids": {},
        },
        "sources": {"vivid": {"production_id": 1, "buy_url": "u"},
                    "gametime": {"event_id": "x", "buy_url": "u", "image": "img.jpg"}},
        "image": "https://images.gametime.co/musicweezer/hero@4x.jpg",
        "zones": {"Lower": {"deal": 120, "steal": 90}},
        "min_seats": 2,
    }

    @pytest.fixture(autouse=True)
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ui, "HERE", tmp_path)
        monkeypatch.setattr(ui, "_restart_watcher_if_running", lambda: None)
        self.tmp = tmp_path

    def test_save_writes_a_config_the_watcher_accepts(self):
        res = ui.api_save(self.BODY)
        assert res["ok"]
        cfg = watcher.load_configs(self.tmp / "config.json")[0]
        assert cfg.zone_tiers["Lower"] == {"deal": 120.0, "steal": 90.0, "currency": "USD"}
        assert cfg.stop_at == datetime(2026, 9, 9, 20, 30)   # start + 90 min
        assert "Weezer" in cfg.event

    def test_save_strips_image_from_sources_but_keeps_it_on_event(self):
        ui.api_save(self.BODY)
        raw = json.loads((self.tmp / "config.json").read_text())["events"][0]
        assert raw["image"].endswith("hero@4x.jpg")
        assert "image" not in raw["sources"]["gametime"]

    def test_saving_twice_replaces_not_duplicates(self):
        ui.api_save(self.BODY)
        ui.api_save(self.BODY)
        assert len(ui.load_events()) == 1

    def test_two_different_shows_are_both_tracked(self):
        ui.api_save(self.BODY)
        other = {**self.BODY, "anchor": {**self.BODY["anchor"], "name": "Turnstile"}}
        ui.api_save(other)
        assert len(ui.load_events()) == 2

    def test_remove_deletes_only_the_target(self, monkeypatch):
        monkeypatch.setattr(ui, "stop_watcher", lambda: {"ok": True})
        ui.api_save(self.BODY)
        other = {**self.BODY, "anchor": {**self.BODY["anchor"], "name": "Turnstile"}}
        saved = ui.api_save(other)
        ui.api_remove({"id": saved["id"]})
        events = ui.load_events()
        assert [e["name"] for e in events] == ["Weezer"]

    def test_save_without_zones_is_rejected(self):
        res = ui.api_save({**self.BODY, "zones": {}})
        assert not res["ok"]
        assert not (self.tmp / "config.json").exists()


class TestNtfySetup:
    @pytest.fixture(autouse=True)
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ui, "HERE", tmp_path)
        monkeypatch.setattr(ui, "_restart_watcher_if_running", lambda: None)
        self.tmp = tmp_path

    def test_topic_saves_and_watcher_reads_it(self, monkeypatch):
        event = {"event": "X @ Y (Z)",
                 "stop_at": "2027-01-01T23:00:00",
                 "zones": {"Lower": {"deal": 100, "steal": 80}},
                 "sources": {"gametime": {"event_id": "x", "buy_url": "u"}}}
        (self.tmp / "config.json").write_text(json.dumps({"events": [event]}))
        res = ui.api_ntfy_save({"topic": "doors-abc123def456"})
        assert res["ok"]
        monkeypatch.setattr(watcher, "NTFY_TOPIC", "")   # no env override
        watcher.load_configs(self.tmp / "config.json")
        assert watcher.NTFY_TOPIC == "doors-abc123def456"

    def test_event_saves_keep_the_topic(self):
        # save_events rewrites config.json wholesale; the phone-push setting
        # must survive adding or removing shows
        ui.api_ntfy_save({"topic": "doors-abc123def456"})
        ui.save_events([{"id": "x", "event": "X @ Y (Z)"}])
        raw = json.loads((self.tmp / "config.json").read_text())
        assert raw["ntfy_topic"] == "doors-abc123def456"
        assert len(raw["events"]) == 1

    def test_empty_topic_disables(self):
        ui.api_ntfy_save({"topic": "doors-abc123def456"})
        ui.api_ntfy_save({"topic": ""})
        assert "ntfy_topic" not in json.loads((self.tmp / "config.json").read_text())

    def test_garbage_topic_rejected(self):
        # spaces/slashes would break the ntfy URL; short topics are guessable
        assert not ui.api_ntfy_save({"topic": "hi"})["ok"]
        assert not ui.api_ntfy_save({"topic": "has spaces here"})["ok"]
        assert not ui.api_ntfy_save({"topic": "a/../b12345678"})["ok"]
