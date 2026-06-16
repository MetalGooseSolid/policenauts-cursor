#!/usr/bin/env python3
"""
policenauts_cursor.py — drive a Sega Saturn game's internal cursor from the
host mouse, via RetroArch's UDP network-command interface (Beetle Saturn core).

The Saturn mouse is a *relative* device: the game integrates motion deltas into
a cursor position it tracks in its own Work RAM. That integration is what makes
mouse control feel clunky in emulation. This tool sidesteps it entirely:
it finds the RAM address holding the cursor X/Y and writes an *absolute*
position derived from where your host cursor sits inside the RetroArch window.

It talks to RetroArch over UDP (default 127.0.0.1:55355). Beetle Saturn exposes
NO system memory map, so READ_CORE_MEMORY is unavailable ("no memory map
defined"); this tool uses READ_CORE_RAM / WRITE_CORE_RAM instead, which address
the RetroAchievements flat space (Saturn work RAM = 0x000000..0x1FFFFF).

Subcommands (run in this order the first time):
  probe    Test the connection, detect the working command family, and show
           which Saturn RAM bases respond.  ALWAYS RUN THIS FIRST.
  watch    Continuously print the value at an address. Use it to find the
           cursor's min/max by moving the cursor to the screen extremes.
  peek     Read N bytes once and print them (hex + decimal interpretations).
  poke     Write bytes, then read them back to PROVE writes stick. This is the
           go/no-go gate before building anything on top of it.
  track    Robustly FIND cursor X/Y by motion: hold still, then sweep — finds
           addresses quiet at rest but moving with the cursor. Try this first;
           it covers all of work RAM and doesn't assume direction or encoding.
  scan     Interactive RAM search to FIND the cursor X and Y addresses.
  run      The live bridge: host cursor position -> game cursor, ~60 Hz.

Only `run` needs macOS (pyobjc Quartz); everything else is pure stdlib so you
can do all the discovery work without installing anything.
"""

import argparse
import json
import socket
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55355

# Saturn Work RAM in the RetroAchievements flat address space used by
# READ_CORE_RAM / WRITE_CORE_RAM (Beetle Saturn exposes NO system memory map,
# so READ_CORE_MEMORY is unavailable — "no memory map defined"). Verified on
# RetroArch 1.22.2 + Beetle Saturn: work RAM spans 0x000000..0x1FFFFF, with
# game state usually in the high half. `probe` confirms which respond.
SATURN_BASES = [
    ("LWRAM (Low Work RAM, 1 MB)", 0x000000, 0x100000),
    ("HWRAM (High Work RAM, 1 MB)", 0x100000, 0x100000),
]

