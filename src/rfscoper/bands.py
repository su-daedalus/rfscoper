"""A catalog of common US signals, used to guess what a detected signal is.

Each entry describes a signal by the things a power sweep can actually see:

- where it lives (frequency ranges, and exact channel centers when it is channelized)
- how wide it is (typical occupied bandwidth)
- how it behaves over time (steady, intermittent, or either)
- whether it comes from a low-power personal device, so that "loud" means "close"

A sweep measures power only. It cannot decode a transmission, so these are
educated guesses from a signal's shape, never proof. This is an orientation
aid, not a regulatory reference: allocations differ by country, and a signal
inside a band is not necessarily the service the band is named after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Pattern = Literal["steady", "intermittent", "any"]


@dataclass(frozen=True)
class SignalType:
    name: str  # what it is, in plain words
    short: str  # a few characters, for plot labels
    band: str  # the allocation it lives in
    ranges_mhz: tuple[tuple[float, float], ...]
    bandwidth_khz: tuple[float, float]  # typical occupied bandwidth, min and max
    pattern: Pattern
    channels_mhz: tuple[float, ...] = ()  # exact channel centers, if channelized
    low_power: bool = False  # personal devices: a strong signal means a nearby one
    note: str = ""

    @property
    def bandwidth_hz(self) -> tuple[float, float]:
        low, high = self.bandwidth_khz
        return low * 1e3, high * 1e3

    def range_containing(self, freq_hz: float, slack_hz: float = 0.0) -> tuple[float, float] | None:
        """The (start_hz, stop_hz) range holding freq_hz, or None."""
        for start, stop in self.ranges_mhz:
            low, high = start * 1e6 - slack_hz, stop * 1e6 + slack_hz
            if low <= freq_hz <= high:
                return start * 1e6, stop * 1e6
        return None


def _grid(first: float, last: float, step: float) -> tuple[float, ...]:
    """Evenly spaced channel centers in MHz, first and last included."""
    count = round((last - first) / step) + 1
    return tuple(round(first + i * step, 4) for i in range(count))


# The catalog. Keys are stable IDs, used in JSON output and on the command line.
CATALOG: dict[str, SignalType] = {
    # ------------------------------------------------------------- broadcast
    "am_broadcast": SignalType(
        name="AM radio station",
        short="AM",
        band="AM broadcast",
        ranges_mhz=((0.53, 1.71),),
        bandwidth_khz=(5, 20),
        pattern="steady",
        channels_mhz=_grid(0.54, 1.70, 0.01),
        note="Below the HackRF's 1 MHz lower limit for most of the band.",
    ),
    "fm_broadcast": SignalType(
        name="FM radio station",
        short="FM",
        band="FM broadcast",
        ranges_mhz=((88, 108),),
        bandwidth_khz=(100, 420),
        pattern="steady",
        channels_mhz=_grid(88.1, 107.9, 0.2),
        note=(
            "US stations sit on odd tenths: 88.1, 88.3 ... 107.9 MHz. Analog FM is about "
            "200 kHz wide; stations that also broadcast HD Radio add digital sidebands out "
            "to about 200 kHz either side, so about 400 kHz in all."
        ),
    ),
    "tv_vhf_low": SignalType(
        name="Digital TV (VHF low, ch 2-6)",
        short="TV",
        band="VHF TV",
        ranges_mhz=((54, 72), (76, 88)),
        bandwidth_khz=(5000, 6200),
        pattern="steady",
        channels_mhz=(57, 63, 69, 79, 85),
        note="ATSC fills a 6 MHz channel with a flat top and a small pilot at the low edge.",
    ),
    "tv_vhf_high": SignalType(
        name="Digital TV (VHF high, ch 7-13)",
        short="TV",
        band="VHF TV",
        ranges_mhz=((174, 216),),
        bandwidth_khz=(5000, 6200),
        pattern="steady",
        channels_mhz=_grid(177, 213, 6),
    ),
    "tv_uhf": SignalType(
        name="Digital TV (UHF, ch 14-36)",
        short="TV",
        band="UHF TV",
        ranges_mhz=((470, 608),),
        bandwidth_khz=(5000, 6200),
        pattern="steady",
        channels_mhz=_grid(473, 605, 6),
    ),
    "noaa_weather_radio": SignalType(
        name="NOAA Weather Radio",
        short="WX",
        band="NOAA weather radio",
        ranges_mhz=((162.4, 162.55),),
        bandwidth_khz=(5, 16),
        pattern="steady",
        channels_mhz=_grid(162.4, 162.55, 0.025),
    ),
    # -------------------------------------------------------------- aviation
    "aviation_nav": SignalType(
        name="Aviation navigation beacon (VOR, ILS)",
        short="VOR",
        band="Aviation navigation",
        ranges_mhz=((108, 118),),
        bandwidth_khz=(5, 50),
        pattern="steady",
    ),
    "airband_voice": SignalType(
        name="Aircraft or tower voice (AM)",
        short="Air",
        band="Aviation voice (airband)",
        ranges_mhz=((118, 137),),
        bandwidth_khz=(3, 25),
        pattern="intermittent",
    ),
    "aviation_dme": SignalType(
        name="Aviation radionavigation (DME, TACAN)",
        short="DME",
        band="Aviation radionavigation",
        ranges_mhz=((960, 1215),),
        bandwidth_khz=(100, 1500),
        pattern="intermittent",
    ),
    "transponder_interrogation": SignalType(
        name="Radar interrogating aircraft transponders",
        short="1030",
        band="Aircraft transponders",
        ranges_mhz=((1029, 1031),),
        bandwidth_khz=(500, 3000),
        pattern="intermittent",
        channels_mhz=(1030,),
    ),
    "ads_b": SignalType(
        name="ADS-B / transponder replies from aircraft",
        short="ADS-B",
        band="Aircraft transponders",
        ranges_mhz=((1089, 1091),),
        bandwidth_khz=(500, 3000),
        pattern="intermittent",
        channels_mhz=(1090,),
        note="Short pulses many times a second; near an airport, peak hold catches them easily.",
    ),
    # ------------------------------------------------------- two-way radio
    "cb_radio": SignalType(
        name="CB radio",
        short="CB",
        band="CB radio",
        ranges_mhz=((26.965, 27.405),),
        bandwidth_khz=(3, 10),
        pattern="intermittent",
        low_power=True,
    ),
    "amateur_6m": SignalType(
        name="Amateur radio (6 m)",
        short="Ham",
        band="6 m amateur",
        ranges_mhz=((50, 54),),
        bandwidth_khz=(0.5, 16),
        pattern="intermittent",
    ),
    "amateur_2m": SignalType(
        name="Amateur radio (2 m)",
        short="Ham",
        band="2 m amateur",
        ranges_mhz=((144, 148),),
        bandwidth_khz=(3, 16),
        pattern="intermittent",
    ),
    "marine_vhf": SignalType(
        name="Marine VHF radio",
        short="Marine",
        band="Marine VHF",
        ranges_mhz=((156, 162.025),),
        bandwidth_khz=(5, 16),
        pattern="intermittent",
    ),
    "amateur_70cm": SignalType(
        name="Amateur radio (70 cm)",
        short="Ham",
        band="70 cm amateur",
        ranges_mhz=((420, 450),),
        bandwidth_khz=(3, 25),
        pattern="intermittent",
    ),
    "frs_gmrs": SignalType(
        name="FRS/GMRS walkie-talkie",
        short="FRS",
        band="FRS/GMRS",
        ranges_mhz=((462.54, 462.73), (467.55, 467.73)),
        bandwidth_khz=(5, 20),
        pattern="intermittent",
        channels_mhz=_grid(462.55, 462.725, 0.0125) + _grid(467.5625, 467.7125, 0.025),
        low_power=True,
    ),
    "land_mobile_800": SignalType(
        name="Trunked land-mobile or public-safety radio",
        short="LMR",
        band="800 MHz land mobile",
        ranges_mhz=((806, 824), (851, 869)),
        bandwidth_khz=(6, 25),
        pattern="intermittent",
    ),
    # ---------------------------------------------- short-range devices (ISM)
    "remotes_315": SignalType(
        name="315 MHz device (key fob, garage remote, tire sensor)",
        short="315",
        band="315 MHz devices",
        ranges_mhz=((314.5, 315.5),),
        bandwidth_khz=(5, 300),
        pattern="intermittent",
        channels_mhz=(315.0,),
        low_power=True,
    ),
    "ism_433": SignalType(
        name="433 MHz device (weather sensor, key fob, doorbell)",
        short="433",
        band="433 MHz ISM",
        ranges_mhz=((433.05, 434.79),),
        bandwidth_khz=(5, 300),
        pattern="intermittent",
        channels_mhz=(433.92,),
        low_power=True,
        note="Most transmit a burst every 30-60 s, so capture for a minute or more.",
    ),
    "ism_915": SignalType(
        name="915 MHz device (LoRa, smart meter, Z-Wave, sensor)",
        short="915",
        band="915 MHz ISM",
        ranges_mhz=((902, 928),),
        bandwidth_khz=(20, 600),
        pattern="intermittent",
        low_power=True,
        note="Many hop across the band, so they look like scattered short bursts.",
    ),
    # ------------------------------------------------------------- cellular
    "cell_600_down": SignalType(
        name="Cell tower (600 MHz)",
        short="LTE",
        band="600 MHz cellular",
        ranges_mhz=((617, 652),),
        bandwidth_khz=(1400, 20500),
        pattern="steady",
    ),
    "cell_600_up": SignalType(
        name="Phone transmitting (600 MHz uplink)",
        short="Phone",
        band="600 MHz cellular",
        ranges_mhz=((663, 698),),
        bandwidth_khz=(180, 20500),
        pattern="intermittent",
        low_power=True,
    ),
    "cell_700": SignalType(
        name="Cell tower or public safety (700 MHz)",
        short="LTE",
        band="700 MHz cellular",
        ranges_mhz=((698, 806),),
        bandwidth_khz=(1400, 20500),
        pattern="any",
    ),
    "cell_850_up": SignalType(
        name="Phone transmitting (850 MHz uplink)",
        short="Phone",
        band="850 MHz cellular",
        ranges_mhz=((824, 849),),
        bandwidth_khz=(180, 20500),
        pattern="intermittent",
        low_power=True,
    ),
    "cell_850_down": SignalType(
        name="Cell tower (850 MHz)",
        short="LTE",
        band="850 MHz cellular",
        ranges_mhz=((869, 894),),
        bandwidth_khz=(1400, 20500),
        pattern="steady",
    ),
    "cell_aws_up": SignalType(
        name="Phone transmitting (AWS uplink)",
        short="Phone",
        band="AWS cellular",
        ranges_mhz=((1710, 1780),),
        bandwidth_khz=(180, 20500),
        pattern="intermittent",
        low_power=True,
    ),
    "cell_pcs_up": SignalType(
        name="Phone transmitting (PCS uplink)",
        short="Phone",
        band="PCS cellular",
        ranges_mhz=((1850, 1915),),
        bandwidth_khz=(180, 20500),
        pattern="intermittent",
        low_power=True,
    ),
    "cell_pcs_down": SignalType(
        name="Cell tower (PCS)",
        short="LTE",
        band="PCS cellular",
        ranges_mhz=((1930, 1995),),
        bandwidth_khz=(1400, 20500),
        pattern="steady",
    ),
    "cell_aws_down": SignalType(
        name="Cell tower (AWS)",
        short="LTE",
        band="AWS cellular",
        ranges_mhz=((2110, 2200),),
        bandwidth_khz=(1400, 20500),
        pattern="steady",
    ),
    "cell_2500": SignalType(
        name="5G/LTE cell tower (2.5 GHz)",
        short="5G",
        band="2.5 GHz cellular",
        ranges_mhz=((2496, 2690),),
        bandwidth_khz=(5000, 100500),
        pattern="steady",
    ),
    "cell_3450": SignalType(
        name="5G cell tower (3.45 GHz)",
        short="5G",
        band="3.45 GHz cellular",
        ranges_mhz=((3450, 3550),),
        bandwidth_khz=(10000, 100500),
        pattern="steady",
    ),
    "cbrs": SignalType(
        name="CBRS (private LTE/5G, fixed wireless)",
        short="CBRS",
        band="CBRS",
        ranges_mhz=((3550, 3700),),
        bandwidth_khz=(5000, 40500),
        pattern="any",
    ),
    "c_band_5g": SignalType(
        name="5G cell tower (C-band)",
        short="5G",
        band="C-band 5G",
        ranges_mhz=((3700, 3980),),
        bandwidth_khz=(10000, 100500),
        pattern="steady",
    ),
    # ------------------------------------------------------------ satellite
    "weather_satellite": SignalType(
        name="Weather satellite downlink (NOAA APT, Meteor)",
        short="Sat",
        band="Weather satellites",
        ranges_mhz=((137, 138),),
        bandwidth_khz=(30, 150),
        pattern="intermittent",
        note="Only during a 10-15 minute pass overhead.",
    ),
    "emergency_beacon": SignalType(
        name="Emergency locator beacon (EPIRB, PLB)",
        short="406",
        band="Emergency beacons",
        ranges_mhz=((406, 406.1),),
        bandwidth_khz=(1, 10),
        pattern="intermittent",
    ),
    "gnss": SignalType(
        name="GNSS (GPS, Galileo, GLONASS)",
        short="GNSS",
        band="GNSS",
        ranges_mhz=((1164, 1189), (1215, 1240), (1559, 1610)),
        bandwidth_khz=(2000, 24000),
        pattern="steady",
        note=(
            "Satellite navigation arrives below the noise floor, so a sweep should not "
            "see it. A visible signal here is unusual: a GPS repeater, a jammer, or interference."
        ),
    ),
    "inmarsat": SignalType(
        name="L-band satellite downlink (Inmarsat)",
        short="Sat",
        band="L-band satellite",
        ranges_mhz=((1525, 1559),),
        bandwidth_khz=(1, 200),
        pattern="steady",
    ),
    "iridium": SignalType(
        name="Iridium satellite phone or pager",
        short="Irid",
        band="Iridium",
        ranges_mhz=((1616, 1626.5),),
        bandwidth_khz=(30, 50),
        pattern="intermittent",
    ),
    # ---------------------------------------------------- 2.4 GHz ISM band
    "wifi_24": SignalType(
        name="Wi-Fi (2.4 GHz)",
        short="Wi-Fi",
        band="2.4 GHz ISM",
        ranges_mhz=((2401, 2495),),
        bandwidth_khz=(15000, 42000),
        pattern="any",
        channels_mhz=(*_grid(2412, 2472, 5), 2484),
        low_power=True,
        note="20 MHz wide, usually on channel 1, 6 or 11 (2412, 2437, 2462 MHz).",
    ),
    "ble_advertising": SignalType(
        name="Bluetooth Low Energy advertising",
        short="BLE",
        band="2.4 GHz ISM",
        ranges_mhz=((2400, 2483.5),),
        bandwidth_khz=(500, 1500),
        pattern="intermittent",
        channels_mhz=(2402, 2426, 2480),
        low_power=True,
        note=(
            "BLE devices announce themselves on exactly three channels (37, 38 and 39, "
            "at 2402, 2426 and 2480 MHz): phones, trackers, watches, beacons. "
            "Advertising always uses the 1 Mbit/s PHY, so it is about 1 MHz wide."
        ),
    ),
    "bluetooth_hopping": SignalType(
        name="Bluetooth, hopping (Classic BR/EDR, or a BLE connection)",
        short="BT",
        band="2.4 GHz ISM",
        ranges_mhz=((2400, 2483.5),),
        bandwidth_khz=(30000, 84000),
        pattern="intermittent",
        low_power=True,
        note=(
            "Connected Bluetooth jumps across the whole band: Classic (headphones, car audio) "
            "uses 79 channels 1600 times a second, BLE connections use 37. In peak hold "
            "this looks like a carpet from 2402 to 2480 MHz that no single bin holds for long. "
            "A sweep cannot reliably tell Classic from a BLE connection."
        ),
    ),
    "zigbee": SignalType(
        name="Zigbee / Thread / Matter (smart home)",
        short="Zigbee",
        band="2.4 GHz ISM",
        ranges_mhz=((2400, 2483.5),),
        bandwidth_khz=(1600, 3000),
        pattern="intermittent",
        channels_mhz=_grid(2405, 2480, 5),
        low_power=True,
        note="2 MHz wide, on 5 MHz steps from 2405 MHz (channels 11-26).",
    ),
    "microwave_oven": SignalType(
        name="Microwave oven leakage",
        short="Oven",
        band="2.4 GHz ISM",
        ranges_mhz=((2400, 2500),),
        bandwidth_khz=(5000, 60000),
        pattern="intermittent",
        note="Wide, messy and on only while the oven runs, usually near 2450-2470 MHz.",
    ),
    # ------------------------------------------------------- 5 GHz and up
    "wifi_5": SignalType(
        name="Wi-Fi (5 GHz)",
        short="Wi-Fi",
        band="5 GHz Wi-Fi",
        ranges_mhz=((5150, 5895),),
        bandwidth_khz=(15000, 165000),
        pattern="any",
        channels_mhz=(*_grid(5180, 5320, 20), *_grid(5500, 5720, 20), *_grid(5745, 5885, 20)),
        low_power=True,
    ),
    "fpv_video_58": SignalType(
        name="Analog FPV / wireless camera video (5.8 GHz)",
        short="FPV",
        band="5.8 GHz ISM",
        ranges_mhz=((5645, 5945),),
        bandwidth_khz=(8000, 22000),
        pattern="steady",
        channels_mhz=(5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917),
        low_power=True,
        note="Analog video transmits continuously, so it looks steady and about 18 MHz wide.",
    ),
    "wifi_6": SignalType(
        name="Wi-Fi 6E (6 GHz)",
        short="Wi-Fi",
        band="6 GHz Wi-Fi",
        ranges_mhz=((5925, 7125),),
        bandwidth_khz=(15000, 330000),
        pattern="any",
        low_power=True,
        note="Mostly above the HackRF's 6 GHz limit.",
    ),
}


def candidates(freq_hz: float, slack_hz: float = 0.0) -> dict[str, SignalType]:
    """Every catalog entry whose ranges contain freq_hz."""
    return {
        key: kind
        for key, kind in CATALOG.items()
        if kind.range_containing(freq_hz, slack_hz) is not None
    }


def lookup(freq_hz: float) -> str | None:
    """Name of the narrowest allocation containing freq_hz, or None.

    Narrowest wins because allocations nest: 433.92 MHz is inside both the
    70 cm amateur band and the 433 MHz ISM band, and the second one says more
    about what you are probably looking at.
    """
    best: tuple[float, str] | None = None
    for kind in CATALOG.values():
        found = kind.range_containing(freq_hz)
        if found is not None:
            width = found[1] - found[0]
            if best is None or width < best[0]:
                best = (width, kind.band)
    return best[1] if best else None
