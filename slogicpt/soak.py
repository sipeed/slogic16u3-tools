"""Soak-test verdicts, ported from the SLogic repo's
build/bench/slogic_selftest.py --soak.

The soak is one long CONTINUOUS capture per pattern -- minutes, not the
selftest's 250 ms rounds -- so every sample has to be verified as it arrives;
the whole stream never fits in memory (32ch@200M is 48 GB per minute).
`sigrok.SigrokCli.capture_verify` reads the pipe in chunks and feeds them to a
StreamVerifier here; nothing is written to disk.

  Emulation  the analyzer's on-board pattern.  Every sample is compared with
             [soak] emulation (a counter advancing one per sample); a wrong
             sample, a lost run or a repeated run is reported with its sample
             index.  Without a rule for this channel count, the first samples
             are checked per channel's period instead.
  Normal     floating inputs.  Only the stream itself is judged: exact sample
             count, the core's verdict end=ok, enough USB transfers in flight,
             and the measured rate >= min_rate_pct % of channels*rate/8.

The rate and the end verdict come from libsigrok's `slogic-report v1` line
(SLogic >= 2.3.0, log level info); a capture cut short by Stop has no report,
and its own delivered rate is used instead.
"""
from __future__ import annotations

import re

PATTERNS = ("Emulation", "Normal")

# slogic-report v1 model=SLogic32U3 ch=32 rate_mhz=200 end=ok samples=N/N
#   bytes=... mbps=799.4 exp_mbps=800.0 ... xfer=SIZExN ...
REPORT_RE = re.compile(r"slogic-report v1 (.*)$", re.M)
MIN_IN_FLIGHT = 4


def parse_report(text: str) -> dict | None:
    """The last slogic-report line as a dict, or None."""
    found = REPORT_RE.findall(text)
    if not found:
        return None
    return dict(tok.split("=", 1) for tok in found[-1].split() if "=" in tok)