# Max bytes per READ command. RetroArch's network reply buffer caps out
# between 2048 and 4096 bytes (measured on 1.22.2: 2048 OK, 4096 times out),
# so 2048 is the largest safe chunk — ~8x fewer round-trips than 256.
READ_CHUNK = 2048


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
class RetroArchLink:
    """Thin UDP client for RetroArch's network command interface."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=0.5):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        # Large receive buffer so a burst of pipelined replies doesn't overflow
        # the kernel queue before we drain it.
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                 4 * 1024 * 1024)
        except OSError:
            pass
        # "RAM"    = READ_CORE_RAM/WRITE_CORE_RAM (RetroAchievements address
        #            space) — the family that actually works on Beetle Saturn.
        # "MEMORY" = READ_CORE_MEMORY/WRITE_CORE_MEMORY (system memory map) —
        #            unavailable on Beetle Saturn, kept for other cores.
        self.family = "RAM"

    # -- low level ---------------------------------------------------------- #
    def _send(self, text):
        self.sock.sendto(text.encode("ascii"), self.addr)

    def _recv(self):
        data, _ = self.sock.recvfrom(8192)
        return data.decode("ascii", "replace").strip()

    def _send_recv(self, text, retries=2):
        last_err = None
        for _ in range(retries + 1):
            self._send(text)
            try:
                return self._recv()
            except socket.timeout as e:
                last_err = e
        raise TimeoutError(f"no reply to {text!r} (is RetroArch running with "
                           f"network_cmd_enable = \"true\"?)") from last_err

    def _pipe_read(self, requests, family=None, batch=128, retries=3,
                   progress=None):
        """Read many (addr, nbytes) requests by pipelining.

        Fires a batch of requests without waiting, then drains replies and
        matches them by echoed address. RetroArch processes a burst per frame,
        so this is ~8x faster than request-wait-request. Lost datagrams (rare
        on localhost) are detected as missing addresses and retried. Returns
        {addr: bytes}; raises if anything stays unanswered after `retries`.
        """
        fam = family or self.family
        out = {}
        remaining = list(requests)
        total = len(requests)
        for _attempt in range(retries + 1):
            if not remaining:
                break
            still_missing = []
            for i in range(0, len(remaining), batch):
                group = remaining[i:i + batch]
                need = {addr for addr, _ in group}
                for addr, n in group:
                    self._send(f"READ_CORE_{fam} {addr:x} {n}")
                while need:
                    try:
                        resp = self._recv()
                    except (socket.timeout, TimeoutError):
                        break  # remaining needs were dropped; retry them
                    toks = resp.split()
                    if len(toks) < 3:
                        continue
                    try:
                        raddr = int(toks[1], 16)
                    except ValueError:
                        continue
                    if raddr not in need:
                        continue  # stale/duplicate datagram
                    need.discard(raddr)
                    if toks[2] == "-1":
                        continue  # core error at this addr; leave out of `out`
                    try:
                        out[raddr] = bytes(int(t, 16) for t in toks[2:])
                    except ValueError:
                        pass
                still_missing.extend((a, n) for a, n in group if a not in out)
                if progress:
                    sys.stderr.write(f"\r  {progress}: {len(out)}/{total}   ")
                    sys.stderr.flush()
            remaining = still_missing
        if progress:
            sys.stderr.write("\n")
        if remaining:
            raise IOError(f"{len(remaining)} read(s) unanswered after "
                          f"{retries} retries (e.g. {remaining[0][0]:#x})")
        return out

    # -- memory ops --------------------------------------------------------- #
    def read(self, addr, n, family=None):
        """Read n bytes at addr. Returns bytes, or raises on a core error."""
        fam = family or self.family
        cmd = f"READ_CORE_{fam} {addr:x} {n}"
        resp = self._send_recv(cmd)
        toks = resp.split()
        # Expected: READ_CORE_xxx <addr> <b0> <b1> ...   ('-1' => error)
        if len(toks) < 3 or toks[2] == "-1":
            raise IOError(f"core refused read at {addr:#x}: {resp!r}")
        try:
            out = bytes(int(t, 16) for t in toks[2:])
        except ValueError:
            raise IOError(f"unparseable read reply: {resp!r}")
        if len(out) != n:
            # Not fatal, but worth knowing: response was truncated/expanded.
            sys.stderr.write(f"[warn] asked for {n} bytes, got {len(out)} "
                             f"at {addr:#x}\n")
        return out

    def read_region(self, base, length, family=None, progress=False):
        """Read a whole region (pipelined READ_CHUNK pieces). Returns bytes."""
        reqs = []
        off = 0
        while off < length:
            n = min(READ_CHUNK, length - off)
            reqs.append((base + off, n))
            off += n
        parts = self._pipe_read(
            reqs, family=family,
            progress="reading region (chunks)" if progress else None)
        buf = bytearray()
        for addr, n in reqs:
            buf += parts[addr]
        return buf

    def read_values(self, offsets, base, width, endian, signed,
                    family=None, progress=False):
        """Read only the given candidate offsets, not the whole region.

        Offsets are packed into <=READ_CHUNK windows (so each is one read) and
        the windows are pipelined. Since RetroArch's cost is per-round-trip,
        packing + pipelining makes a narrowed candidate set very fast.
        Returns {offset: decoded value}.
        """
        if not offsets:
            return {}
        spans = _window_offsets(sorted(offsets), width, READ_CHUNK)
        reqs = [(base + s, e - s) for s, e, _ in spans]
        parts = self._pipe_read(
            reqs, family=family,
            progress="reading candidates" if progress else None)
        vals = {}
        for s, e, glist in spans:
            buf = parts[base + s]
            for o in glist:
                vals[o] = decode_value(buf, o - s, width, endian, signed)
        return vals

    def write(self, addr, data, family=None, confirm=False):
        """Write bytes at addr.

        RetroArch sends NO reply to a write command, so writes are always
        fire-and-forget. `confirm` verifies via a separate read-back rather
        than by waiting for a (non-existent) write acknowledgement.
        """
        fam = family or self.family
        hexbytes = " ".join(f"{b:02x}" for b in data)
        self._send(f"WRITE_CORE_{fam} {addr:x} {hexbytes}")
        if confirm:
            time.sleep(0.03)  # let the core apply it before reading back
            back = self.read(addr, len(data), family=fam)
            return bytes(back) == bytes(data)
        return True

    # -- detection ---------------------------------------------------------- #
    def detect_family(self, probe_addr=0x0):
        """Pick the command family this build/core actually answers.

        Probes at a low address valid in the RetroAchievements space (RAM),
        trying RAM first since that's the family Beetle Saturn supports.
        """
        for fam in ("RAM", "MEMORY"):
            try:
                self.read(probe_addr, 1, family=fam)
                self.family = fam
                return fam
            except (IOError, TimeoutError):
                continue
        return None


# --------------------------------------------------------------------------- #
# Value encoding helpers
# --------------------------------------------------------------------------- #
def decode_value(buf, off, width, endian, signed):
    raw = buf[off:off + width]
    val = int.from_bytes(raw, "big" if endian == "big" else "little",
                         signed=signed)
    return val


def encode_value(val, width, endian, signed):
    return int(val).to_bytes(width, "big" if endian == "big" else "little",
                             signed=signed)


def _window_offsets(sorted_offsets, width, window):
    """Greedily pack sorted offsets into <=`window`-byte spans.

    Each span becomes a single read covering several candidates, minimizing
    round-trips. Returns [(start, end_exclusive, [offsets_in_span])].
    """
    spans = []
    i, n = 0, len(sorted_offsets)
    while i < n:
        start = sorted_offsets[i]
        limit = start + window
        group = []
        while i < n and sorted_offsets[i] + width <= limit:
            group.append(sorted_offsets[i])
            i += 1
        spans.append((start, group[-1] + width, group))
    return spans


def map_axis(frac, lo, hi, invert):
    """Map a 0..1 window fraction to an integer game coordinate."""
    frac = min(1.0, max(0.0, frac))
    if invert:
        frac = 1.0 - frac
    return round(lo + frac * (hi - lo))


# --------------------------------------------------------------------------- #
# Subcommand: probe
# --------------------------------------------------------------------------- #
def cmd_probe(link, args):
    print(f"Connecting to RetroArch at {link.addr[0]}:{link.addr[1]} ...")
    fam = link.detect_family()
    if fam is None:
        print("\nNo response from either command family.")
        print("Checklist:")
        print("  * RetroArch is running with a game loaded (Beetle Saturn).")
        print('  * retroarch.cfg has:  network_cmd_enable = "true"')
        print(f'  * network_cmd_port in retroarch.cfg matches {link.addr[1]} '
              f'(default 55355).')
        print("  * No firewall is blocking localhost UDP.")
        return 1
    print(f"OK — using READ_CORE_{fam} / WRITE_CORE_{fam}.\n")
    print("Probing candidate Saturn RAM bases (first 8 bytes):")
    any_ok = False
    for name, base, _length in SATURN_BASES:
        try:
            data = link.read(base, 8)
            hexs = " ".join(f"{b:02x}" for b in data)
            print(f"  {base:#010x}  {name:<28}  {hexs}")
            any_ok = True
        except (IOError, TimeoutError) as e:
            print(f"  {base:#010x}  {name:<28}  <no data: {e}>")
    if any_ok:
        print("\nA base that returns data is where the game's state lives.")
        print("HWRAM (0x100000) is the usual home for cursor coordinates —")
        print("start your scan there:  scan --base 100000 --length 0x100000")
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: peek
# --------------------------------------------------------------------------- #
def cmd_peek(link, args):
    link.detect_family()
    addr = int(args.addr, 16)
    data = link.read(addr, args.bytes)
    hexs = " ".join(f"{b:02x}" for b in data)
    print(f"{addr:#010x}: {hexs}")
    if args.bytes >= 2:
        be = int.from_bytes(data[:2], "big")
        le = int.from_bytes(data[:2], "little")
        print(f"  as 16-bit  big-endian={be} (0x{be:04x})  "
              f"little-endian={le} (0x{le:04x})")
    if args.bytes >= 1:
        print(f"  as 8-bit   {data[0]} (0x{data[0]:02x})")
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: watch
# --------------------------------------------------------------------------- #
def cmd_watch(link, args):
    link.detect_family()
    addr = int(args.addr, 16)
    nctx = max(args.width, 2)  # always grab 2 bytes so we can see the low byte
    print(f"Watching {addr:#010x}. Primary = {args.width * 8}-bit "
          f"{args.endian}-endian. Ctrl-C to stop.")
    print("Sweep the in-game cursor fully left<->right (or up<->down).")
    lo = hi = None
    second_bytes = set()
    try:
        while True:
            data = link.read(addr, nctx)
            val = decode_value(data, 0, args.width, args.endian, args.signed)
            lo = val if lo is None else min(lo, val)
            hi = val if hi is None else max(hi, val)
            second_bytes.add(data[1])
            raw = " ".join(f"{b:02x}" for b in data[:2])
            u16be = int.from_bytes(data[:2], "big")
            u16le = int.from_bytes(data[:2], "little")
            sys.stdout.write(
                f"\r  bytes={raw}  u8={data[0]:3d}  u16BE={u16be:5d}  "
                f"u16LE={u16le:5d} | min={lo} max={hi}  "
                f"2nd-byte-varies={'yes' if len(second_bytes) > 2 else 'no '}  ")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        span = (hi - lo) if lo is not None else 0
        print(f"\nPrimary ({args.width * 8}-bit {args.endian}-endian): "
              f"min={lo} max={hi} span={span}")
        if len(second_bytes) > 2:
            print(f"2nd byte took {len(second_bytes)} distinct values -> there "
                  "IS sub-unit detail; use --width 2 and calibrate the 16-bit "
                  "value.")
        else:
            print("2nd byte barely moved -> this looks coarse; we should hunt "
                  "for a finer address.")
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: poke  (the write go/no-go gate)
# --------------------------------------------------------------------------- #
def cmd_poke(link, args):
    link.detect_family()
    addr = int(args.addr, 16)
    # Accept "00 80" (one quoted arg) or "00" "80" (separate args) alike.
    tokens = " ".join(args.bytes).split()
    data = bytes(int(b, 16) for b in tokens)
    before = link.read(addr, len(data))
    ok = link.write(addr, data, confirm=True)
    after = link.read(addr, len(data))
    print(f"{addr:#010x}")
    print(f"  before: {' '.join(f'{b:02x}' for b in before)}")
    print(f"  wrote:  {' '.join(f'{b:02x}' for b in data)}")
    print(f"  after:  {' '.join(f'{b:02x}' for b in after)}")
    if ok and bytes(after) == data:
        print("\nWRITE CONFIRMED — the core honors memory writes. ✅")
        print("Next: prove it's the *functional* cursor, not just the drawn")
        print("crosshair. poke the cursor onto a known hotspot and check the")
        print("game actually reacts (highlights it / examine works).")
    else:
        print("\nWrite did NOT stick. ❌  This core/build may not support")
        print("memory writes over the network, or the game immediately")
        print("overwrites this address every frame. If every write fails,")
        print("the memory-write approach can't work and we pivot to the")
        print("light-gun (absolute) device instead.")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: scan  (interactive RAM search)
# --------------------------------------------------------------------------- #
def cmd_scan(link, args):
    link.detect_family()
    base = int(args.base, 16)
    length = int(args.length, 16) if args.length.startswith("0x") \
        else int(args.length)
    width, endian, signed = args.width, args.endian, args.signed
    align = args.align

    print(f"Scanning {base:#010x}..{base+length:#010x} as "
          f"{width*8}-bit {endian}-endian{' signed' if signed else ''}.")
    print("How it works: move the in-game cursor a known direction, then tell")
    print("me how the value should have changed. Repeat until one address is")
    print("left. Then do it again for the other axis.\n")
    print("Commands at each step:")
    print("  i = value increased (e.g. moved cursor right -> X up)")
    print("  d = value decreased")
    print("  s = value stayed the same")
    print("  c = value changed (either direction)")
    print("  =N = value now equals exactly N")
    print("  r = reset (start over with all addresses)")
    print("  l = list surviving addresses")
    print("  q = done\n")

    def full_snapshot(cands):
        buf = link.read_region(base, length, progress=True)
        return {off: decode_value(buf, off, width, endian, signed)
                for off in cands}

    candidates = list(range(0, length - width + 1, align))
    print(f"Taking initial snapshot of {len(candidates)} addresses "
          "(one-time full read)...")
    prev_vals = full_snapshot(candidates)
    print(f"{len(candidates)} candidate addresses. "
          "Subsequent passes re-read only survivors.\n")

    while True:
        try:
            choice = input("move cursor, then enter command > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice:
            continue
        if choice == "q":
            break
        if choice == "r":
            candidates = list(range(0, length - width + 1, align))
            prev_vals = full_snapshot(candidates)
            print(f"reset: {len(candidates)} candidates")
            continue
        if choice == "l":
            _list_candidates(base, prev_vals, candidates)
            continue

        eq_target = None
        if choice.startswith("="):
            try:
                eq_target = int(choice[1:])
            except ValueError:
                print("  bad =N value")
                continue
        elif choice not in ("i", "d", "s", "c"):
            print("  unknown command")
            continue

        # Re-read ONLY the surviving candidates, not the whole region.
        new_vals = link.read_values(candidates, base, width, endian, signed,
                                    progress=(len(candidates) > 4000))
        survivors = []
        for off in candidates:
            pv, nv = prev_vals[off], new_vals[off]
            if eq_target is not None:
                keep = (nv == eq_target)
            elif choice == "i":
                keep = nv > pv
            elif choice == "d":
                keep = nv < pv
            elif choice == "s":
                keep = nv == pv
            else:  # "c"
                keep = nv != pv
            if keep:
                survivors.append(off)
        candidates = survivors
        prev_vals = {off: new_vals[off] for off in survivors}
        print(f"  -> {len(candidates)} candidates remain")
        if 0 < len(candidates) <= 20:
            _list_candidates(base, prev_vals, candidates)

    print("\nSurvivors:")
    _list_candidates(base, prev_vals, candidates)
    if candidates:
        print("\nIf several addresses survive, they're usually adjacent "
              "byte-offset")
        print("artifacts. Pick the one whose values look like real screen")
        print("coordinates (e.g. ~0..319 for X) — neighbors tend to show")
        print("implausible values (huge, or jumping in steps of 256).")
        first = base + candidates[0]
        print(f"\nLikely address: {first:#010x}")
        print("Verify with:  watch  on this address while moving the cursor,")
        print("then record its min and max for the run config.")
    return 0


def _sample_region(link, base, length, count, interval, label):
    samples = []
    for k in range(count):
        samples.append(bytes(link.read_region(base, length)))
        sys.stderr.write(f"\r  {label}: sample {k + 1}/{count}   ")
        sys.stderr.flush()
        if interval:
            time.sleep(interval)
    sys.stderr.write("\n")
    return samples


def _changed_mask(samples, length):
    """Byte mask (bytes) nonzero where any sample differs from the first."""
    ref = int.from_bytes(samples[0], "big")
    bits = 0
    for s in samples[1:]:
        bits |= int.from_bytes(s, "big") ^ ref
    return bits.to_bytes(length, "big")


def _observe(link, candidates, base, width, endian, signed, count, min_period,
             label):
    """Sample only `candidates` `count` times; return (min_map, max_map).

    Enforces a minimum period per sample so the window spans real time even
    when reads are fast — otherwise a narrowing pass would finish before you
    could sweep the cursor.
    """
    first = link.read_values(candidates, base, width, endian, signed)
    mn, mx, prev = dict(first), dict(first), dict(first)
    ups = {o: 0 for o in candidates}
    downs = {o: 0 for o in candidates}
    for k in range(1, count):
        t0 = time.monotonic()
        cur = link.read_values(candidates, base, width, endian, signed)
        for off in candidates:
            v = cur[off]
            p = prev[off]
            if v > p:
                ups[off] += 1
            elif v < p:
                downs[off] += 1
            if v < mn[off]:
                mn[off] = v
            elif v > mx[off]:
                mx[off] = v
            prev[off] = v
        sys.stderr.write(f"\r  {label}: sample {k + 1}/{count}   ")
        sys.stderr.flush()
        dt = min_period - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)
    sys.stderr.write("\n")
    return mn, mx, ups, downs


def _track_survivors_mono(candidates, smn, smx, ups, downs,
                          min_changes=4, mono_frac=0.8):
    """Keep candidates stable at rest that moved (near-)monotonically.

    stable at rest: no variation during the still phase.
    moved monotonically: enough steps, mostly in one direction (a real
    coordinate during a one-way sweep; random-walk noise fails this).
    """
    out = []
    for o in candidates:
        if smx[o] != smn[o]:                 # twitched at rest -> noise
            continue
        changes = ups[o] + downs[o]
        if changes < min_changes:            # didn't track the cursor
            continue
        if max(ups[o], downs[o]) < mono_frac * changes:   # not monotonic
            continue
        out.append(o)
    return out


def _report_tracked(base, spanmap, limit=30):
    ranked = sorted(spanmap.items(), key=lambda kv: kv[1][1] - kv[1][0],
                    reverse=True)
    print(f"  {len(ranked)} candidate(s), ranked by span:")
    shown = ranked if limit is None else ranked[:limit]
    for off, (lo, hi) in shown:
        print(f"    {base + off:#010x}  min={lo:6d} max={hi:6d} "
              f"span={hi - lo}")
    if limit is not None and len(ranked) > limit:
        print(f"    ... and {len(ranked) - limit} more (l = list all)")


def cmd_track(link, args):
    """Find cursor coords by motion: quiet when still, moving when the cursor
    moves. Direction/encoding-agnostic, and covers all of work RAM."""
    link.detect_family()
    base = int(args.base, 16)
    length = int(args.length, 16) if args.length.startswith("0x") \
        else int(args.length)
    width, endian, signed, align = args.width, args.endian, args.signed, \
        args.align

    print(f"Tracking {base:#010x}..{base + length:#010x} as "
          f"{width * 8}-bit {endian}-endian.")
    print("Find X with a LEFT<->RIGHT sweep; find Y with an UP<->DOWN sweep.\n")

    input("1) Hold the cursor perfectly STILL, then press Enter...")
    still = _sample_region(link, base, length, args.still, args.interval,
                           "still ")
    input(f"2) Press Enter, then immediately SWEEP the cursor back and forth "
          f"across the FULL range, nonstop, until sampling finishes "
          f"(~{max(1, int(args.move * 0.35))}s)...")
    move = _sample_region(link, base, length, args.move, args.interval,
                          "moving")

    print("Analyzing...")
    volatile = _changed_mask(still, length)   # changed even when still -> noise
    changed = _changed_mask(move, length)     # changed during the sweep

    candidates = [off for off in range(0, length - width + 1, align)
                  if not any(volatile[off:off + width])
                  and any(changed[off:off + width])]
    spanmap = {}
    for off in candidates:
        vals = [decode_value(m, off, width, endian, signed) for m in move]
        spanmap[off] = (min(vals), max(vals))

    print(f"\nPass 1: {len(candidates)} address(es) quiet at rest but moving "
          "with the cursor.")
    _report_tracked(base, spanmap)
    if not candidates:
        print("\nNothing matched. Hold still in step 1, then sweep the FULL "
              "range nonstop in step 2. If X is truly not a stored coordinate, "
              "that's the cue to switch to the light-gun approach.")
        return 0

    # ---- Iterative narrowing: intersect with further still/sweep passes ----
    while len(candidates) > 1:
        choice = input(f"\n{len(candidates)} candidates. [Enter] = narrow with "
                       "another sweep, l = list all, q = stop > ").strip().lower()
        if choice == "q":
            break
        if choice == "l":
            _report_tracked(base, spanmap, limit=None)
            continue
        input("   Hold the cursor STILL, then press Enter...")
        smn, smx, _su, _sd = _observe(link, candidates, base, width, endian,
                                      signed, args.still, 0.15, "still ")
        input("   Press Enter, then sweep SLOWLY in ONE direction "
              "(left->right for X, up->down for Y) across the full range...")
        mmn, mmx, ups, downs = _observe(link, candidates, base, width, endian,
                                        signed, args.move, 0.15, "moving")
        survivors = _track_survivors_mono(candidates, smn, smx, ups, downs)
        if not survivors:
            print("   That pass eliminated everything — keeping the previous "
                  "set (hold still, then sweep slowly in one direction).")
            continue
        candidates = survivors
        spanmap = {off: (mmn[off], mmx[off]) for off in survivors}
        print(f"   -> {len(candidates)} remain")
        _report_tracked(base, spanmap)

    print("\nVerify the top address with:  watch <addr>  (it should move "
          "smoothly across its whole span as you sweep).")
    return 0


def _list_candidates(base, vals, candidates):
    if not candidates:
        print("  (none)")
        return
    shown = candidates[:40]
    for off in shown:
        print(f"    {base + off:#010x} = {vals[off]}")
    if len(candidates) > len(shown):
        print(f"    ... and {len(candidates) - len(shown)} more")


# --------------------------------------------------------------------------- #
# Subcommand: run  (the live bridge — macOS only)
# --------------------------------------------------------------------------- #
def _load_run_config(args):
    cfg = {
        "x_addr": None, "y_addr": None,
        "width": 2, "endian": "big", "signed": False,
        "x_min": 0, "x_max": 319, "y_min": 0, "y_max": 223,
        "x_invert": False, "y_invert": False,
        "rate_hz": 60, "window_owner": "RetroArch",
        # Fractions of the window to crop off, to skip the macOS title bar
        # and any letterbox/pillarbox bars so the mapping hits the game area.
        "inset_left": 0.0, "inset_right": 0.0,
        "inset_top": 0.0, "inset_bottom": 0.0,
    }
    if args.config:
        with open(args.config) as f:
            cfg.update(json.load(f))
    # CLI flags override the file.
    for k in ("x_addr", "y_addr", "width", "endian", "rate_hz",
              "window_owner"):
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    if cfg["x_addr"] is None or cfg["y_addr"] is None:
        sys.exit("run needs x_addr and y_addr (via --config or "
                 "--x-addr/--y-addr).")
    # Normalize to lists of ints so mirror copies can all be written.
    cfg["x_addr"] = _as_addr_list(cfg["x_addr"])
    cfg["y_addr"] = _as_addr_list(cfg["y_addr"])
    return cfg


def _as_addr_list(v):
    """Accept a hex string or a list of hex strings/ints -> list of ints."""
    items = v if isinstance(v, list) else [v]
    return [int(a, 16) if isinstance(a, str) else int(a) for a in items]


def _make_mac_readers(owner):
    """Return (get_cursor, get_window_rect) backed by Quartz, or exit."""
    try:
        import Quartz
    except ImportError:
        sys.exit("`run` needs pyobjc on macOS:\n"
                 "    pip install pyobjc-framework-Quartz")

    def get_cursor():
        ev = Quartz.CGEventCreate(None)
        pt = Quartz.CGEventGetLocation(ev)  # global, top-left origin, points
        return float(pt.x), float(pt.y)

    def get_window_rect():
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
        best = None
        best_area = 0
        for w in wins:
            if w.get("kCGWindowOwnerName", "") != owner:
                continue
            b = w.get("kCGWindowBounds")
            if not b:
                continue
            area = b["Width"] * b["Height"]
            if area > best_area:
                best_area = area
                best = (b["X"], b["Y"], b["Width"], b["Height"])
        return best  # (x, y, w, h) in points, or None

    return get_cursor, get_window_rect


def _capture_point(get_cursor, prompt, secs):
    print(prompt)
    for i in range(secs, 0, -1):
        sys.stdout.write(f"\r   capturing in {i}...  (hold the cursor there)  ")
        sys.stdout.flush()
        time.sleep(1)
    pt = get_cursor()
    print(f"\r   captured at {pt[0]:.0f}, {pt[1]:.0f}                          ")
    return pt


def cmd_calibrate(link, args):
    """Capture window->game-image insets by pointing at two image corners."""
    owner = args.window_owner or "RetroArch"
    get_cursor, get_window_rect = _make_mac_readers(owner)
    rect = get_window_rect()
    if rect is None:
        sys.exit(f"No on-screen '{owner}' window found — is it visible?")
    wx, wy, ww, wh = rect
    print(f"{owner} window: x={wx:.0f} y={wy:.0f} w={ww:.0f} h={wh:.0f}")
    print("Point at the corners of the GAME IMAGE itself (not the window "
          "frame / black bars).\n")
    tl = _capture_point(get_cursor,
                        "1) Move the cursor to the image's TOP-LEFT corner.",
                        args.secs)
    br = _capture_point(get_cursor,
                        "2) Move the cursor to the image's BOTTOM-RIGHT corner.",
                        args.secs)

    insets = {
        "inset_left": round(max(0.0, (tl[0] - wx) / ww), 4),
        "inset_top": round(max(0.0, (tl[1] - wy) / wh), 4),
        "inset_right": round(max(0.0, (wx + ww - br[0]) / ww), 4),
        "inset_bottom": round(max(0.0, (wy + wh - br[1]) / wh), 4),
    }
    print("\nComputed insets:")
    for k, v in insets.items():
        print(f"  {k}: {v}")

    if args.config:
        with open(args.config) as f:
            cfg = json.load(f)
        cfg.update(insets)
        with open(args.config, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"\nWrote insets into {args.config}. Now run:\n"
              f"  python3 policenauts_cursor.py run --config {args.config}")
    else:
        print("\nPaste these into cursor.json, or re-run with "
              "--config cursor.json to write them automatically.")
    return 0


def cmd_run(link, args):
    cfg = _load_run_config(args)
    link.detect_family()
    get_cursor, get_window_rect = _make_mac_readers(cfg["window_owner"])

    width, endian, signed = cfg["width"], cfg["endian"], cfg["signed"]
    period = 1.0 / cfg["rate_hz"]
    warned_no_window = False

    xs = " ".join(f"{a:#x}" for a in cfg["x_addr"])
    ys = " ".join(f"{a:#x}" for a in cfg["y_addr"])
    print(f"Bridging host cursor -> game cursor at {cfg['rate_hz']} Hz "
          f"({cfg['endian']}-endian).")
    print(f"  X -> {xs} range [{cfg['x_min']},{cfg['x_max']}]"
          f"{' inverted' if cfg['x_invert'] else ''}")
    print(f"  Y -> {ys} range [{cfg['y_min']},{cfg['y_max']}]"
          f"{' inverted' if cfg['y_invert'] else ''}")
    if args.debug:
        print("  (debug: printing fractions + written values)")
    print("Ctrl-C to stop.\n")

    next_t = time.monotonic()
    try:
        while True:
            rect = get_window_rect()
            if rect is None:
                if not warned_no_window:
                    sys.stderr.write(
                        f"[warn] no on-screen '{cfg['window_owner']}' window "
                        f"found — is RetroArch focused/visible?\n")
                    warned_no_window = True
                next_t += period
                _sleep_until(next_t)
                continue
            warned_no_window = False
            wx, wy, ww, wh = rect

            # Crop to the actual game viewport (skip title bar / letterbox).
            ix0 = wx + ww * cfg["inset_left"]
            iy0 = wy + wh * cfg["inset_top"]
            iw = ww * (1.0 - cfg["inset_left"] - cfg["inset_right"])
            ih = wh * (1.0 - cfg["inset_top"] - cfg["inset_bottom"])

            cx, cy = get_cursor()
            fx = (cx - ix0) / iw if iw > 0 else 0.0
            fy = (cy - iy0) / ih if ih > 0 else 0.0

            gx = map_axis(fx, cfg["x_min"], cfg["x_max"], cfg["x_invert"])
            gy = map_axis(fy, cfg["y_min"], cfg["y_max"], cfg["y_invert"])

            xbytes = encode_value(gx, width, endian, signed)
            ybytes = encode_value(gy, width, endian, signed)
            for a in cfg["x_addr"]:
                link.write(a, xbytes)
            for a in cfg["y_addr"]:
                link.write(a, ybytes)

            if args.debug:
                sys.stdout.write(
                    f"\r  fx={fx:.3f} fy={fy:.3f} -> X={gx:4d} Y={gy:4d}   ")
                sys.stdout.flush()

            next_t += period
            _sleep_until(next_t)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def _sleep_until(t):
    dt = t - time.monotonic()
    if dt > 0:
        time.sleep(dt)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Drive a Saturn game's cursor from the host mouse via "
                    "RetroArch network commands.")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=0.5,
                   help="UDP receive timeout, seconds")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="test connection + show RAM bases")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("peek", help="read N bytes once")
    sp.add_argument("addr", help="hex address, e.g. 06012abc")
    sp.add_argument("bytes", nargs="?", type=int, default=2)
    sp.set_defaults(func=cmd_peek)

    sp = sub.add_parser("watch", help="continuously print a value")
    sp.add_argument("addr", help="hex address")
    sp.add_argument("--width", type=int, choices=(1, 2), default=2)
    sp.add_argument("--endian", choices=("big", "little"), default="big")
    sp.add_argument("--signed", action="store_true")
    sp.add_argument("--interval", type=float, default=0.1)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("poke", help="write bytes and verify (write gate)")
    sp.add_argument("addr", help="hex address")
    sp.add_argument("bytes", nargs="+", help="hex byte(s), e.g. 00 80")
    sp.set_defaults(func=cmd_poke)

    sp = sub.add_parser("track", help="robustly find X/Y by cursor motion")
    sp.add_argument("--base", default="0", help="hex region start (0 = all WRAM)")
    sp.add_argument("--length", default="0x200000",
                    help="region length (default 0x200000 = full 2MB WRAM)")
    sp.add_argument("--width", type=int, choices=(1, 2), default=2)
    sp.add_argument("--endian", choices=("big", "little"), default="big")
    sp.add_argument("--signed", action="store_true")
    sp.add_argument("--align", type=int, default=2,
                    help="address stride; 2 (default) since SH-2 16-bit values "
                         "are even-aligned. Use 1 only for an 8-bit search.")
    sp.add_argument("--still", type=int, default=24,
                    help="samples while still (keep equal to --move so the "
                         "only difference between phases is cursor motion)")
    sp.add_argument("--move", type=int, default=24,
                    help="samples while sweeping the cursor")
    sp.add_argument("--interval", type=float, default=0.0)
    sp.set_defaults(func=cmd_track)

    sp = sub.add_parser("scan", help="interactive RAM search for X/Y")
    sp.add_argument("--base", default="100000",
                    help="hex region start (RA space: HWRAM=100000, LWRAM=0)")
    sp.add_argument("--length", default="0x100000",
                    help="region length (hex with 0x, or decimal)")
    sp.add_argument("--width", type=int, choices=(1, 2), default=2)
    sp.add_argument("--endian", choices=("big", "little"), default="big")
    sp.add_argument("--signed", action="store_true")
    sp.add_argument("--align", type=int, default=1,
                    help="candidate address stride; use 2 to halve noise if "
                         "the value turns out to live at an even address")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("calibrate",
                        help="capture window->image insets by pointing at corners")
    sp.add_argument("--config", help="JSON config to write the insets into")
    sp.add_argument("--window-owner", dest="window_owner", default=None)
    sp.add_argument("--secs", type=int, default=4,
                    help="countdown seconds before each capture")
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("run", help="live bridge (macOS)")
    sp.add_argument("--config", help="path to JSON config")
    sp.add_argument("--x-addr", dest="x_addr", help="hex address of cursor X")
    sp.add_argument("--y-addr", dest="y_addr", help="hex address of cursor Y")
    sp.add_argument("--width", type=int, choices=(1, 2), default=None)
    sp.add_argument("--endian", choices=("big", "little"), default=None)
    sp.add_argument("--rate-hz", dest="rate_hz", type=int, default=None)
    sp.add_argument("--window-owner", dest="window_owner", default=None)
    sp.add_argument("--debug", action="store_true",
                    help="print fractions + written values")
    sp.set_defaults(func=cmd_run)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    link = RetroArchLink(args.host, args.port, args.timeout)
    try:
        return args.func(link, args)
    except (TimeoutError, IOError) as e:
        sys.stderr.write(f"error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
