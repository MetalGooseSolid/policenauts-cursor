# policenauts-cursor

Drive a Sega Saturn game's on-screen cursor with your **host mouse**, by writing
the cursor position straight into the emulated console's RAM over RetroArch's UDP
network-command interface.

Built while making the point-and-click adventure **Policenauts** playable with a
mouse under [RetroArch](https://www.retroarch.com/) + the **Beetle Saturn** core
on macOS — but nothing here is Policenauts-specific. It works for any Saturn game
whose cursor coordinates live in Work RAM.

## Why

The Saturn mouse is a *relative* device: the game integrates motion deltas into a
cursor position it tracks in its own Work RAM. Emulating that on a host trackpad
feels sluggish and imprecise. This tool sidesteps the integration entirely — it
finds the RAM address holding the cursor's X/Y and writes an **absolute** position
derived from where your host cursor sits inside the RetroArch window, ~60 times a
second. The result is a 1:1 hardware-mouse feel.

## How it works

RetroArch exposes a UDP network-command interface (default `127.0.0.1:55355`).
Beetle Saturn defines **no system memory map**, so `READ_CORE_MEMORY` is
unavailable ("no memory map defined"); this tool uses `READ_CORE_RAM` /
`WRITE_CORE_RAM`, which address the flat RetroAchievements space. Saturn Work RAM
spans `0x000000..0x1FFFFF` there:

| Region | Range | Notes |
| --- | --- | --- |
| LWRAM (Low Work RAM)  | `0x000000`–`0x0FFFFF` | 1 MB |
| HWRAM (High Work RAM) | `0x100000`–`0x1FFFFF` | 1 MB — game state usually lives here |

The bridge reads your host cursor and the RetroArch window rect via Quartz
(macOS), maps the cursor's position within the (optionally inset-cropped) game
viewport to a game coordinate, and writes it to the cursor address(es) each frame.

## Requirements

- **RetroArch** with the **Beetle Saturn** core, network commands enabled.
- **Python 3.9+**. Discovery commands (`probe`, `track`, `scan`, `peek`, `watch`,
  `poke`) are pure stdlib. Only the live `run`/`calibrate` bridge needs macOS +
  [pyobjc](https://pypi.org/project/pyobjc/):
  ```sh
  pip install pyobjc-framework-Quartz
  ```

Enable network commands in `retroarch.cfg` (or via Settings → Network):
```
network_cmd_enable = "true"
network_cmd_port   = "55355"
```

## Setup

```sh
python3 policenauts_cursor.py probe   # always run this first
```

`probe` confirms the connection, detects the working command family, and shows
which RAM bases respond.

## Finding the cursor address

Two complementary search strategies — try `track` first.

### `track` — find X/Y by motion (recommended)
Direction- and encoding-agnostic; covers all of Work RAM. Samples while the cursor
is held still, then while you sweep it, and keeps addresses that are quiet at rest
but move (near-)monotonically during the sweep.
```sh
python3 policenauts_cursor.py track          # full 2 MB WRAM
# Find X with a LEFT<->RIGHT sweep; find Y with an UP<->DOWN sweep.
```

### `scan` — classic interactive RAM search
Cheat-engine style: move the cursor, then tell the tool how the value changed
(`i`/`d`/`s`/`c`/`=N`) until one address survives.
```sh
python3 policenauts_cursor.py scan --base 100000 --length 0x100000
```

### Verify a candidate
```sh
python3 policenauts_cursor.py watch 14b5f4   # value should sweep smoothly
python3 policenauts_cursor.py peek  14b5f4   # one-shot read, hex + decimal
```

### Prove writes stick (go/no-go gate)
```sh
python3 policenauts_cursor.py poke 14b5f4 00 80
```
If the read-back doesn't match, the core/build won't honor network writes (or the
game overwrites the address every frame) — and the memory-write approach can't
work for that game.

## Running the bridge

Calibrate the window→game-image insets (skips the macOS title bar and any
letterbox/pillarbox bars) by pointing at two corners of the game image:
```sh
python3 policenauts_cursor.py calibrate --config cursor.json
```

Then go live:
```sh
python3 policenauts_cursor.py run --config cursor.json
```
Move your mouse inside the RetroArch window; the in-game cursor follows. `Ctrl-C`
to stop. Add `--debug` to print the mapped fractions and written values.

## Configuration

Copy [`cursor.example.json`](cursor.example.json) to `cursor.json` and fill in the
addresses and calibrated extremes you found above. The included
[`cursor.json`](cursor.json) holds the values calibrated for Policenauts on this
setup — treat it as a worked reference; your addresses and insets may differ.

| Key | Meaning |
| --- | --- |
| `x_addr`, `y_addr` | Cursor address(es), hex. A list writes every mirror copy. |
| `width`, `endian`, `signed` | Value encoding (Saturn is SH-2; `width: 2` is typical). |
| `x_min`/`x_max`, `y_min`/`y_max` | Calibrated coordinate extremes (from `watch`). |
| `x_invert`, `y_invert` | Flip an axis if the cursor moves the wrong way. |
| `inset_*` | Fractional crop of the window to the game viewport (from `calibrate`). |
| `rate_hz` | Update rate (default 60). |
| `window_owner` | Window to track (default `RetroArch`). |

CLI flags (`--x-addr`, `--y-addr`, `--width`, `--endian`, `--rate-hz`,
`--window-owner`) override the config file.

## Tests

Pure-logic tests — no RetroArch, no macOS needed:
```sh
python3 test_logic.py
```

## Commands at a glance

| Command | Purpose |
| --- | --- |
| `probe` | Test the connection; show which RAM bases respond. **Run first.** |
| `track` | Find cursor X/Y by motion (quiet at rest, moves on sweep). |
| `scan`  | Interactive RAM search for X/Y. |
| `watch` | Continuously print a value while you sweep — reads its min/max. |
| `peek`  | Read N bytes once (hex + decimal interpretations). |
| `poke`  | Write bytes and read back to prove writes stick. |
| `calibrate` | Capture window→game-image insets from two corner points. |
| `run`   | The live bridge: host cursor → game cursor, ~60 Hz (macOS). |
