"""DDR3 test mode (R32_DDRTST, address 0x0050) over USB vendor control.

The usb2_integration firmware (serial 202611120832 and later) carries a
self-contained DDR3 self-test: while enabled it owns the DDR3MI user port and
sweeps bank 0..7 -> row 0..16383 -> a 128-beat column sweep, writing a pattern
and comparing all 128 bits (8 x 16-bit slots).  Each completed sweep increments
a 16-bit counter (~391/s at 133.33 MHz); mismatches latch a per-slot mask, and
a stall watchdog or calibration timeout latches the sticky bit.

THE TEST AND A CAPTURE ARE MUTUALLY EXCLUSIVE.  The RTL has no interlock:
enabling while the ring is streaming stops command issue, discards in-flight
reads and resets the ring engine.  `run()` refuses unless the capture is
stopped (R32_CTRL[0] == 0); the CLI's --force overrides it, the station never
does.

Register access is the U3/U2 vendor protocol (works in both link modes):

    READ  bmRequestType=0xC0 bRequest=0x00 wValue=index<<2 wLength=4
    WRITE bmRequestType=0x40 bRequest=0x01 wValue=index<<2 wLength=4

Read 0x14: [31:16] loop counter, [15:8] per-slot sticky error mask,
[3] busy, [2] calib_ok, [1] enable, [0] sticky.
Write 0x14: [0] enable level, [1] clear toggle (every write with bit1=1 clears
all stickies; bit0 of the same write still applies, so 0x3 = clear + keep
enabled, 0x2 = clear + disable).

Reference: the SLogic USB protocol spec (section 2) and
20261112-ddr3-test-mode-usage.md; the CLI mirrors that document's ddr_test.py
options so either tool can be used on the bench.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from .device_watch import DeviceStatus, libusb_backend
from .i18n import t

DDRTST_INDEX = 0x14         # 0x0050
CTRL_INDEX = 0x01           # 0x0004
CTRL_RUN = 0x01             # R32_CTRL bit0

READ_REQTYPE = 0xC0         # vendor, device, IN
WRITE_REQTYPE = 0x40        # vendor, device, OUT
REQ_READ = 0x00
REQ_WRITE = 0x01
TIMEOUT_MS = 500
RECONNECT_S = 30            # how long the run waits for a re-enumerated board

WRITE_ENABLE = 0x01
WRITE_DISABLE = 0x00
WRITE_CLEAR_KEEP = 0x03     # clear stickies, stay enabled
WRITE_CLEAR_STOP = 0x02     # clear stickies, disable

# the doc's reference figure, sweeps per second
EXPECTED_SWEEPS_PER_S = 391


class DdrError(Exception):
    """The register could not be read/written (device gone, USB error)."""


class DdrRefused(Exception):
    """Refused on purpose: a capture is running (or the register is absent)."""


@dataclass(frozen=True)
class DdrStatus:
    raw: int

    @classmethod
    def decode(cls, raw: int) -> "DdrStatus":
        return cls(raw & 0xFFFFFFFF)

    @property
    def loop(self) -> int:
        return (self.raw >> 16) & 0xFFFF

    @property
    def lanes(self) -> tuple[int, ...]:
        mask = (self.raw >> 8) & 0xFF
        return tuple(i for i in range(8) if mask >> i & 1)

    @property
    def busy(self) -> bool:
        return bool(self.raw >> 3 & 1)

    @property
    def calib_ok(self) -> bool:
        return bool(self.raw >> 2 & 1)

    @property
    def enable(self) -> bool:
        return bool(self.raw >> 1 & 1)

    @property
    def sticky(self) -> bool:
        return bool(self.raw & 1)

    def __str__(self) -> str:
        lanes = ",".join(str(i) for i in self.lanes) or "-"
        return (f"raw=0x{self.raw:08X} sticky={int(self.sticky)} "
                f"enable={int(self.enable)} calib_ok={int(self.calib_ok)} "
                f"busy={int(self.busy)} lanes=[{lanes}] loop={self.loop}")


@dataclass
class DdrResult:
    """What one run observed."""
    calibrated: bool = False
    calib_s: float = 0.0
    sweeps: int = 0                 # counter delta over the watch
    rate: float = 0.0               # sweeps per second
    sticky: bool = False
    lanes: tuple[int, ...] = ()
    stall: bool = False             # our own check: the counter stopped
    final: DdrStatus | None = None
    error: str = ""                 # USB/first-read failure
    reconnects: int = 0             # times the board re-enumerated under us
    stopped_early: bool = False     # the operator cut the watch short
    watch_s: float = 0.0
    events: list[tuple[float, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.calibrated and not self.error and not self.sticky
                and not self.stall and self.sweeps > 0)


class DdrDevice:
    """R32_DDRTST access for one board (found by bus/port, so a second board
    of the same VID:PID is never touched).  The board can re-enumerate on
    the bus while the DDR test runs (the test keeps running in the FPGA);
    reopen() finds it again by its port path and the caller carries on."""

    def __init__(self, dev, status: DeviceStatus | None = None):
        self.dev = dev
        self.status_of = status

    @classmethod
    def open(cls, d: DeviceStatus) -> "DdrDevice":
        import usb.core
        try:
            dev = usb.core.find(idVendor=d.profile.vid, idProduct=d.pid,
                                backend=libusb_backend(),
                                custom_match=lambda x: (x.bus == d.bus and tuple(
                                    x.port_numbers or ()) == d.port_numbers))
        except Exception as e:                                  # noqa: BLE001
            raise DdrError(str(e)) from e
        if dev is None:
            raise DdrError(t("device not found"))
        return cls(dev, d)

    def reopen(self) -> bool:
        """Find the board again after it re-enumerated; False when it is not
        back (yet)."""
        if self.status_of is None:
            return False
        self.close()
        try:
            self.dev = DdrDevice.open(self.status_of).dev
            return True
        except DdrError:
            return False

    def close(self):
        try:
            import usb.util
            usb.util.dispose_resources(self.dev)
        except Exception:                                       # noqa: BLE001
            pass

    # -- raw register access -------------------------------------------
    def read(self, index: int) -> int:
        try:
            data = self.dev.ctrl_transfer(READ_REQTYPE, REQ_READ, index << 2, 0,
                                          4, timeout=TIMEOUT_MS)
        except Exception as e:                                  # noqa: BLE001
            raise DdrError(str(e)) from e
        if len(data) < 4:
            raise DdrError(t("short register read ({n} bytes)").format(n=len(data)))
        return int.from_bytes(bytes(data[:4]), "little")

    def write(self, index: int, value: int) -> None:
        try:
            self.dev.ctrl_transfer(WRITE_REQTYPE, REQ_WRITE, index << 2, 0,
                                   int(value & 0xFFFFFFFF).to_bytes(4, "little"),
                                   timeout=TIMEOUT_MS)
        except Exception as e:                                  # noqa: BLE001
            raise DdrError(str(e)) from e

    # -- the test register ---------------------------------------------
    def status(self) -> DdrStatus:
        return DdrStatus.decode(self.read(DDRTST_INDEX))

    def capture_running(self) -> bool:
        return bool(self.read(CTRL_INDEX) & CTRL_RUN)

    def enable(self) -> None:
        self.write(DDRTST_INDEX, WRITE_ENABLE)

    def disable(self) -> None:
        self.write(DDRTST_INDEX, WRITE_DISABLE)

    def clear(self, keep_enabled: bool = False) -> None:
        self.write(DDRTST_INDEX, WRITE_CLEAR_KEEP if keep_enabled else WRITE_CLEAR_STOP)


def _loop_delta(prev: int, cur: int) -> int:
    """16-bit counter, wrapping."""
    return (cur - prev) & 0xFFFF


def run(ddr: DdrDevice, seconds: float, *, poll_s: float = 1.0,
        expected_rate: float = EXPECTED_SWEEPS_PER_S, force: bool = False,
        cancel=None, on_tick: Callable[[float, DdrStatus], None] | None = None,
        log: Callable[[str], None] | None = None,
        calib_timeout_s: float = 3.0,
        stall_after_s: float | None = None,
        reconnect_s: float = RECONNECT_S) -> DdrResult:
    """Enable, wait for calibration and liveness, watch for `seconds`
    (0 = until stopped), then stop + clear and read the frozen counter.

    Follows the document's host flow: only while the capture is stopped,
    poll calib_ok and the loop counter, then `0x0050 = 0` and `0x0050 = 2`."""
    log = log or (lambda _s: None)
    res = DdrResult()

    def _recover(timeout_s: float) -> None:
        """After a USB error: wait for the board to re-enumerate and reopen
        it.  The test runs in the FPGA, so nothing is lost; the counter picks
        up where it was.  Raises the last DdrError if it does not come back."""
        announced = False
        waited = 0.0
        while True:
            if not announced:
                log(t("USB link dropped; waiting for the board to come back"))
                announced = True
            time.sleep(0.5)
            waited += 0.5
            if ddr.reopen():
                res.reconnects += 1
                log(t("board is back; the test kept running"))
                return
            if waited > timeout_s:
                raise
            if cancel is not None and cancel.is_set():
                raise

    def status(timeout_s: float = reconnect_s) -> DdrStatus:
        try:
            return ddr.status()
        except DdrError:
            _recover(timeout_s)
            return ddr.status()

    def write(index: int, value: int, timeout_s: float = reconnect_s) -> None:
        try:
            ddr.write(index, value)
        except DdrError:
            _recover(timeout_s)
            ddr.write(index, value)

    if not force and ddr.capture_running():
        raise DdrRefused(t("a capture is running (R32_CTRL bit0=1): stop it "
                           "before enabling the DDR test"))
    first = status()
    if first.raw == 0:
        raise DdrRefused(t("R32_DDRTST reads 0: this firmware has no DDR3 test "
                           "mode (needs the usb2_integration image, "
                           "202611120832 or later)"))
    write(CTRL_INDEX, 0x00)              # release RST (the write is ignored while set)
    write(DDRTST_INDEX, WRITE_ENABLE)
    t0 = time.monotonic()
    try:
        # calibration + first counter movement
        prev = status()
        while True:
            cur = status()
            res.events.append((time.monotonic() - t0, f"start {cur}"))
            if cur.calib_ok and _loop_delta(prev.loop, cur.loop) > 0:
                res.calibrated, res.calib_s = True, time.monotonic() - t0
                break
            if cur.sticky:
                res.sticky, res.lanes = True, cur.lanes
                res.events.append((time.monotonic() - t0, f"sticky at start {cur}"))
                break
            if time.monotonic() - t0 > calib_timeout_s:
                res.error = t("calibration did not complete within {s:g} s "
                              "({st})").format(s=calib_timeout_s, st=cur)
                break
            prev = cur
            if cancel is not None and cancel.is_set():
                res.stopped_early = True
                break
            time.sleep(0.05)
        # the watch
        if res.calibrated:
            watch_t0, last_move = time.monotonic(), time.monotonic()
            prev = status()
            while not res.sticky and not res.stall:
                if cancel is not None and cancel.is_set():
                    res.stopped_early = True
                    break
                elapsed = time.monotonic() - watch_t0
                if seconds and elapsed >= seconds:
                    break
                time.sleep(min(poll_s, max(0.05, (seconds - elapsed) if seconds else poll_s)))
                cur = status()
                d = _loop_delta(prev.loop, cur.loop)
                res.sweeps += d
                if d:
                    last_move = time.monotonic()
                elif time.monotonic() - last_move > (
                        stall_after_s if stall_after_s is not None
                        else max(2.0, 3 * poll_s)):
                    res.stall = True
                    res.events.append((time.monotonic() - t0, "loop counter stopped"))
                if cur.sticky:
                    res.sticky, res.lanes = True, cur.lanes
                    res.events.append((time.monotonic() - t0, f"sticky {cur}"))
                prev = cur
                if on_tick is not None:
                    on_tick(time.monotonic() - watch_t0, cur)
            res.watch_s = time.monotonic() - watch_t0
            res.rate = res.sweeps / res.watch_s if res.watch_s > 0 else 0.0
    finally:
        try:
            # short timeout: a Stop must not wait half a minute on a board
            # that is gone; the next enable clears stickies anyway
            write(DDRTST_INDEX, WRITE_DISABLE, 3.0)
            write(DDRTST_INDEX, WRITE_CLEAR_STOP, 3.0)
            res.final = status(3.0)
        except DdrError as e:
            res.error = res.error or str(e)
    if res.final is not None:
        res.sticky = res.sticky or res.final.sticky
        res.lanes = res.lanes or res.final.lanes
    return res


def verdict_text(res: DdrResult, expected_rate: float = EXPECTED_SWEEPS_PER_S) -> str:
    """One-line verdict for the report, '' when it passed."""
    if res.error:
        return res.error
    if not res.calibrated:
        return t("no calibration/liveness (the test never started)")
    if res.sticky:
        if res.lanes:
            return t("sticky error, lanes {lanes} (DDR data/timing)").format(
                lanes="/".join("slot %d" % i for i in res.lanes))
        return t("sticky error without a lane (stall watchdog or calibration timeout)")
    if res.stall:
        return t("the sweep counter stopped")
    if not res.sweeps:
        return t("no sweep completed")
    return ""


# --------------------------------------------------------------------- CLI
def _find_board(serial: str | None) -> DeviceStatus | None:
    from .device_watch import Mode, scan_devices
    from .profiles import load_profiles
    profiles, _ = load_profiles()
    for d in scan_devices(profiles):
        if d.mode is not Mode.APP or d.profile.ddr_test is None:
            continue
        if serial and (d.serial or "") != serial:
            continue
        return d
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="python -m slogicpt.ddr", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="read and decode R32_DDRTST (default)")
    ap.add_argument("--start", action="store_true", help="enable, wait for calibration and liveness")
    ap.add_argument("--stop", action="store_true", help="disable the test (write 0)")
    ap.add_argument("--clear", action="store_true", help="clear stickies (keeps the enable level)")
    ap.add_argument("--watch", type=float, metavar="SEC", help="poll for SEC seconds, checking the counter")
    ap.add_argument("--interval", type=float, default=1.0, help="--watch sampling interval (default 1.0)")
    ap.add_argument("--serial", help="pick the board by USB serial")
    ap.add_argument("--force", action="store_true", help="allow --start while a capture runs (dangerous)")
    ap.add_argument("--json", metavar="PATH", help="write a machine-readable report")
    ap.add_argument("--quiet", action="store_true", help="only JSON / exit code")
    ap.add_argument("--self-test", action="store_true", help="offline self-test (no device)")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if not (args.status or args.start or args.stop or args.clear or args.watch):
        args.status = True

    def say(s: str) -> None:
        if not args.quiet:
            print(s, flush=True)

    events: list[dict] = []
    result = {"refused": False, "sticky_observed": False, "stall_detected": False,
              "last_status": None, "exit_code": 0}
    code = 0
    board = _find_board(args.serial)
    if board is None:
        say("device not found" + (f" (serial {args.serial})" if args.serial else ""))
        result.update(refused=False, exit_code=3)
        code = 3
    else:
        ddr = DdrDevice.open(board)
        say(f"# {board}")
        try:
            if args.start:
                if not args.force and ddr.capture_running():
                    say("refused: a capture is running (R32_CTRL bit0=1)")
                    result.update(refused=True, exit_code=4)
                    code = 4
                else:
                    ddr.write(CTRL_INDEX, 0x00)
                    ddr.enable()
                    t0, prev = time.monotonic(), ddr.status()
                    while time.monotonic() - t0 < 3.0:
                        cur = ddr.status()
                        if cur.calib_ok and _loop_delta(prev.loop, cur.loop):
                            break
                        prev = cur
                        time.sleep(0.05)
                    st = ddr.status()
                    say(f"start: raw=0x{st.raw:08X} calib_ok={int(st.calib_ok)} loop={st.loop}")
                    events.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "action": "start", "raw": st.raw, "decoded": str(st)})
            if args.watch:
                if not ddr.status().enable:
                    ddr.enable()
                t0, prev, sweeps, last_move = time.monotonic(), ddr.status(), 0, time.monotonic()
                while time.monotonic() - t0 < args.watch:
                    time.sleep(min(args.interval, max(0.05, args.watch - (time.monotonic() - t0))))
                    cur = ddr.status()
                    d = _loop_delta(prev.loop, cur.loop)
                    sweeps += d
                    if d:
                        last_move = time.monotonic()
                    say(f"watch t={time.monotonic() - t0:7.3f}s {cur}")
                    if cur.sticky:
                        result["sticky_observed"] = True
                    if (time.monotonic() - last_move) > max(2.0, 3 * args.interval):
                        result["stall_detected"] = True
                        say("STALL: the loop counter stopped")
                    prev = cur
                    events.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "action": "watch", "raw": cur.raw, "decoded": str(cur)})
                    if result["sticky_observed"] or result["stall_detected"]:
                        break
                say(f"watch: {sweeps} sweeps in {args.watch:g}s "
                    f"({sweeps / args.watch if args.watch else 0:.0f}/s)")
            if args.stop:
                ddr.disable()
            if args.clear:
                ddr.clear(keep_enabled=ddr.status().enable)
            if args.status or args.stop or args.clear:
                st = ddr.status()
                say(f"status: {st}")
                result["last_status"] = st.raw
                events.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "action": "status", "raw": st.raw, "decoded": str(st)})
                if st.sticky:
                    result["sticky_observed"] = True
            if result["sticky_observed"] or result["stall_detected"]:
                code = 2
        except DdrRefused as e:
            say(f"refused: {e}")
            result.update(refused=True, exit_code=4)
            code = 4
        except DdrError as e:
            say(f"error: {e}")
            result["exit_code"] = 3
            code = 3
        finally:
            ddr.close()
    result["exit_code"] = code
    if args.json:
        doc = {"tool": "slogicpt.ddr", "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                     time.gmtime()),
               "device": str(board) if board else None,
               "params": vars(args), "events": events, "result": result}
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        say(f"json: {args.json}")
    return code


class FakeRegs:
    """R32_DDRTST / R32_CTRL semantics; the sweep counter advances on
    every status read until `stall_at`, unless told to go sticky."""

    def __init__(self, loop_step: int = 5, sticky_at: int | None = None,
                 stall_at: int | None = None, zero=False, ctrl=0):
        self.zero = zero
        self.regs = {0x01: ctrl, 0x14: 0 if zero else 0x4}   # calib_ok
        self.loop_step, self.sticky_at, self.stall_at = loop_step, sticky_at, stall_at
        self.reads = self.clears = 0

    def ctrl_transfer(self, reqtype, request, value, index, data, timeout=None):
        reg = value >> 2
        if reqtype == 0xC0:
            self.reads += 1
            v = self.regs.get(reg, 0)
            if reg == 0x14 and not self.zero:
                loop = (v >> 16) & 0xFFFF
                if self.stall_at is None or self.reads < self.stall_at:
                    loop = (loop + self.loop_step) & 0xFFFF
                v = (loop << 16) | (v & 0xFFFF)
                v = (v & ~0x8) | (0x8 if (v & 0x2 and v & 0x4) else 0)  # busy
                if self.sticky_at is not None and self.reads >= self.sticky_at:
                    v = (v & ~0xFFFF) | 0x0001 | (0x08 << 8)   # sticky + slot 3
                self.regs[0x14] = v
            return list(v.to_bytes(4, "little"))
        w = int.from_bytes(bytes(data), "little") if data else 0
        if reg == 0x14:
            # write bit0 is the enable LEVEL -> read-back bit1; write bit1
            # is the clear toggle (read-back bit0 is sticky)
            v = (self.regs[0x14] & ~0x2) | (0x2 if w & 0x1 else 0)
            if w & 0x2:
                self.clears += 1
                v &= ~0xFF01                            # clear lanes + sticky
            v = (v & ~0x8) | (0x8 if (v & 0x2 and v & 0x4) else 0)  # busy
            self.regs[0x14] = v
        else:
            self.regs[reg] = w
        return len(data) if data else 0


def _self_test() -> int:
    """Offline self-test: the fake register device drives the whole flow."""
    st = DdrStatus.decode(0x0013000E)
    assert (st.loop, st.enable, st.calib_ok, st.busy, st.sticky, st.lanes) == (19, 1, 1, 1, 0, ()), st
    st = DdrStatus.decode(0x0000A801)            # 0xA8 = slots 3, 5, 7 + sticky
    assert st.sticky and st.lanes == (3, 5, 7), st

    try:
        run(DdrDevice(FakeRegs(zero=True)), 0.1, poll_s=0.01)
        raise AssertionError("a firmware without the test mode must be refused")
    except DdrRefused as e:
        print("zero:", e)

    dev = FakeRegs()
    r = run(DdrDevice(dev), 0.3, poll_s=0.01)
    assert r.ok and r.sweeps > 0 and r.final.enable == 0, r
    assert dev.clears == 1, dev.clears
    assert verdict_text(r) == "", verdict_text(r)

    dev = FakeRegs(sticky_at=8)
    r = run(DdrDevice(dev), 1.0, poll_s=0.01)
    assert r.sticky and r.lanes == (3,), r
    print("sticky:", verdict_text(r))
    assert "slot 3" in verdict_text(r)

    dev = FakeRegs(stall_at=6)                    # the counter stops once the watch begins
    r = run(DdrDevice(dev), 1.0, poll_s=0.01, stall_after_s=0.1)
    assert r.stall and not r.ok, r
    print("stall:", verdict_text(r))

    dev = FakeRegs(ctrl=1)
    try:
        run(DdrDevice(dev), 0.1, poll_s=0.01)
        raise AssertionError("a running capture must be refused")
    except DdrRefused as e:
        print("refused:", e)
    assert dev.regs[0x14] & 0x1 == 0, "the test must not be enabled"

    dev = FakeRegs()
    r = run(DdrDevice(dev), 0.2, poll_s=0.01, force=True)
    assert r.ok, r

    class Vanishing(FakeRegs):
        """The USB link drops after a few reads and never comes back."""

        def ctrl_transfer(self, *a, **kw):
            if self.reads >= 3:
                raise OSError(19, "No such device (it may have been disconnected)")
            return super().ctrl_transfer(*a, **kw)

    try:
        run(DdrDevice(Vanishing()), 1.0, poll_s=0.01, reconnect_s=0.3)
        raise AssertionError("a vanished board must fail the run")
    except DdrError as e:
        print("vanished:", e)

    class OnceVanishing(Vanishing):
        """Drops once, then behaves (a board that re-enumerated)."""
        drops = 0

        def ctrl_transfer(self, *a, **kw):
            if self.drops == 0 and self.reads >= 3:
                self.drops = 1
                raise OSError(19, "No such device (it may have been disconnected)")
            return FakeRegs.ctrl_transfer(self, *a, **kw)

    class Reconnecting(DdrDevice):
        def __init__(self):
            super().__init__(OnceVanishing())
            self.reopens = 0

        def reopen(self):
            self.reopens += 1
            self.dev = FakeRegs()            # a fresh handle to the same board
            return True

    rc = Reconnecting()
    r = run(rc, 0.5, poll_s=0.01, reconnect_s=2.0)
    assert r.ok and r.reconnects == 1 and rc.reopens == 1, (r.ok, r.reconnects, rc.reopens)
    print("reconnect:", r.sweeps, "sweeps, reconnects", r.reconnects)
    print("ddr 自测 PASS")
    return 0



if __name__ == "__main__":
    sys.exit(main())
