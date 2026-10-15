# Near-field probing with rfscoper

A wide-area sweep tells you *what* is transmitting. A near-field probe tells
you *where*. This guide covers the probe itself (buy or build), how to protect
the HackRF while using it, and the method `rfscoper probe` is built around.

## Why it works

Radio waves spread out. Far from a transmitter, moving a meter changes the
signal very little: a cell tower reads about the same at every spot in a
room. Close in, inside roughly one wavelength, the field falls off steeply,
by the square or cube of the distance. A small loop antenna held 2 cm from a
transmitter picks up a signal tens of dB stronger than it does at 20 cm.

So the test is a difference, not a level: **a signal that is 20-40 dB stronger
with the probe against one object than in open air is coming from that
object.** A signal that reads the same everywhere is coming from far away,
however strong it is.

A loop is also deaf to signals that a whip antenna hears well. That is a
feature: it keeps the FM stations and cell towers small, which makes the
local spike easier to see.

## The probe

### Buy

Any shielded **H-field (magnetic) near-field probe** works. Sets sold for
EMC debugging usually include a few loop sizes plus one E-field stub, cost
somewhere in the tens of dollars, and come with an SMA connector that
plugs straight into the HackRF. A bigger loop (about 20 mm) is more sensitive;
a smaller one (5 mm) points more precisely. Start with a mid-sized loop.

### Build (20 minutes)