def ring_problem(kv: dict) -> str:
    """'' or why the USB transfer ring was too small.  With fewer than
    MIN_IN_FLIGHT transfers in flight the analyzer's FIFO can overflow, and
    the firmware then drops data without telling the host: end=ok, exact
    count, broken data."""
    try:
        size, n = (int(x) for x in kv.get("xfer", "").split("x"))
        raw = int(kv.get("bytes", 0))
    except (AttributeError, ValueError):
        return ""                                   # no ring figure
    need = min(MIN_IN_FLIGHT, max(1, -(-raw // size))) if size else MIN_IN_FLIGHT
    return (f"only {n} USB transfer(s) of {size} B in flight (need {need})"
            if n < need else "")


# ------------------------------------------------------------ Emulation

_BIT_PLANES = [bytes(((b >> k) & 1) for b in range(256)) for k in range(8)]
_INVERT = bytes([1, 0]) + bytes(254)


def check_periods(out: bytes, nch: int, u: int) -> tuple[bool, str]:
    """Every channel is a square wave with a fixed half-period h: require
    x[i+h] == !x[i] for every sample."""
    halves, bad = [], []
    for ch in range(nch):
        col = out[ch // 8::u] if u > 1 else out
        plane = col.translate(_BIT_PLANES[ch % 8])
        first = plane[0:1]
        t1 = plane.find(b"\x01" if first == b"\x00" else b"\x00")
        t2 = plane.find(first, t1) if t1 >= 0 else -1
        if t1 < 0 or t2 < 0:
            bad.append(f"CH{ch}: fewer than two edges")
            continue
        h = t2 - t1
        if 4 * h > len(plane):
            halves.append(f"{h}(skipped)")
            continue
        a, b = plane[h:], plane[:-h].translate(_INVERT)
        if a != b:
            lo, hi = 0, len(a)              # first differing sample, by bisection
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if a[:mid] == b[:mid] else (lo, mid)
            bad.append(f"CH{ch}: half-period {h} broken at sample {lo + h}")
            continue
        halves.append(str(h))
    if bad:
        return False, "; ".join(bad[:4]) + (f" (+{len(bad) - 4} more)" if len(bad) > 4 else "")
    return True, f"every channel's period exact (half-periods {', '.join(halves[:4])} …)"


_PERIODS: dict = {}


def _period(rules: tuple, bits: int, u: int) -> tuple[int, bytes]:
    """One full period of the expected sample stream, from counter 0."""
    key = (rules, bits, u)
    if key not in _PERIODS:
        top = max([b for b, _ in rules if b is not None] or [0])
        period = 1 << min(top + 1, bits)
        words = []
        for c in range(period):
            w = 0
            for ch, (bit, inv) in enumerate(rules):
                v = inv if bit is None else ((c >> bit) & 1) ^ inv
                w |= v << ch
            words.append(w.to_bytes(u, "little"))
        _PERIODS[key] = (period, b"".join(words))
    return _PERIODS[key]


def check_spec(out: bytes, nch: int, u: int, rules: tuple, bits: int,
               start: int | None) -> tuple[bool, str]:
    """Compare an in-memory prefix with the Emulation rules."""
    period, base = _period(rules, bits, u)
    n = len(out) // u
    if start is not None:
        k = start % period
        base = base[k * u:] + base[:k * u]
    elif n:                                     # free-running: phase from sample 0
        first = out[:u]
        k = next((c for c in range(period) if base[c * u:(c + 1) * u] == first), None)
        if k is None:
            return False, (f"sample 0 (0x{int.from_bytes(first, 'little'):0{2 * u}x}) "
                           "is not in the pattern")
        base = base[k * u:] + base[:k * u]
    ring = base * (n * u // len(base) + 2)
    if out != ring[:len(out)]:
        i = next(k for k in range(0, len(out), u) if out[k:k + u] != ring[k:k + u])
        got = int.from_bytes(out[i:i + u], "little")
        want = int.from_bytes(ring[i:i + u], "little")
        diff = [f"CH{ch}" for ch in range(nch) if (got ^ want) >> ch & 1]
        return False, (f"sample {i // u}: {', '.join(diff[:8])}"
                       f"{' …' if len(diff) > 8 else ''} wrong")
    return True, f"all {n} samples match"


class StreamVerifier:
    """Checks a continuous Emulation capture chunk by chunk.

    The expected stream is periodic (2^bits samples), so the comparison is a
    plain bytes comparison of each chunk against a tiled copy of one period,
    at the running phase -- full speed, no per-sample Python.  On the first
    wrong sample it reports which sample, and whether the stream slipped
    (samples lost or repeated) by matching the following samples against the
    period at shifted phases."""

    REACH = 64          # largest slip searched for, in samples
    RUN = 16            # samples needed after the error to recognize a slip

    def __init__(self, nch: int, rules: tuple, bits: int, start: int | None,
                 chunk_max: int):
        self.u = (nch + 7) // 8
        self.nch = nch
        period, base = _period(rules, bits, self.u)
        if start is not None:
            k = start % period
            base = base[k * self.u:] + base[:k * self.u]
        self.plen = len(base)
        assert self.plen, "empty emulation pattern"
        self.ring = base * (chunk_max // len(base) + 2)
        self.off = 0              # ring offset the next sample should match
        self.samples = 0          # samples compared so far
        self.carry = b""          # bytes of an incomplete sample
        self._pending: tuple[int, int, bytearray] | None = None
        self.problem = ""
        self.need = (self.REACH + self.RUN) * self.u

    def feed(self, chunk: bytes) -> str:
        """Verify one chunk; returns the problem text once known, else ''."""
        data = self.carry + chunk
        usable = len(data) - len(data) % self.u
        self.carry = data[usable:]
        if not usable:
            return ""
        if self._pending is not None:
            buf = self._pending[2]
            buf += data[:usable]
            if len(buf) >= self.need and not self.problem:
                self.problem = self._describe()
            return self.problem
        want = self.ring[self.off:self.off + usable]
        if data[:usable] == want:
            self.off = (self.off + usable) % self.plen
            self.samples += usable // self.u
            return ""
        i = next(k for k in range(0, usable, self.u) if data[k:k + self.u] != want[k:k + self.u])
        self._pending = (self.samples + i // self.u, (self.off + i) % self.plen,
                         bytearray(data[i:i + self.need]))
        if len(self._pending[2]) >= self.need:
            self.problem = self._describe()
        return self.problem

    def finish(self) -> str:
        """The verdict so far (called once the capture has ended)."""
        if not self.problem and self._pending is not None:
            s, off, buf = self._pending
            self.problem = (f"sample {s}: wrong"
                            if len(buf) < self.u else
                            f"sample {s}: {self._describe()} (at the end of the capture)")
        return self.problem

    def _describe(self) -> str:
        s, off, buf = self._pending
        run = self.RUN * self.u
        got = bytes(buf[:run])
        if len(got) >= run:
            for d in sorted(range(-self.REACH, self.REACH + 1), key=abs):
                if not d:
                    continue
                k = (off + d * self.u) % self.plen
                if self.ring[k:k + run] == got:
                    return (f"sample {s}: {abs(d)} sample(s) "
                            f"{'missing' if d > 0 else 'repeated'} ({abs(d) * self.u} bytes)")
        got_w = int.from_bytes(buf[:self.u], "little") if len(buf) >= self.u else 0
        want_w = int.from_bytes(self.ring[off:off + self.u], "little")
        diff = [f"CH{ch}" for ch in range(self.nch) if (got_w ^ want_w) >> ch & 1]
        return (f"sample {s}: {', '.join(diff[:8])}{' …' if len(diff) > 8 else ''} "
                f"wrong (got 0x{got_w:0{2 * self.u}x}, want 0x{want_w:0{2 * self.u}x})")


class HeadVerifier:
    """Keeps the first samples of a capture for the period check, used when a
    product has no emulation rules for this channel count."""

    SAMPLES = 1 << 20               # enough for several periods of the slowest channel

    def __init__(self, unitsize: int):
        self.data = b""
        self.limit = self.SAMPLES * unitsize

    def feed(self, chunk: bytes) -> str:
        if len(self.data) < self.limit:
            self.data += chunk[:self.limit - len(self.data)]
        return ""

    def finish(self) -> str:
        return ""


# ------------------------------------------------------------- verdicts

def judge_capture(pattern: str, *, rc: int | None, killed_by: str | None,
                  n_bytes: int, elapsed_s: float, log: str, verify_problem: str,
                  n_expected: int, nch: int, rate_hz: int, plan) -> tuple[str, float]:
    """('' if the capture passed else why not, measured MB/s).  `killed_by` is
    None (ran to its end), "cancel" (operator Stop) or "timeout"."""
    u = (nch + 7) // 8
    kv = parse_report(log)
    stopped = killed_by == "cancel"
    if killed_by == "timeout":
        return f"capture timed out after {elapsed_s:.0f} s", 0.0
    if rc not in (0, None):
        tail = [ln for ln in log.strip().splitlines() if ln.strip()]
        why = (f"end={kv.get('end')}" if kv and kv.get("end")
               else (tail[-1][:100] if tail else ""))
        return f"rc={rc} {why}".strip(), 0.0
    if not stopped and not kv:
        return "no slogic-report line (sigrok-cli older than SLogic 2.3.0?)", 0.0
    if not stopped and n_bytes != n_expected * u:
        why = f"end={kv['end']}" if kv and kv.get("end") else ""
        return (f"got {n_bytes} B, want {n_expected * u} B {why}".strip(), 0.0)
    if kv:
        if not stopped and kv.get("end") != "ok":
            return f"end={kv.get('end')}", 0.0
        ring = ring_problem(kv)
        if ring:
            return ring, 0.0
        try:
            mbps = float(kv.get("mbps", "nan"))
        except ValueError:
            mbps = float("nan")
        if mbps != mbps and stopped:            # report says nan: use our own figure
            mbps = n_bytes / 1e6 / elapsed_s if elapsed_s else 0.0
    else:                       # stopped: no report, the delivered rate is ours
        mbps = n_bytes / 1e6 / elapsed_s if elapsed_s else 0.0
    exp = rate_hz * nch / 8 / 1e6
    if mbps != mbps:
        if pattern == "Normal":
            return "no rate in the report", 0.0
    elif mbps < exp * plan.min_rate_pct / 100:
        return (f"rate {mbps:.1f} MB/s < {plan.min_rate_pct}% of {exp:g} MB/s"), mbps
    if pattern == "Emulation" and verify_problem:
        return verify_problem, mbps
    return "", mbps


if __name__ == "__main__":
    # offline self-test: synthesize streams from the 32U3 rules, feed them in
    # awkward chunks, and break them
    from .profiles import load_profiles
    p32 = next(p for p in load_profiles()[0] if p.id == "slogic32u3")
    plan = p32.soak
    nch, u = 32, 4
    n = 2_000_000
    period, base = _period(plan.emulation, plan.counter_bits, u)
    good = (base * (n // period + 1))[:n * u]
    log = ("sr: slogic-report v1 model=SLogic32U3 ch=32 rate_mhz=200 end=ok "
           f"samples={n}/{n} bytes={n * u} mbps=799.4 exp_mbps=800.0 xfer=4194304x8\n")
    gate = dict(n_expected=n, nch=nch, rate_hz=200_000_000, plan=plan)

    def verify(data, chunk=1 << 20):
        v = StreamVerifier(nch, plan.emulation, plan.counter_bits, plan.counter_start, chunk)
        for k in range(0, len(data), chunk):
            p = v.feed(data[k:k + chunk])
            if p:
                return p
        return v.finish()

    assert verify(good) == ""
    assert verify(good, chunk=4099) == ""               # chunks off the sample grid
    bad = bytearray(good)
    bad[1000 * u] ^= 0x04                               # CH2 flipped
    assert "sample 1000" in verify(bytes(bad)) and "CH2" in verify(bytes(bad))
    slipp = good[:5000 * u] + good[5003 * u:]           # 3 samples lost
    why = verify(slipp)
    assert "sample 5000" in why and "3 sample(s) missing" in why, why
    rep = good[:5000 * u] + good[4997 * u:]             # 3 samples repeated
    why = verify(rep)
    assert "sample 5000" in why and "3 sample(s) repeated" in why, why
    broken_at_end = bytearray(good)
    broken_at_end[-8] ^= 0x80
    assert "sample" in verify(bytes(broken_at_end))

    assert judge_capture("Emulation", rc=0, killed_by=None, n_bytes=n * u,
                         elapsed_s=1.0, log=log, verify_problem="",
                         **gate)[0] == ""
    assert "rate 600.0" in judge_capture(
        "Normal", rc=0, killed_by=None, n_bytes=n * u, elapsed_s=1.0,
        log=log.replace("mbps=799.4", "mbps=600.0"), verify_problem="", **gate)[0]
    assert judge_capture("Normal", rc=0, killed_by=None, n_bytes=n * u,
                         elapsed_s=1.0, log=log.replace("end=ok", "end=error:stall"),
                         verify_problem="", **gate)[0] == "end=error:stall"
    assert "in flight" in judge_capture(
        "Normal", rc=0, killed_by=None, n_bytes=n * u, elapsed_s=1.0,
        log=log.replace("xfer=4194304x8", "xfer=65536x2"), verify_problem="", **gate)[0]
    assert "got" in judge_capture("Normal", rc=0, killed_by=None, n_bytes=n * u - 4,
                                  elapsed_s=1.0, log=log, verify_problem="", **gate)[0]
    # operator Stop mid-capture: no report, own rate is used, sampled data still counts
    why, mbps = judge_capture("Emulation", rc=None, killed_by="cancel",
                              n_bytes=int(780e6), elapsed_s=1.0, log="",
                              verify_problem="", **gate)
    assert why == "" and abs(mbps - 780.0) < 1, (why, mbps)
    why, _ = judge_capture("Emulation", rc=None, killed_by="cancel",
                           n_bytes=int(100e6), elapsed_s=1.0, log="",
                           verify_problem="", **gate)
    assert "rate 100.0" in why, why
    assert "timed out" in judge_capture("Normal", rc=None, killed_by="timeout",
                                        n_bytes=0, elapsed_s=5.0, log="",
                                        verify_problem="", **gate)[0]
    assert "no slogic-report" in judge_capture("Normal", rc=0, killed_by=None,
                                               n_bytes=n * u, elapsed_s=1.0, log="",
                                               verify_problem="", **gate)[0]
    sq = bytes(i & 0xFF for i in range(4096))       # 8ch counter: CHk half-period 2^k
    assert check_periods(sq, 8, 1)[0]
    assert not check_periods(sq[:100] + bytes([sq[100] ^ 1]) + sq[101:], 8, 1)[0]
    assert check_spec(good[:period * u], nch, u, plan.emulation, 16, 0)[0]
    print("soak 判定自测 PASS")
