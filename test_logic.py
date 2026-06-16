"""Pure-logic tests — no RetroArch, no macOS needed. Run: python3 test_logic.py"""
from collections import deque

import policenauts_cursor as pc


def test_encode_decode_roundtrip():
    cases = [
        (0, 1, "big", False), (255, 1, "big", False),
        (0, 2, "big", False), (319, 2, "big", False), (65535, 2, "big", False),
        (319, 2, "little", False),
        (-1, 2, "little", True), (-128, 1, "big", True),
    ]
    for val, width, endian, signed in cases:
        enc = pc.encode_value(val, width, endian, signed)
        assert len(enc) == width, (val, enc)
        back = pc.decode_value(enc, 0, width, endian, signed)
        assert back == val, (val, width, endian, signed, back)
    # Saturn is big-endian: 319 = 0x013F -> high byte first.
    assert pc.encode_value(319, 2, "big", False) == b"\x01\x3f"
    assert pc.encode_value(319, 2, "little", False) == b"\x3f\x01"


def test_map_axis():
    assert pc.map_axis(0.0, 0, 319, False) == 0
    assert pc.map_axis(1.0, 0, 319, False) == 319
    assert pc.map_axis(0.5, 0, 319, False) == 160      # rounds 159.5 -> 160
    # clamps out-of-window cursor positions
    assert pc.map_axis(-0.3, 0, 319, False) == 0
    assert pc.map_axis(1.7, 0, 319, False) == 319
    # inversion flips the axis
    assert pc.map_axis(0.0, 0, 319, True) == 319
    assert pc.map_axis(1.0, 0, 319, True) == 0
    # non-zero minimum (calibrated extremes need not start at 0)
    assert pc.map_axis(0.0, 40, 600, False) == 40
    assert pc.map_axis(1.0, 40, 600, False) == 600


class FakeRA:
    """Stands in for RetroArch: serves bytes from an in-memory image.

    Faithful to the real protocol in the way that matters: READ commands get a
    reply; WRITE commands get NO reply (fire-and-forget). The earlier version
    replied to writes, which masked a real bug in write(confirm=True).
    """
    def __init__(self, base, image, drop=0):
        self.base, self.image = base, image
        self.read_calls = 0
        self.bytes_read = 0
        self.drop = drop          # silently drop this many read replies (loss)
        self.queue = deque()      # pending reply datagrams

    def _apply(self, text):
        toks = text.split()
        cmd, addr, rest = toks[0], int(toks[1], 16), toks[2:]
        off = addr - self.base
        if cmd.startswith("READ_CORE_"):
            n = int(rest[0])
            self.read_calls += 1
            self.bytes_read += n
            data = self.image[off:off + n]
            return f"{cmd} {addr:x} {' '.join(f'{b:02x}' for b in data)}"
        if cmd.startswith("WRITE_CORE_"):
            for i, hb in enumerate(rest):
                self.image[off + i] = int(hb, 16)
            return None  # writes produce no datagram back
        return f"{cmd} {addr:x} -1"

    def send(self, text):
        reply = self._apply(text)
        if reply is None:
            return                       # write: nothing to receive
        if self.drop > 0 and text.startswith("READ_CORE_"):
            self.drop -= 1               # simulate a lost reply
            return
        self.queue.append(reply)

    def recv(self):
        if self.queue:
            return self.queue.popleft()
        raise TimeoutError("no datagram")

    def send_recv(self, text, retries=2):
        self.send(text)
        return self.recv()               # raises if it was a write/dropped


def make_link(fake):
    link = pc.RetroArchLink()
    link._send_recv = fake.send_recv
    link._send = fake.send
    link._recv = fake.recv
    return link


def test_read_parses_reply():
    base = 0x06000000
    img = bytearray(b"\x01\x3f\xde\xad\xbe\xef" + bytes(250))
    link = make_link(FakeRA(base, img))
    assert link.read(base, 2) == b"\x01\x3f"
    assert link.read(base + 2, 4) == b"\xde\xad\xbe\xef"


def test_read_region_chunks():
    base = 0x100000
    n = 5000  # > READ_CHUNK (2048): forces multiple chunks
    img = bytearray((i & 0xFF) for i in range(n))
    fake = FakeRA(base, img)
    link = make_link(fake)
    out = link.read_region(base, n)
    assert out == img, "chunked read must reassemble the full region exactly"
    assert fake.read_calls == 3, fake.read_calls  # 2048 + 2048 + 904