A classic shielded loop, after the design in
[DIY near-field probes](https://interferencetechnology.com/diy-near-field-probes-preamplifiers/):

1. Take 15-20 cm of thin 50 Ω coax. Semi-rigid (0.086") holds its shape best;
   ordinary RG-316 works if you stiffen it with heat-shrink.
2. Put an SMA male connector on one end.
3. Bend the other end into a loop about 15-20 mm across, and solder the end of
   the loop back onto the cable's shield where the loop closes, so the shield
   forms a closed ring. Solder the center conductor at the tip to the shield
   as well.
4. Cut a 1-2 mm gap in the *shield only* at the top of the loop, opposite the
   feed point. Don't cut the center conductor. The gap is what makes it a
   magnetic probe: without it the shield shorts out the loop.
5. Cover the loop in heat-shrink or liquid tape so it can't short against
   anything you touch it to.

An E-field probe is even simpler: the same coax with 10 mm of center
conductor sticking out of the end and the shield trimmed back. It responds
to voltage rather than current, so it picks up different things (switching
power supplies, unshielded wiring). The loop is the one to start with.

### Preamplifier (optional)

Most small transmitters are easy to hear from a few centimeters away without
one. A low-noise amplifier (LNA) between the probe and the HackRF helps with
weak, quiet devices, but it also amplifies everything else, so remember the
input limit below. Check that a spike disappears when you pull the probe
away; if it stays, it's ambient pickup, not the object.

## Protect the HackRF

The HackRF One's documented limit is **-5 dBm at the antenna port; exceeding
it can cause permanent damage** ([HackRF One docs](https://hackrf.readthedocs.io/en/latest/hackrf_one.html)).
A probe held directly against a transmitting antenna can reach that.

- **Never enable the RF amplifier (`--amp`) while probing.** The `probe`
  commands don't offer it. With the amp off the documented safe input rises
  to 10 dBm.
- **Keep the LNA gain low.** `rfscoper probe` defaults to 8 dB (the wide
  sweeps use 24). You don't need gain when the source is centimeters away.
- **Don't press the probe against the antenna of a known transmitter** such as
  your own phone or router while it is active. Probe from a few centimeters.
- **Transmit nothing.** `hackrf_sweep` only receives, and so does rfscoper.

## Method

### 1. Set up

- Fit the probe (no whip antenna) and check `hackrf_info` sees the HackRF.
- Leave the room's known devices in their normal state and list them. Every
  hot spot you find gets checked against that list first.
- Decide on the band. The default `300:2500` MHz covers where small
  transmitters live (315/433 MHz remotes and sensors, 915 MHz ISM, cellular
  uplink, 2.4 GHz). Add `--range 2400:2500 --bin 100k` as a second pass
  when you care about Bluetooth vs Zigbee.

### 2. Reference

```bash
rfscoper probe start office
```

Hold the probe in open air at the center of the room, at arm's length from
everything, for the 10 seconds of the capture. This is what "not from any
object" looks like.

### 3. Spots

```bash
rfscoper probe spot office "desk lamp"
rfscoper probe spot office "power strip"
rfscoper probe spot office "smoke detector"
rfscoper probe spot office "picture frame"
```

For each object: hold the loop flat against it, move slowly over its surface
for the first few seconds, then hold still. After each spot rfscoper prints
anything that is stronger here than at the reference. Name spots the way
you'll remember them.

What to probe, in rough order of how often small transmitters are hidden
there: things plugged into mains power (strips, chargers, lamps, clocks,
smoke detectors), things with their own battery compartment, anything you
didn't put in the room yourself, furniture edges and undersides, and
wall plates and vents.

### 4. Report

```bash
rfscoper probe report office
```

Every signal seen anywhere in the session gets a gain figure per spot, a
verdict, and a best guess at what it is:

```text
       MHz  ref dB  +desk lamp  +power strip  +smoke detect  +picture fram  verdict                                    likely
  2426.000   -64.8         +10           +44            +11            +10  HOT at power strip (+44 dB)                Bluetooth Low Energy advertising
   315.000   -69.0           0             0            +29             +1  HOT at smoke detector (+29 dB)             315 MHz device (key fob, garage remote, tire sensor)
   433.900   -57.6           0             0              0              0  same everywhere: not from any probed spot  433 MHz device (weather sensor, key fob, doorbell)
   462.600   -52.0           0             0              0              0  same everywhere: not from any probed spot  unknown (FRS/GMRS)
  2437.000   -53.9           0             0              0              0  same everywhere: not from any probed spot  Wi-Fi (2.4 GHz)
```

- **HOT (20 dB or more above the reference)**: the source is in or on that
  object. Open it up and look.
- **warm (10-20 dB)**: probably nearby but not inside; probe the objects
  around it.
- **same everywhere**: a distant signal. However strong it is, it's not
  coming from anything you probed.

The report also saves a heatmap (`report.png`) of signals against spots.

### 5. Live meter

For hunting rather than surveying, `live` prints one bar per sweep while
you move the probe, like a Geiger counter:

```bash
rfscoper probe live --watch 433.92M
```

Watching a single frequency narrows the sweep to the 20 MHz around it, so
the bar updates several times a second. The bar's scale is set by the first
sweep; the number on the right is the level in dB. Move toward whatever
makes it grow. Without `--watch` it shows the strongest signal in the whole
range on each sweep, which is useful for a first pass over a wall or a desk.

### 6. The scope window

The most direct way to hunt is the live window:

```bash
rfscoper scope --near --range 400:480 --lna 0 --vga 10 --watch 433.92M
```

1. Hold the probe in open air and press **Take reference** (it averages the
   next 8 sweeps).
2. Move the probe slowly over each object. The Geiger meter shows how far the
   watched frequency is above the reference: orange at +10 dB, red **HOT** at
   +20 dB. Tick **Clicks** to hear it instead of watching.
3. Without a frequency in mind, press **Follow strongest**: the meter follows
   whichever signal is loudest. The **gain** column in the signals table shows
   every signal's rise over the reference at once; click a row to watch it.
4. Press **Record 30 s** at a hot spot to keep it as a capture for the report.

With the 30 dB preamp in line, keep the LNA at 0 and the VGA low (10-20 dB),
and back the VGA off if the spectrum's top trace flattens out.

Try the whole workflow without hardware: `rfscoper scope --demo --near
--range 400:480`, then drag the demo's probe-distance slider toward
*touching*.

## Reading the results honestly

- **A probe finds things that transmit.** A device that only records, or
  that transmits once a day, does not light up a 10-second capture. Probe the
  likely objects for longer (`--duration 60`) and make noise in the room to
  trigger voice-activated devices.
- **Hot doesn't mean hostile.** The hottest spot in most rooms is a known
  device: a phone, a smart plug, a wireless mouse. Check your list first.
- **Switching power supplies are loud up close**, mostly below 100 MHz with
  harmonics above. They show up on the E-field probe especially. A steady
  comb of signals from a charger is normal.
- **dB are relative**, not calibrated dBm. Compare spots against the
  reference taken with the same probe and gains, never against another
  session.

## Checklist

```text
[ ] probe fitted, no whip antenna, --amp never used
[ ] hackrf_info sees the device
[ ] list of known transmitters in the room
[ ] rfscoper probe start NAME       reference, open air, mid-room
[ ] rfscoper probe spot NAME ...    every suspect object, slow then still
[ ] rfscoper probe report NAME      HOT spots: open and inspect
[ ] rfscoper probe live --watch F   to pin down exactly where
[ ] rfscoper scope --near           or hunt live with the meter and waterfall
```
