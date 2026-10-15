# How rfscoper works: a guided tour of the code

This walks through what happens between `rfscoper capture room` and the
`likely` column, one file at a time, with the real code. Each section ends
with something to try in a Python shell (`python3`, then the `>>>` prompt).

Before starting, make some data to poke at:

```bash
python examples/make_demo_data.py
python3
```

```python
>>> from pathlib import Path
>>> from rfscoper import load_sweeps
>>> data = load_sweeps(Path.home() / "rfscoper-captures/demo-2g4.csv")
```

---

## The big picture

```text
 HackRF  ──►  hackrf_sweep  ──►  CSV rows  ──►  matrix  ──►  spectrum  ──►  signals  ──►  guesses
            (capture.py)                   (parser.py)     (analysis.py)              (identify.py)
```

Every stage turns one shape of data into another. If you can say what shape
the data has at each arrow, you understand the program.

---

## 1. The radio: what `hackrf_sweep` actually does

The HackRF can only listen to about 20 MHz at a time. To cover 2400-2500 MHz
it **retunes in steps**, and at each step it:

1. records a short burst of IQ samples (complex numbers: the wave's amplitude and phase),
2. runs an FFT, which turns "the wave over time" into "how much power at each frequency",
3. keeps two clean 5 MHz slices of that window, dropping the edges (where the
   filter rolls off) and the center (where the hardware leaks a false spike, the "DC spike"),
4. prints each slice as one CSV row.

One full pass over the requested range is **one sweep**. Here is a real row
from a capture taken with `--bin 250k`:

```text
2026-10-14, 08:10:39.335650, 2455000000, 2460000000, 238095.24, 84, -70.43, -65.33, ...
   date      time (to µs)     low edge    high edge   bin width  FFT  21 dB values, one per bin
```

Three numbers in that row are tied together, and they explain why the bin
width is 238,095.24 Hz instead of the 250,000 that was asked for:

- **FFT size (84).** `hackrf_sweep` divides its 20 MHz sample rate by the
  requested width: 20,000,000 // 250,000 = 80. Its interleaved slicing needs
  FFT size + 4 to be a multiple of 8, so it rounds 80 up to **84**. FFT sizes
  are always 4, 12, 20, 28 ... (8k + 4).
- **Bin width (238,095.24 Hz).** 20,000,000 / 84. Pick a width that is already
  20 MHz divided by an 8k+4 number (`1M`, `200k`, `40k`) and it comes out exact.
- **Number of values (21).** Each row is a quarter of the FFT: 84 / 4 = 21
  values, covering 21 × 238,095.24 Hz = exactly 5 MHz, the high edge minus the low edge.

The time is when that slice was measured. A 100 MHz range is 20 slices, so
20 rows make one sweep; on a Raspberry Pi that took about 0.15 s.

`rfscoper info NAME` shows all of this for any capture: what you requested,
what `hackrf_sweep` actually used, and what ended up in the file.

### Counted vs. timed captures (`capture.py`)

With `-N 30`, `hackrf_sweep` stops by itself after 30 sweeps. Without `-N` it
runs forever, which is how `--duration` works: rfscoper starts it, waits, then
stops it.

```python
def _run_for(cmd: list[str], seconds: float) -> tuple[bool, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, stderr = proc.communicate(timeout=seconds)
        return False, stderr  # it exited on its own: that's an error
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGINT)  # the same signal as pressing Ctrl+C
        ...
        return True, stderr
```

Three ideas worth noticing:

- **`Popen` vs. `run`.** `subprocess.run` waits for a program to finish.
  `Popen` starts it and hands back control, so we can stop it ourselves.
- **The timeout is the plan, not a failure.** `communicate(timeout=20)` raises
  `TimeoutExpired` after 20 seconds, and *that* is the normal path. If the
  program exits *before* the timeout, something went wrong (unplugged HackRF).
- **`SIGINT`, not `kill`.** `kill` stops a program instantly, possibly halfway
  through writing a line. `SIGINT` politely asks it to stop; `hackrf_sweep`
  catches it, finishes its line and closes the file.

**Try it:** `rfscoper capture test --range 88:108 --duration 20 --dry-run`
prints the command without running it. Notice there is no `-N`.

---

## 2. From CSV rows to a matrix (`parser.py`)

The rows arrive **in the order the radio visited them**, which is not
frequency order, and one sweep is spread over many rows. The parser has to
put the puzzle back together.

### Reading one row

```python
def parse_row(row: list[str]) -> SweepRow | None:
    if len(row) < 7:
        return None
    try:
        hz_low = int(float(row[2]))
        bin_width = float(row[4])
        fft_size = int(float(row[5]))
        powers = [float(value) for value in row[6:]]
    except ValueError:
        return None  # header, truncated line, or garbage

    freqs = [round(hz_low + (i + 0.5) * bin_width) for i in range(len(powers))]
```

- `row[6:]` is a **slice**: everything from index 6 to the end, which is all
  the dB values however many there are.
- The bin width comes from the row itself (`row[4]`), never from what was
  requested, since the two can differ.
- `(i + 0.5) * bin_width` puts each frequency at the **center** of its bin.
  In the row above, bin 3 covers 2455.714-2455.952 MHz, so its center is
  2,455,000,000 + 3.5 × 238,095.24 = 2455.833 MHz.
- `round(...)` matters more than it looks. Floating-point math can produce
  `2455833333.3400002`, and since these frequencies become dictionary keys,
  the same bin could end up as two different keys. Rounding to whole Hz makes
  every key exact.
- Returning `None` for bad rows instead of crashing means one truncated line
  (from a capture stopped mid-write) doesn't ruin the whole file.

### Finding where each sweep starts

```python
for raw in csv.reader(f):
    row = parse_row(raw)
    if row is None:
        continue

    if row.hz_low in seen_lows:  # we've been here already this sweep:
        sweeps.append(current)  # the radio wrapped around, so the
        current, seen_lows = {}, set()  # previous sweep is complete

    seen_lows.add(row.hz_low)
    current.update(zip(row.freqs_hz, row.powers_db, strict=True))
```

The trick: within one sweep, the radio visits each slice exactly once. So
**the moment a slice repeats, a new sweep has begun.** This works no matter
what order the slices come in.

- `seen_lows` is a **set**: checking `x in some_set` is instant, however big it gets.
- `current` is a **dict** from frequency to dB. `zip` pairs each frequency
  with its value, and `update` adds the pairs. `strict=True` makes `zip`
  raise an error if the two lists have different lengths, instead of silently
  dropping data.

### Building the matrix

```python
all_freqs = sorted(set().union(*sweeps))  # every frequency seen, in order
column = {freq: i for i, freq in enumerate(all_freqs)}  # frequency -> column number

power = np.full((len(sweeps), len(all_freqs)), np.nan)
for s, sweep in enumerate(sweeps):
    for freq, db in sweep.items():
        power[s, column[freq]] = db
```

The matrix starts full of `NaN` ("not a number"), then each measurement is
dropped into its row (the sweep) and column (the frequency). Anything never
measured stays `NaN`, which is honest: it means "no data", where a made-up
value like -200 dB would quietly poison every average later.

**Try it:**

```python
>>> data.power_db.shape        # (sweeps, bins)
(397, 420)
>>> data.freqs_hz[:3]          # first three bin centers
>>> data.power_db[0]           # sweep 0: one whole spectrum
>>> data.power_db[:, 104]      # one frequency over time: a column
```

Rows are moments in time; columns are frequencies. Hold on to that picture,
because everything else is arithmetic on rows and columns.

---

## 3. From a matrix to signals (`analysis.py`)

### Collapsing time: peak vs. mean

```python
np.nanmax(data.power_db, axis=0)  # peak hold
np.nanmean(data.power_db, axis=0)  # average
```

`axis=0` means "collapse along the rows", which squashes all the sweeps into
one value per column. The `nan` versions skip the `NaN`s instead of letting
one missing value turn a whole column into `NaN`.

- **Peak hold** keeps the loudest moment of every frequency. A key fob that
  pressed once in 30 sweeps still shows up.
- **Mean** shows what each frequency usually looks like. Brief signals fade into it.

**Try it:** for the Zigbee bin, compare `peak[i]` with `mean[i]`, where
`i = abs(data.freqs_hz - 2450e6).argmin()`. A big gap means intermittent.

### The noise floor, two ways

To find signals we need to know what *no signal* looks like at each
frequency. rfscoper estimates that twice, because each method has a blind spot.

**Across frequency** (`estimate_floor`): slide a window of about 60 MHz along
the spectrum and take a low percentile of each window. Signals are a minority
of the bins in most windows, so the low percentile lands on noise.
*Blind spot:* if a signal fills most of the window, it becomes the "noise".
Hopping Bluetooth does exactly that.

**Across time** (`time_floor`): for each frequency, take its quietest sweeps
(the 10th percentile down a column). Anything that isn't on *all* the time
leaves some quiet sweeps behind. *Blind spot:* a signal that never turns off,
like an FM station, has no quiet sweeps.

```python
def signal_floor(data, stat="peak"):
    across_freq = estimate_floor(data.freqs_hz, spectrum(data, stat))
    return np.fmin(across_freq, time_floor(data, stat))
```

Each one is blind exactly where the other can see, so taking the **lower of
the two** at each frequency covers both. That is a pattern that comes up all
over engineering: combine two imperfect estimators whose failures don't overlap.

### Finding the signals

```python
margin = spec - floor
mask = margin >= threshold_db  # True where a bin stands out
for start, stop in _runs(mask):  # each run of consecutive True bins
    ...  # is one signal
```

A boolean mask turns "how loud?" into "loud enough?". Consecutive `True`
bins are one signal: an FM station or a Wi-Fi channel spans many bins. Each
signal keeps its **edges** (so we know its width) and its **strongest bin**.

`_runs` finds the runs with a neat trick. Pad the mask with `False` on both
ends, convert to 0/1, and take the difference between neighbors: every run
starts where the difference is +1 and ends where it is -1.

```text
mask       0 0 1 1 1 0 0 1 0
diff        0 +1 0 0 -1 0 +1 -1     → runs at [2, 5) and [7, 8)
```

**Try it:**

```python
>>> from rfscoper.analysis import spectrum, signal_floor, find_peaks
>>> peak = spectrum(data, "peak")
>>> for s in find_peaks(data.freqs_hz, peak, signal_floor(data), 10):
...     print(s.center_hz / 1e6, s.width_hz / 1e6, round(s.margin_db, 1))
```

### Duty cycle

For each signal, look down its column and count the sweeps where it was
clearly above the noise:

```python
np.mean(column >= level_db)  # True counts as 1, False as 0
```

The mean of a boolean array is the fraction that is `True`. That one line is
the whole duty cycle.

---

## 4. From signals to guesses (`bands.py` and `identify.py`)

### The catalog is data

`bands.py` holds a dictionary. Each entry describes one kind of signal by
what a sweep can see:

```python
"ble_advertising": SignalType(
    name="Bluetooth Low Energy advertising",
    band="2.4 GHz ISM",
    ranges_mhz=((2400, 2483.5),),
    bandwidth_khz=(500, 1500),
    pattern="intermittent",
    channels_mhz=(2402, 2426, 2480),
    low_power=True,
),
```

Keeping the catalog as plain data, separate from the logic that uses it,
means adding a signal type is one new entry and no code changes. The
`@dataclass(frozen=True)` makes entries read-only, so nothing can modify the
catalog by accident while the program runs.

### Scoring is evidence

`identify.py` gathers every entry whose range covers the signal, gives each
one a base score of 0.40, and runs up to three checks:

| Check | For | Against |
|---|---|---|
| On one of its channels? | +0.25 | -0.20 |
| Width in the expected range? | +0.20 | -0.25 |
| Steady or intermittent, as expected? | +0.15 | -0.15 |

0.75 and above is "likely", 0.50 and above is "possible".

The most important design rule: **a check only counts if the sweep can
actually measure it.** With 1 MHz bins, a 200 kHz FM station and a 50 kHz
walkie-talkie both look one bin wide, so the width check scores 0 for both
instead of pretending to know. A tool that admits what it can't see is more
trustworthy than one that always has an answer.

**Try it:**

```python
>>> from rfscoper.identify import identify
>>> for g in identify(2426e6, 1e6, 250e3, duty=0.3)[:3]:
...     print(g.score, g.kind.name)
...     for r in g.reasons:
...         print("   ", r.effect, r.text)
```

Then change one input at a time and watch the scores move: `2433e6` (off the
BLE channels), width `20e6` (Wi-Fi-sized), `duty=1.0` (always on).

---

## Exercises

1. **Read the matrix.** In the headphones demo, pick a frequency inside the
   Bluetooth carpet and print its column. In how many sweeps is it loud?
2. **Break a floor on purpose.** For the headphones demo, compute
   `peak - estimate_floor(...)` and `peak - signal_floor(...)` at 2460 MHz.
   Which one sees the carpet?
3. **Add a signal type.** Add a catalog entry for 2.4 GHz analog wireless
   cameras (channels 2414, 2432, 2450 and 2468 MHz, about 18 MHz wide,
   steady), then write a test in `tests/test_identify.py` that expects it.
4. **Find the limit.** 2480 MHz is both BLE advertising channel 39 and Zigbee
   channel 26, so only width can tell them apart. Use
   `rfscoper identify 2480 --width 1M --duty 0.3 --bin 250k` and the same with
   `--width 2M`, then raise `--bin`. What is the coarsest bin width at which
   the two get different answers, and why does it stop working after that?
