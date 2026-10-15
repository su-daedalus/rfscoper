import pytest

from rfscoper.bands import CATALOG
from rfscoper.identify import identify, proximity_hint, strength

MHZ = 1_000_000


def best(center_mhz, width_mhz, bin_mhz=0.25, duty=None):
    guesses = identify(center_mhz * MHZ, width_mhz * MHZ, bin_mhz * MHZ, duty)
    return guesses[0]


def test_ble_advertising_on_an_advertising_channel():
    guess = best(2426, 1.0, duty=0.3)
    assert guess.key == "ble_advertising"
    assert guess.label == "likely"


def test_narrow_signal_off_the_ble_channels_is_not_confident_ble():
    guess = best(2433, 1.0, duty=0.3)
    assert not (guess.key == "ble_advertising" and guess.label == "likely")


def test_wifi_channel():
    guess = best(2437, 18, duty=0.6)
    assert guess.key == "wifi_24"
    assert guess.label == "likely"


def test_hopping_bluetooth_carpet():
    guess = best(2441, 78, duty=0.3)
    assert guess.key == "bluetooth_hopping"


def test_zigbee_is_wider_than_ble():
    # 2480 MHz is both BLE advertising channel 39 and Zigbee channel 26:
    # with fine bins, width decides
    assert best(2480, 1.0, duty=0.3).key == "ble_advertising"
    assert best(2480, 2.0, duty=0.3).key == "zigbee"


def test_fm_station_is_steady():
    steady = identify(96.5 * MHZ, 0.2 * MHZ, 0.1 * MHZ, duty=1.0)[0]
    assert steady.key == "fm_broadcast" and steady.label == "likely"


def test_width_is_neutral_when_bins_are_too_coarse():
    # 1 MHz bins can't measure a 200 kHz FM station: width counts neither way
    guess = identify(96.5 * MHZ, 1 * MHZ, 1 * MHZ, duty=1.0)[0]
    assert guess.key == "fm_broadcast"
    assert any(r.effect == "~" and "can measure" in r.text for r in guess.reasons)


def test_too_wide_counts_against():
    guess = identify(433.92 * MHZ, 15 * MHZ, 0.1 * MHZ, duty=0.3)[0]
    assert guess.key == "ism_433"
    assert guess.label != "likely"
    assert any(r.effect == "-" and "too wide" in r.text for r in guess.reasons)


def test_nothing_known_at_this_frequency():
    assert identify(300 * MHZ, 0.1 * MHZ, 0.1 * MHZ) == []


def test_scores_are_bounded_and_sorted():
    guesses = identify(2426 * MHZ, 1 * MHZ, 0.25 * MHZ, 0.3)
    scores = [g.score for g in guesses]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 1 for s in scores)


@pytest.mark.parametrize("margin, level", [(5, "weak"), (20, "moderate"), (40, "strong")])
def test_strength(margin, level):
    assert strength(margin) == level


def test_proximity_hint_only_for_low_power_devices():
    assert "close" in proximity_hint(CATALOG["ble_advertising"], 40)
    assert "close" not in (proximity_hint(CATALOG["fm_broadcast"], 40) or "")
    assert proximity_hint(CATALOG["fm_broadcast"], 20) is None


@pytest.mark.parametrize(
    "word, expected",
    [
        ("FM", "an FM"),
        ("BLE", "a BLE"),
        ("Wi-Fi", "a Wi-Fi"),
        ("ADS-B", "an ADS-B"),
        ("433", "a 433"),
    ],
)
def test_article(word, expected):
    from rfscoper.identify import _article

    assert _article(word) == expected


@pytest.mark.parametrize("width_khz", [196, 294, 392, 490])
def test_fm_with_hd_radio_sidebands(width_khz):
    # Widths measured on a real capture in Kansas City with 98 kHz bins:
    # analog-only stations ~200-300 kHz, HD Radio hybrids up to ~490 kHz
    guess = identify(101.1 * MHZ, width_khz * 1e3, 98_039.22, duty=0.95)[0]
    assert guess.key == "fm_broadcast" and guess.label == "likely"
