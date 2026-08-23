"""Unit tests for the pure logic in watcher.py and discover.py: parsers,
alert engine, config loading, event matching, state.

Network, notifications, and process supervision are exercised on show day;
everything decision-making is tested here.
"""

import json
from datetime import datetime

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
        "Lower": {"good": 200.0, "screaming": 150.0},
        "Floor": {"good": 270.0, "screaming": 200.0},
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

    def record(src, zone, q, tier, momentum, drop_pct, prev, floor):
        calls.append((zone, q.price, tier, momentum))

    monkeypatch.setattr(engine, "_fire", record)
    return calls


SRC = Source("Test", fetch=lambda: {}, buy_url="https://example.com", pair_verified=True)


def quote(price: float) -> Quote:
    return Quote(price=price, section="Test 101", listings=3)


# ---------------------------------------------------------------- tier alerts
# "Lower" thresholds: good <= $200, screaming <= $150


class TestTiers:
    def test_good_tier_fires(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        assert fired == [("Lower", 180, "good", False)]

    def test_screaming_beats_good(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(140), quiet=False)
        assert fired[0][2] == "screaming"

    def test_threshold_is_inclusive(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(200), quiet=False)
        assert fired[0][2] == "good"

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

    def test_realerts_after_cooldown_expires(self, engine, fired):
        engine.evaluate(SRC, "Lower", quote(180), quiet=False)
        engine.state["Test:Lower"]["tier_good"]["ts"] -= (
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
        watcher.save_state({"_event": CFG.event, "Test:Lower": {"min": 250.0}})
        assert watcher.load_state(CFG)["Test:Lower"] == {"min": 250.0}

    def test_other_events_state_is_discarded(self):
        # session lows/cooldowns from a previous concert must not carry over
        watcher.save_state({"_event": "Some Other Show", "Test:Lower": {"min": 250.0}})
        assert watcher.load_state(CFG) == {"_event": CFG.event}

    def test_corrupt_file_starts_fresh(self):
        watcher.STATE_FILE.write_text("{not json")
        assert watcher.load_state(CFG) == {"_event": CFG.event}


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
        "zones": {"Lower": {"good": 200, "screaming": 150}},
        "sources": {"vivid": {"production_id": 1, "buy_url": "u"}},
    }

    def write(self, tmp_path, raw):
        p = tmp_path / "config.json"
        p.write_text(json.dumps(raw))
        return p

    def test_loads_and_defaults_min_seats(self, tmp_path):
        cfg = watcher.load_config(self.write(tmp_path, self.RAW))
        assert cfg.min_seats == 2
        assert cfg.zone_tiers["Lower"]["screaming"] == 150.0
        assert cfg.stop_at == datetime(2026, 9, 9, 20, 30)

    def test_missing_key_is_a_clear_error(self, tmp_path):
        raw = {k: v for k, v in self.RAW.items() if k != "zones"}
        with pytest.raises(SystemExit, match="zones"):
            watcher.load_config(self.write(tmp_path, raw))

    def test_missing_file_points_at_discover(self, tmp_path):
        with pytest.raises(SystemExit, match="discover"):
            watcher.load_config(tmp_path / "nope.json")

    def test_sources_gate_what_gets_built(self, tmp_path):
        cfg = watcher.load_config(self.write(tmp_path, self.RAW))
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


# ------------------------------------------------------------------ UI server


class TestUiSave:
    BODY = {
        "anchor": {
            "site": "gametime", "name": "Weezer",
            "venue": "Chase Center, San Francisco",
            "dt": "2026-09-09T19:00:00", "min_price": 35.0, "ids": {},
        },
        "sources": {"vivid": {"production_id": 1, "buy_url": "u"}},
        "zones": {"Lower": {"good": 120, "screaming": 90}},
        "min_seats": 2,
    }

    @pytest.fixture(autouse=True)
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ui, "HERE", tmp_path)
        self.tmp = tmp_path

    def test_save_writes_a_config_the_watcher_accepts(self):
        res = ui.api_save(self.BODY)
        assert res["ok"]
        cfg = watcher.load_config(self.tmp / "config.json")
        assert cfg.zone_tiers["Lower"] == {"good": 120.0, "screaming": 90.0}
        assert cfg.stop_at == datetime(2026, 9, 9, 20, 30)   # start + 90 min
        assert "Weezer" in cfg.event

    def test_save_without_zones_is_rejected(self):
        res = ui.api_save({**self.BODY, "zones": {}})
        assert not res["ok"]
        assert not (self.tmp / "config.json").exists()