def test_window_offsets_packs():
    spans = pc._window_offsets([0, 2, 4, 5000, 5002], 2, 2048)
    assert [g for _, _, g in spans] == [[0, 2, 4], [5000, 5002]], spans
    # span end is last offset + width
    assert spans[0][:2] == (0, 6) and spans[1][:2] == (5000, 5004)


def test_read_values_only_covers_candidates():
    base, length = 0x100000, 0x10000
    img = bytearray((i * 7) & 0xFF for i in range(length))
    fake = FakeRA(base, img)
    link = make_link(fake)
    # Sparse candidates: two near each other (one window) + two far apart.
    cands = [0x10, 0x12, 0x2000, 0x8000]
    vals = link.read_values(cands, base, 2, "big", False)
    for o in cands:
        assert vals[o] == pc.decode_value(img, o, 2, "big", False), o
    # 0x10/0x12 pack into one window; 0x2000 and 0x8000 each their own -> 3.
    assert fake.read_calls == 3, fake.read_calls
    assert fake.bytes_read < length // 8, fake.bytes_read


def test_as_addr_list():
    assert pc._as_addr_list("14b5f4") == [0x14b5f4]
    assert pc._as_addr_list(["14b5f4", "151518"]) == [0x14b5f4, 0x151518]
    assert pc._as_addr_list([0x10, "20"]) == [0x10, 0x20]


def test_track_survivors_mono():
    cands = [1, 2, 3, 4, 5]
    smn = {1: 5, 2: 5, 3: 5, 4: 5, 5: 5}
    smx = {1: 5, 2: 9, 3: 5, 4: 5, 5: 5}      # 2 twitched at rest -> noise
    # 1: 9/10 steps up (monotonic). 3: 1 up/1 down, too few + not monotonic.
    # 4: 5 steps all up (monotonic). 5: never moved.
    ups = {1: 9, 2: 0, 3: 1, 4: 5, 5: 0}
    downs = {1: 1, 2: 0, 3: 1, 4: 0, 5: 0}
    out = pc._track_survivors_mono(cands, smn, smx, ups, downs,
                                   min_changes=4, mono_frac=0.8)
    assert out == [1, 4], out


def test_pipe_read_retries_dropped_reply():
    base, length = 0x100000, 0x1000
    img = bytearray((i & 0xFF) for i in range(length))
    fake = FakeRA(base, img, drop=2)        # lose the first two replies
    link = make_link(fake)
    reqs = [(base + i * 4, 4) for i in range(10)]
    out = link._pipe_read(reqs, retries=3)
    assert len(out) == 10, len(out)
    for addr, n in reqs:
        assert out[addr] == bytes(img[addr - base:addr - base + n])


def test_write_roundtrip_confirm():
    base = 0x06000000
    img = bytearray(16)
    link = make_link(FakeRA(base, img))
    ok = link.write(base + 4, pc.encode_value(319, 2, "big", False),
                    confirm=True)
    assert ok
    assert link.read(base + 4, 2) == b"\x01\x3f"


def test_scan_filter_finds_moving_address():
    """Simulate the scan filter: only the 'cursor' address should survive."""
    base, length, width = 0x06000000, 64, 2
    img_a = bytearray(length)
    img_b = bytearray(length)
    # Address +10 holds the cursor X; it increases. Everything else is static.
    pc_x = 10
    for off in range(0, length, 2):
        v = 100 if off != pc_x else 150   # snapshot A
        img_a[off:off + 2] = pc.encode_value(v, width, "big", False)
        v2 = 100 if off != pc_x else 170  # snapshot B (cursor moved right)
        img_b[off:off + 2] = pc.encode_value(v2, width, "big", False)

    def passes(stride):
        survivors = []
        for off in range(0, length - width + 1, stride):
            a = pc.decode_value(img_a, off, width, "big", False)
            b = pc.decode_value(img_b, off, width, "big", False)
            if b > a:                     # "increased" filter
                survivors.append(off)
        return survivors

    # Byte-granular scan: the true address always survives; an adjacent
    # misaligned offset may also survive (a known artifact, not a bug).
    byte_survivors = passes(1)
    assert pc_x in byte_survivors, byte_survivors
    assert all(s in (pc_x, pc_x - 1, pc_x + 1) for s in byte_survivors)
    assert 0 not in byte_survivors  # static RAM is correctly filtered out

    # Aligned scan (the --align 2 knob) removes the misaligned neighbor when
    # the value lives at an even offset.
    assert passes(2) == [pc_x]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    run()
