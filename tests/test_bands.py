import pytest

from rfscoper.bands import CATALOG, candidates, lookup


@pytest.mark.parametrize(
    "freq_hz, band",
    [
        (96_500_000, "FM broadcast"),
        (433_920_000, "433 MHz ISM"),  # narrower than the 70 cm amateur band
        (1_090_000_000, "Aircraft transponders"),  # narrower than aviation radionavigation
        (5_800_000_000, "5.8 GHz ISM"),  # narrower than 5 GHz Wi-Fi
        (2_437_000_000, "2.4 GHz ISM"),
    ],
)
def test_lookup_picks_the_narrowest_allocation(freq_hz, band):
    assert lookup(freq_hz) == band


def test_unknown_frequency_returns_none():
    assert lookup(300_000_000) is None
    assert candidates(300_000_000) == {}


def test_candidates_in_the_24_ghz_band():
    keys = set(candidates(2_426_000_000))
    assert {"wifi_24", "ble_advertising", "bluetooth_hopping", "zigbee"} <= keys


@pytest.mark.parametrize("key, kind", list(CATALOG.items()))
def test_catalog_entries_are_well_formed(key, kind):
    assert key == key.lower() and " " not in key
    assert kind.name and kind.short and kind.band
    assert kind.ranges_mhz
    for start, stop in kind.ranges_mhz:
        assert 0 < start < stop
    low, high = kind.bandwidth_khz
    assert 0 < low <= high
    assert kind.pattern in ("steady", "intermittent", "any")
    for channel in kind.channels_mhz:
        assert kind.range_containing(channel * 1e6, slack_hz=1e6), (key, channel)


def test_ble_advertising_channels():
    assert CATALOG["ble_advertising"].channels_mhz == (2402, 2426, 2480)
