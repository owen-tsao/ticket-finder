"""Unit tests for the pure logic in watcher.py: parsers, alert engine, state.

Network, notifications, and process supervision are exercised on show day;
everything decision-making is tested here.
"""

import json

import pytest

import watcher
from watcher import Engine, Quote, Source


@pytest.fixture(autouse=True)
def sandbox_files(tmp_path, monkeypatch):
    """Keep log/state writes inside the test tmp dir."""
    monkeypatch.setattr(watcher, "LOG_FILE", tmp_path / "watcher.log")
    monkeypatch.setattr(watcher, "STATE_FILE", tmp_path / "state.json")


@pytest.fixture()
def engine():
    return Engine(state={})


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
        zones = watcher.parse_vivid_zones(self.PAYLOAD)
        assert zones["Floor"] == Quote(287.0, "Floor 2", 2)
        assert zones["Upper"].price == 113.0
        assert "Pit General Admission" not in zones

    def test_empty_payload_raises(self):
        with pytest.raises(ValueError):
            watcher.parse_vivid_zones({"tickets": []})

    @pytest.mark.parametrize(
        "section, zone",
        [
            ("GA Pit", "Pit General Admission"),
            ("Floor 3", "Floor"),
            ("Lower Level 109", "Lower"),
            ("Upper Level 222", "Upper"),
            ("Suite 12", "Other"),
        ],
    )
    def test_section_to_zone_mapping(self, section, zone):
        assert watcher._vivid_zone(section) == zone


# ----------------------------------------------------------- state & labels


class TestState:
    def test_roundtrip(self):
        watcher.save_state({"Test:Lower": {"min": 250.0}})
        assert watcher.load_state() == {"Test:Lower": {"min": 250.0}}

    def test_corrupt_file_starts_fresh(self):
        watcher.STATE_FILE.write_text("{not json")
        assert watcher.load_state() == {}


def test_zone_labels():
    assert watcher.zone_label("Lower") == "100s"
    assert watcher.zone_label("Pit General Admission") == "Pit GA"
    assert watcher.zone_label("Floor") == "Floor"
