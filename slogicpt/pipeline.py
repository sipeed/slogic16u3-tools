"""Production-test pipeline engine.

Production test of a board running its app firmware: USB link gate, then
the fixture pattern tests, once per link in [usb] links -> PASS/FAIL summary.
(The JTAG flashing of blank boards and the app-firmware OTA are done by
separate tools.)

Mode-switch policy (per user decision): after each flashing stage, wait for
the target PID; on timeout, raise a non-blocking operator prompt ("please
replug") and KEEP polling until the device shows up or the run is cancelled.
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from . import device_watch
from .i18n import t
from .procutil import kill_image
from .profiles import OUTPUT_DIR, ProductProfile, format_rate
from .sigrok import CaptureError, SigrokCli


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class PipelineCallbacks:
    on_log: Callable[[str], None] = lambda s: None
    on_step: Callable[[str, StepStatus], None] = lambda name, st: None
    on_user_prompt: Callable[[str], None] = lambda s: None
    on_user_prompt_clear: Callable[[], None] = lambda: None
    # one-line reason a step failed, sent just before its FAILED status
    on_step_note: Callable[[str, str], None] = lambda name, note: None
    # what is happening right now ("M4 32ch@200M @0.9V", "seen: 10G") -- display
    on_progress: Callable[[str], None] = lambda text: None
    # one pattern mode finished: (link name or "", mode, ok, short reason)
    on_mode_result: Callable[[str, int, bool, str], None] = \
        lambda link, mode, ok, note: None
    on_finished: Callable[[bool, str], None] = lambda ok, report: None


@dataclass
class StepDef:
    id: str                     # stable id, GUI step rows key on this
    label: str                  # operator-facing name
    run: Callable[[], bool]     # True = passed
    abort_on_fail: bool = True  # capture steps keep going to aid diagnosis


# Host-side failures that a second attempt usually clears, because the cause is
# the previous capture's process (not yet released the device) rather than the
# board.  Matched against what sigrok-cli printed.  An analyzer-side fault --
# the stream never starting ("acquisition stall"), "device STOP failed",
# "configure failed" -- is NOT here: the analyzer is the product under test,
# so those fail the step, and the operator can retry it by hand.
_TRANSIENT = (
    "libusb_error_access",
    "libusb_error_busy",
    "no devices found",
    "access is denied",
    "resource busy",
    "failed to open device",
)


def _transient_reason(e: Exception) -> str:
    """The sigrok line that marks a capture failure as host-side transient,
    or "" if it is not one."""
    for line in (str(e) + "\n" + getattr(e, "output", "")).splitlines():
        low = line.lower()
        if any(s in low for s in _TRANSIENT):
            return line.strip()
    return ""


class PipelineAbort(Exception):
    pass


# Step-id -> English label. Shared by sequence_plan() and _sequence_defs() so
# the two step lists never drift; each label is wrapped in t() at use sites.
STEP_LABELS: dict[str, str] = {
    "link:check": "Check USB link",
}


def _sequence_spec(profile: ProductProfile
                   ) -> list[tuple[str, str, str, tuple, bool]]:
    """(step id, label, Pipeline method, method args, abort_on_fail) of the
    production test.  The ONLY definition of the steps: sequence_plan() (the
    GUI's rows) and Pipeline._sequence_defs() (what runs) both read it, so the
    two can never drift apart -- that drift once left the engine running 17
    steps while the GUI showed 6."""
    spec: list[tuple[str, str, str, tuple, bool]] = []
    # one pass per link in [usb] links, e.g. 32U3 = 10G then 5G,
    # so the board is proven on both links a user may plug it into.  The first
    # link is a gate that aborts the run (a board that cannot reach 10G is a
    # FAIL without testing); each later pass asks the operator to move the
    # cable to that port and waits for the board to come back at that speed.
    if profile.reconfig:
        # first of all: the board must leave for DFU and come back to APP on
        # its own (USB RECONFIG) -- no pattern is tested on a board that cannot
        spec.append(("mode:dfu-app", t("Switch to DFU and back to APP"),
                     "_mode_round_trip", (), True))
    links = profile.links or (None,)
    for n, link in enumerate(links):
        name = _link_name(link)
        sfx = f"@{name}" if link is not None else ""
        if n == 0:
            spec.append((f"link:check{sfx}",
                         t("Check USB link ({link})").format(link=name) if link
                         else t(STEP_LABELS["link:check"]),
                         "_check_link", (link,), True))
        else:
            spec.append((f"link:replug{sfx}",
                         t("Re-plug into a {link} port").format(link=name),
                         "_await_link", (link,), True))
        # the pattern step brings the fixture up itself (fixture.ensure_running:
        # SRAM loaded once per session, again only if it stops answering)
        spec.append((f"signal:all{sfx}",
                     t("Signal generator · all {n} patterns").format(
                         n=len(profile.plan)) + (f" ({name})" if link else ""),
                     "_run_all_modes", (link,), False))
    return spec


def _link_name(link: int | None) -> str:
    return device_watch.link_name(link) if link else "-"


def sequence_plan(profile: ProductProfile) -> list[tuple[str, str]]:
    """(id, label) of the sequence, without a Pipeline instance -- the GUI
    builds its step list from this so ids always match."""
    return [(sid, label) for sid, label, *_ in _sequence_spec(profile)]


class Pipeline:
    def __init__(self, profile: ProductProfile, sigrok: SigrokCli | None,
                 callbacks: PipelineCallbacks, steps: list[StepDef]):
        self.profile = profile
        self.sigrok = sigrok
        self.cb = callbacks
        self.steps = steps
        self.cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.report_lines: list[str] = []
        self.app_serial: str | None = None   # APP USB SerialNumber (version id)
        # (mode label, channel text, check, measured, expected, passing channels)
        self.failures: list[tuple[str, str, str, str, str, str]] = []

    # ---- construction helpers -------------------------------------------

    def _sequence_defs(self) -> list[StepDef]:
        return [StepDef(sid, label,
                        (lambda m=getattr(self, meth), a=args: m(*a)),
                        abort_on_fail=abort)
                for sid, label, meth, args, abort in _sequence_spec(self.profile)]

    @classmethod
    def for_board(cls, profile: ProductProfile,
                  sigrok: SigrokCli | None, callbacks: PipelineCallbacks, *,
                  all_profiles: list[ProductProfile] | None = None) -> "Pipeline":
        """Every step for the board that was just detected.  `all_profiles`
        lets link:check see analyzers of OTHER products too (sigrok-cli cannot
        pick one device out of several)."""
        p = cls(profile, sigrok, callbacks, [])
        p._all_profiles = all_profiles
        p.steps = p._sequence_defs()
        return p

    @classmethod
    def single_step(cls, profile: ProductProfile, sigrok: SigrokCli | None,
                    callbacks: PipelineCallbacks, step_id: str, *,
                    all_profiles: list[ProductProfile] | None = None) -> "Pipeline":
        """Manual mode: run exactly one step by id."""
        p = cls(profile, sigrok, callbacks, [])
        p._all_profiles = all_profiles
        matches = [d for d in p._sequence_defs() if d.id == step_id]
        if not matches:
            raise ValueError(t("Unknown step: {step_id}").format(step_id=step_id))
        p.steps = matches
        return p

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_cancel(self) -> None:
        self.cancel.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        all_pass = True
        try:
            for step in self.steps:
                self.cb.on_step(step.id, StepStatus.RUNNING)
                if self.cancel.is_set():
                    raise PipelineAbort(t("Cancelled"))
                r0, f0 = len(self.report_lines), len(self.failures)
                ok = step.run()
                if not ok:
                    self.cb.on_step_note(step.id, self._step_note(r0, f0))
                self.cb.on_step(step.id,
                                StepStatus.PASSED if ok else StepStatus.FAILED)
                if not ok:
                    all_pass = False
                    if step.abort_on_fail:
                        raise PipelineAbort(
                            t("Step \"{label}\" failed").format(label=step.label))
        except PipelineAbort as e:
            all_pass = False
            self._log(t("Flow aborted: {e}").format(e=e))
            self.report_lines.append(t("Flow aborted: {e}").format(e=e))
        except Exception:
            all_pass = False
            self._log(t("Flow exception:") + "\n" + traceback.format_exc())
            self.report_lines.append(t("Flow exception, see log for details"))
        finally:
            # Release the fixture's serial port. Without this, a single-step run
            # leaves the port open and the NEXT step cannot open it:
            #   PermissionError(13, 'Access is denied') on COM30
            # which the operator sees as "no signal generator" from then on.
            self._signal_close()
            self.cb.on_user_prompt_clear()

            # End with "which wire, in which mode" rather than leaving the operator
            # to find it among the per-mode lines and the capture noise.
            #
            # Every failing verdict was recorded as it happened, with the channel
            # text the check itself produced: a per-channel failure names one input
            # ("CH7"), while a structured one names the wires it spans ("CH4..CH7"
            # for an SPI master, "CH24/CH25" for I2C). That is the unit the operator
            # has to go and look at, so it is what the summary leads with.
            if self.failures:
                lines = [t("---- failing channels ({n}) ----").format(
                    n=len(self.failures))]
                for mode, where, kind, got, want, ok_in in self.failures:
                    lines.append("  %-14s %-8s %s" % (where, kind, mode))
                    lines.append("      %-6s %s" % (t("got"), got))
                    lines.append("      %-6s %s" % (t("want"), want))
                    if ok_in:
                        # A group verdict names several wires, and some of them
                        # usually measured fine. Without this line "CH24/CH25 i2c
                        # FAIL" reads as two suspect wires; with it, the reader
                        # sees CH24 passed its own 100 kHz check and only CH25 is
                        # actually in question.
                        lines.append("      %-6s %s" % (t("ok in group"), ok_in))
                for ln in lines:
                    self._log(ln)
                self.report_lines.extend(lines)

            lines = list(self.report_lines)
            if self.app_serial:
                lines.insert(0, f"APP SN: {self.app_serial}")
            self.cb.on_finished(all_pass, "\n".join(lines))

    def _step_note(self, r0: int, f0: int) -> str:
        """One line on why a step failed, built from the report lines and the
        failing verdicts it added: "M5: CH20 CH21 (freq); M0: capture failed".
        Display only -- the verdict itself was made by the step."""
        if self.cancel.is_set():
            return t("Cancelled")
        parts: dict[str, str] = {}
        new_fail = self.failures[f0:]
        for ln in self.report_lines[r0:]:
            if ": FAIL" not in ln:
                continue
            head, _, rest = ln.partition(": FAIL")
            reason = rest.strip().strip("()").strip()
            mode = head.split()[0] if head[:1] == "M" and head[1:2].isdigit() else None
            if mode is None:
                parts.setdefault(head, reason or "FAIL")
                continue
            if mode in parts:
                continue
            chans = [f for f in new_fail if f[0].split()[0] == mode]
            if chans:
                where = list(dict.fromkeys(c[1] for c in chans))
                kinds = "/".join(dict.fromkeys(str(c[2]) for c in chans))
                shown = " ".join(where[:6]) + (" …" if len(where) > 6 else "")
                parts[mode] = f"{shown} ({kinds})"
            else:
                parts[mode] = reason
        return "; ".join(f"{k}: {v}" if v else k for k, v in parts.items())

    # ---- steps -----------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.cb.on_log(msg)

    _all_profiles = None        # every product profile (link:check sees all analyzers)
    _link = None                # link of the current test pass (picks the plan configs)

    # ---- fixture USB presence -------------------------------------------

    def _fixture_ports(self) -> list[str]:
        """夹具 Tang Nano 的串口（按 fixture.toml 里的 USB 身份认出，见
        fpga.fixture_ports）。"""
        from .fpga import fixture_ports
        return fixture_ports(self.profile.signal_source.usb)

    def _wait_for_fixture(self) -> bool:
        """确保夹具已插上，需要时提示操作员并等待。

        这里不弹"请点继续"，而是**直接轮询等到操作员真的做完** —— 少一次点击，
        也不会出现"提示还在、人已经点过了"的错位。
        """
        if self._fixture_ports():
            return True
        msg = t("Please PLUG IN the signal-generator fixture (Tang Nano 9K) "
                "and wait -- the flow continues by itself")
        self._log("[fixture] " + msg)
        self.cb.on_user_prompt(msg)
        try:
            waited = 0.0
            while not self.cancel.is_set():
                ports = self._fixture_ports()
                if ports:
                    self._log("[fixture] " + t("Fixture detected ({p}), continuing").format(
                        p=", ".join(ports)))
                    return True
                time.sleep(0.5)
                waited += 0.5
                # 每 30 秒重述一次，避免操作员以为卡住了。waited 按 0.5 递增，
                # 直接对它取模：用 int(waited) 会让 30.0 和 30.5 各打一行。
                if waited >= 30 and waited % 30 == 0:
                    self._log("[fixture] " + t("still waiting ({s}s): {m}").format(
                        s=int(waited), m=msg))
            return False
        finally:
            self.cb.on_user_prompt_clear()

    def _check_link(self, link: int | None) -> bool:
        """Link gate: exactly one analyzer on the host, it is THIS
        product running its app firmware, and its USB link reached the
        product's minimum (the first of [usb] links, e.g. 10G).  Fails -- and aborts the
        run -- otherwise, so a slow link is never "tested".

        Exactly one, because sigrok-cli cannot choose between several SLogic
        analyzers (the driver rejects conn=) and would capture from any of them."""
        p = self.profile
        devs = device_watch.scan_devices(self._all_profiles or [p])
        apps = [d for d in devs if d.mode is device_watch.Mode.APP]
        mine = [d for d in apps if d.profile.id == p.id]
        need = link
        need_txt = device_watch.link_name(need) if need else "-"
        self._log(f"[criteria] link: exactly 1 analyzer online, {p.display_name} in "
                  f"APP mode, link >= {need_txt}; seen: "
                  + (", ".join(str(d) for d in devs) or "none"))

        def fail(why: str) -> bool:
            self._log(why)
            self.report_lines.append(t("USB link") + ": FAIL (" + why + ")")
            return False

        if len(apps) > 1:
            return fail(t("{n} analyzers online; keep only the board under test "
                          "connected").format(n=len(apps)))
        if not mine:
            if any(d.profile.id == p.id and d.mode is device_watch.Mode.DFU
                   for d in devs):
                return fail(t("{name} is in DFU mode: the app firmware has not been "
                              "flashed").format(name=p.display_name))
            return fail(t("No {name} running its app firmware was found").format(
                name=p.display_name))
        d = mine[0]
        self.app_serial = d.serial
        speed = d.speed or self._sigrok_link_speed()
        got_txt = device_watch.link_name(speed)
        if need and (speed or 0) < need:
            return fail(t("link too slow: {got}, needs {need} (check the cable and "
                          "the host port)").format(got=got_txt, need=need_txt))
        self._log(t("USB link {got} (needs {need}): OK").format(got=got_txt, need=need_txt))
        self.report_lines.append(t("USB link") + f": PASS ({got_txt})")
        return True

    def _await_link(self, link: int) -> bool:
        """A later link pass: ask the operator to move the cable to a `link`
        port, then wait (until cancelled) for exactly one of this product's
        boards to come back in APP mode at EXACTLY that speed -- a 10G link
        does not test 5G.  There is no firmware command to renegotiate a slower
        link, so the re-plug is manual; the run continues by itself."""
        p = self.profile
        want = device_watch.link_name(link)
        self._log(f"[criteria] link pass: exactly 1 {p.display_name} in APP mode at {want}")
        self.cb.on_user_prompt(t("Move the board's USB cable to a {link} port; the "
                                 "test continues when it is detected").format(link=want))
        last = None
        try:
            while not self.cancel.is_set():
                devs = device_watch.scan_devices(self._all_profiles or [p])
                apps = [d for d in devs if d.mode is device_watch.Mode.APP]
                mine = [d for d in apps if d.profile.id == p.id]
                seen = ", ".join(str(d) for d in devs) or "none"
                if seen != last:
                    self._log(t("Waiting for {link}: seen {devs}").format(link=want, devs=seen))
                    self.cb.on_progress(t("Now detected: {devs}").format(
                        devs=", ".join(d.link for d in mine) or t("no board")))
                    last = seen
                if len(apps) == 1 and mine and (mine[0].speed or
                                                self._sigrok_link_speed()) == link:
                    self._log(t("USB link {got} (needs {need}): OK").format(got=want, need=want))
                    self.report_lines.append(t("USB link") + f": PASS ({want})")
                    return True
                time.sleep(0.5)
            return False
        finally:
            self.cb.on_user_prompt_clear()

    def _mode_round_trip(self) -> bool:
        """APP -> DFU -> APP by USB RECONFIG.  Exactly one SLogic must be online,
        this product in APP mode; after each request it must re-enumerate in
        the other mode within [timeouts] mode_switch_s, again as the only SLogic
        on the host, and come back to APP with the same USB serial -- which is
        the app firmware's version id (two boards measured with one value), so
        this proves the same firmware came back, not the same board.

        Not "on the same port": DFU runs at USB 2, which the host puts on
        another bus with other port numbers than the 10G link (measured: APP
        on bus 2 port 3, DFU on bus 1 port 3), and DFU reports no serial.  The
        link speed is the next step's business."""
        p = self.profile
        DFU, APP = device_watch.Mode.DFU, device_watch.Mode.APP
        tmo = p.timeouts.mode_switch_s
        self._log(f"[criteria] mode switch: only SLogic online = 1 {p.display_name} "
                  f"in APP; RECONFIG -> it alone in DFU ({p.dfu_pid:#06x}) within "
                  f"{tmo:.0f}s; RECONFIG -> it alone in APP ({p.app_pid:#06x}) within "
                  f"{tmo:.0f}s, same USB serial (firmware id) as before")

        def fail(why: str) -> bool:
            self._log(why)
            self.report_lines.append(t("DFU/APP switch") + ": FAIL (" + why + ")")
            return False

        def alone(mode):
            """The one SLogic on the host, if it is this product in `mode`."""
            devs = device_watch.scan_devices(self._all_profiles or [p])
            if len(devs) == 1 and devs[0].profile.id == p.id and devs[0].mode is mode:
                return devs[0]
            return None

        devs = device_watch.scan_devices(self._all_profiles or [p])
        if len(devs) > 1:
            return fail(t("{n} analyzers online; keep only the board under test "
                          "connected").format(n=len(devs)))
        board = alone(APP)
        if board is None:
            return fail(t("No {name} running its app firmware was found").format(
                name=p.display_name))
        serial = board.serial
        legs = []
        for frm, to in ((APP, DFU), (DFU, APP)):
            self.cb.on_progress(t("Switching {a} -> {b}").format(a=frm.value, b=to.value))
            t0 = time.time()
            why = device_watch.reconfig(board)
            if why:
                return fail(t("RECONFIG to the {mode} device failed: {e}").format(
                    mode=frm.value, e=why))
            self._log(t("RECONFIG sent ({a} -> {b}), waiting for the {b} device…")
                      .format(a=frm.value, b=to.value))
            board = None
            while time.time() - t0 < tmo:
                if self.cancel.is_set():
                    return False
                board = alone(to)
                if board is not None:
                    break
                time.sleep(0.3)
            if board is None:
                seen = ", ".join(str(d) for d in device_watch.scan_devices(
                    self._all_profiles or [p])) or t("nothing")
                return fail(t("not back in {mode} mode within {s}s after RECONFIG "
                              "(seen: {seen})").format(mode=to.value, s=f"{tmo:.0f}",
                                                       seen=seen))
            legs.append(f"{frm.value}->{to.value} {time.time() - t0:.1f}s")
            self._log(t("{mode} device back: {dev}").format(mode=to.value, dev=board))
        if serial and board.serial != serial:
            return fail(t("APP came back with another serial (firmware id) {a}, was {b}").format(
                a=board.serial, b=serial))
        self.app_serial = board.serial
        self.report_lines.append(t("DFU/APP switch") + ": PASS (" + ", ".join(legs) + ")")
        return True

    def _sigrok_link_speed(self) -> int | None:
        """Link speed from sigrok's scan line ("... - (10G) Sipeed SLogic32 U3"),
        for hosts whose libusb does not report it (old Windows libusb DLLs)."""
        if self.sigrok is None:
            return None
        import re
        try:
            lines = self.sigrok.scan(self.profile.driver)
        except Exception:                                       # noqa: BLE001
            return None
        for line in lines:
            m = re.search(r"\((USB2|5G|10G|20G)\)", line)
            if m:
                from .profiles import parse_link
                return parse_link(m.group(1))
        return None

    # =======================================================================
    # Fixture signal source (Tang Nano 9K)
    # =======================================================================

    def _signal_source(self):
        """Open the fixture once and keep it open.

        Keeping the port open matters: the port round trip is about 6 ms, while
        restarting a helper process per mode change cost ~1.7 s. Across ten modes
        that is the difference between one second and seventeen.
        """
        if getattr(self, "_signal", None) is not None:
            return self._signal
        from . import fixture
        self._signal = fixture.ensure_running(self.profile.signal_source, self._log,
                                              cancel=self.cancel)
        return self._signal

    def _signal_close(self) -> None:
        src = getattr(self, "_signal", None)
        if src is not None:
            try:
                src.close()
            except Exception:                       # noqa: BLE001
                pass
            self._signal = None

    def _fixture_ready(self) -> bool:
        """The fixture is plugged in (else prompt and wait), running testgen
        and answering on its serial port (loaded into SRAM when needed)."""
        if not self._wait_for_fixture():
            self.report_lines.append("fixture: FAIL (" + t("not plugged in") + ")")
            return False
        try:
            self._signal_source()
        except Exception as e:                      # noqa: BLE001
            self._log(t("Signal generator unavailable: {e}").format(e=e))
            self.report_lines.append(
                "fixture: FAIL (" + t("no signal generator") + ")")
            return False
        self._log(t("Signal generator on {p}, alive, mode 0").format(
            p=getattr(self._signal, "port", "?")))
        return True

    def _run_all_modes(self, link: int | None = None) -> bool:
        """Every pattern mode in one step.

        Runs all of them even if an early one fails: the modes are independent
        and a fault in one pattern must not hide the state of the other nine.
        A single line per mode goes into the operator report, so the summary
        still reads as ten results rather than one aggregate.
        """
        self._link = link
        if link is not None:
            self.report_lines.append("-- %s --" % _link_name(link))
        if not self._fixture_ready():
            return False
        summary_before = len(self.report_lines)
        all_ok = True
        for entry in self.profile.plan:
            if self.cancel.is_set():
                break
            before, f0 = len(self.report_lines), len(self.failures)
            self.cb.on_progress("M%d %s" % (entry.mode, entry.label))
            ok = self._run_mode(entry)
            all_ok = all_ok and ok
            self.cb.on_mode_result(_link_name(link) if link else "", entry.mode, ok,
                                   "" if ok else self._step_note(before, f0))
            # Keep the per-mode PASS/FAIL line, drop the per-config noise
            # that _run_mode_config already put in the log.
            if len(self.report_lines) == before:
                self.report_lines.append(
                    "M%d %s: %s" % (entry.mode, entry.label,
                                    t("PASS") if ok else t("FAIL")))
        passing = sum(1 for ln in self.report_lines[summary_before:]
                      if ": PASS" in ln)
        failing = sum(1 for ln in self.report_lines[summary_before:]
                      if ": FAIL" in ln)
        self._log(t("== signal generator: {p} patterns pass, {f} fail ==")
                  .format(p=passing, f=failing))
        return all_ok

    def _run_mode(self, entry) -> bool:
        """Run one pattern mode at every configured channel count and rate."""
        from . import checks, patterns, waveform

        ss = self.profile.signal_source
        if not entry.ready:
            self._log(t("M{mode} is not ready: {note}").format(
                mode=entry.mode, note=entry.note or "-"))
            self.report_lines.append(
                "M%d %s: FAIL (%s)" % (entry.mode, entry.label,
                                       entry.note or t("not ready")))
            return False                      # a planned mode that cannot run is a fail

        try:
            src = self._signal_source()
        except Exception as e:                      # noqa: BLE001
            self._log(t("Signal generator unavailable: {e}").format(e=e))
            self.report_lines.append(
                "M%d %s: FAIL (%s)" % (entry.mode, entry.label,
                                       t("no signal generator")))
            return False

        try:
            src.select_mode(entry.mode)
        except Exception as e:                      # noqa: BLE001
            self._log(t("Mode {m} not confirmed: {e}").format(m=entry.mode, e=e))
            self.report_lines.append(
                "M%d %s: FAIL (%s)" % (entry.mode, entry.label, t("mode not confirmed")))
            return False

        self._log(t("== M{mode} {label} ==").format(mode=entry.mode, label=entry.label))
        configs = [c for c in entry.configs if c.link is None or c.link == self._link]
        if not configs:
            # a pass that silently tests less is exactly what must not happen
            why = t("no config for the {link} pass").format(link=_link_name(self._link))
            self.report_lines.append("M%d %s: FAIL (%s)" % (entry.mode, entry.label, why))
            return False
        all_ok = True
        for cfg in configs:
            ok = self._run_mode_config(entry, cfg, ss, checks, patterns, waveform)
            all_ok = all_ok and ok
            if self.cancel.is_set():
                break
        return all_ok

    def _run_mode_config(self, entry, cfg, ss, checks, patterns, waveform) -> bool:
        """One mode at one channel count, at the default and any group thresholds.

        Two passes when the profile declares threshold groups: the fixture mixes
        3.3 V and 1.8 V logic, and measurement showed no single input threshold
        serves both. Channels outside the groups take their verdict from the
        default pass; the grouped channels take theirs from the pass at their own
        threshold.
        """
        label = "%dch@%s" % (cfg.channels, format_rate(cfg.samplerate_hz))
        # The sample cap matters: a 20 ms window at 1400 MSa/s would ask for 28 M
        # samples (14 MB), which the 5 Gbps link cannot drain at the requested rate
        # and which buys nothing -- 8 M samples is still 5.7 ms, enough for the
        # slowest signal M0 carries. The EFFECTIVE window is what the model needs,
        # because a channel slower than it is marked unmeasurable (a fail that
        # points at the profile, not the board).
        n_samples = min(int(cfg.samplerate_hz * cfg.window_ms / 1000.0),
                        cfg.max_samples)
        n_samples = max(n_samples, 1024)
        eff_window_s = n_samples / float(cfg.samplerate_hz)
        self._log(t("-- {label} ({ms} ms window, {n} samples) --").format(
            label=label, ms=round(eff_window_s * 1000, 1), n=n_samples))

        passes = [(self.profile.voltage_threshold_v, None)]
        if entry.mode in patterns.NEEDS_THRESHOLD_GROUPS:
            for g in self.profile.threshold_groups:
                passes.append((g.voltage_threshold_v, g))

        verdicts_by_channel: dict[int, object] = {}
        extra: dict[str, object] = {}
        for vth, group in passes:
            self.cb.on_progress("M%d %s · %s @%.1fV" % (entry.mode, entry.label,
                                                        label, vth))
            ok, verdicts = self._capture_and_check(
                entry, cfg, vth, str(n_samples), eff_window_s,
                checks, patterns, waveform)
            if not ok and verdicts is None:
                self.report_lines.append("M%d %s @%.1fV: FAIL (%s)"
                                         % (entry.mode, label, vth, t("capture failed")))
                return False
            tag = "" if group is None else " [%s @%.1fV]" % (group.label, vth)
            for v in verdicts:
                if v.channel < 0:
                    # A structured verdict: one SPI master, one I2C transaction,
                    # the rotation check. These are NOT per-channel -- an SPI
                    # instance spans SCLK plus three more wires -- so they cannot
                    # be bucketed by channel number and must not be deduplicated
                    # by one.
                    #
                    # Keyed by kind, so the LAST pass wins. The passes run
                    # slowest-threshold last (default 1.5 V, then each threshold
                    # group), which means for M4 the 0.9 V pass supplies the
                    # protocol verdicts. That is the right way round: at 1.5 V the
                    # bank-3 wires (CH20..CH25) are marginal, and measured, spi5's
                    # MISO decoded 0x0F68 instead of 0x0F69 and I2C returned
                    # garbage. At 0.9 V both are exact. Keeping both passes would
                    # instead report the same instance twice, once PASS and once
                    # FAIL.
                    #
                    # `continue` 很关键：这些判定**只能**进 extra。以前它们同时被
                    # 塞进 verdicts_by_channel[-1]，于是 final 里出现两次 ——
                    # 失败计数翻倍、报告里同一条失败重复两行（实测 M0 的轮转判定
                    # 就报成 "0 ok / 2 fail"，其实只有 1 条）。
                    v.channel_text = v.channel_text or v.kind
                    # 合并规则：**哪一轮的阈值真正管着这条判定名下的那些通道，
                    # 就用哪一轮。**
                    #
                    #   默认轮（1.5 V）永远有资格；
                    #   阈值组轮只在该判定的通道落在该组里时才有资格接管。
                    #
                    # M4 的 I2C 在 CH24/CH25（bank 3，1.8 V）-> 0.9 V 那轮接管它，
                    # 正确：1.5 V 下 bank 3 是勉强电平，实测 spi5 的 MISO 解成
                    # 0x0F68（应为 0x0F69）、I2C 直接是垃圾。
                    #
                    # M5 的窄脉冲判定在 CH2~CH4（3.3 V 那侧）-> 0.9 V 那轮**不许**
                    # 接管它，否则会把一个本该在 1.5 V 下判的检查改用坏阈值来判。
                    # 以前是"最后一轮永远赢"，M0 在 0.9 V 下的轮转判定就是这么被
                    # 弄坏的（1.5 V 通过、0.9 V 失败）。
                    if group is None or (set(v.channels) & set(group.channels)):
                        extra[v.kind] = v
                    continue
                if group is None:
                    verdicts_by_channel.setdefault(v.channel, v)
                elif v.channel in group.channels:
                    v.detail = (v.detail + tag).strip()
                    verdicts_by_channel[v.channel] = v

        final = ([v for _c, v in sorted(verdicts_by_channel.items())]
                 + list(extra.values()))
        good, passed, failed = checks.summarise(final)
        by_ch = {v.channel: v for v in final}
        for v in final:
            if not v.ok:
                where = (v.channel_text if v.channel < 0
                         else "CH%d" % v.channel)
                self._log("     %-12s %-8s got %-24s want %-24s %s"
                          % (where, v.kind, v.measured, v.expected, v.detail[:60]))
                # For a group verdict, spell out which of its wires did pass, so
                # the reader is not left guessing across all of them.
                ok_in = ""
                if v.channels:
                    good_ones = [c for c in v.channels
                                 if by_ch.get(c) is not None
                                 and by_ch[c].ok is True]
                    if good_ones:
                        ok_in = ", ".join("CH%d" % c for c in good_ones)
                self.failures.append((
                    "M%d %s" % (entry.mode, entry.label), where, v.kind,
                    v.measured, v.expected, ok_in))
        self._log(t("   {label}: {verdict}  ({p} ok / {f} fail)").format(
            label=label, verdict="PASS" if good else "FAIL",
            p=passed, f=failed))
        self.report_lines.append(
            "M%d %s: %s (%d ok / %d fail)"
            % (entry.mode, label, "PASS" if good else "FAIL", passed, failed))
        return good

    def _capture_retrying(self, cfg, vth, n_samples, out_file, label,
                          attempts: int = 3):
        """Capture, retrying automatically only host-side transient failures
        (see _TRANSIENT); a retry that then succeeds is written to the report.

        `LIBUSB_ERROR_ACCESS` / "No devices found" is what the next capture sees
        after a previous sigrok-cli was killed: the old process has not released
        the USB device yet, or a stray one is still holding it. The very next
        attempt normally succeeds, but without a retry the operator sees a hard
        FAIL and -- worse -- that 120 s timeout cascades into the following
        captures. Measured on the real fixture: one M4 run failed two configs this
        way, all of which passed on a rerun.

        Real failures (a genuinely absent device, a bad samplerate) still surface:
        they fail every attempt, and the last error is what gets reported.
        """
        last = None
        why = ""
        for attempt in range(1, attempts + 1):
            if self.cancel.is_set():
                return None
            try:
                result = self.sigrok.capture(
                    driver=self.profile.driver, channels=cfg.channels,
                    samplerate_hz=cfg.samplerate_hz, samples=n_samples,
                    voltage_threshold_v=vth, out_file=out_file,
                    timeout_s=self.profile.timeouts.capture_s,
                    log_cb=self._log, cancel=self.cancel)
                if attempt > 1:
                    self.report_lines.append(
                        "   %s — " % label + t("capture retried {n}x: {why}").format(
                            n=attempt - 1, why=why))
                return result
            except CaptureError as e:
                last = e
                why = _transient_reason(e)
                if attempt < attempts and why and not self.cancel.is_set():
                    self._log(t("Capture attempt {a}/{n} failed ({e}); retrying")
                              .format(a=attempt, n=attempts, e=why))
                    self._kill_stray_sigrok()
                    time.sleep(1.5)
                    continue
                break
        self._log(t("Capture failed: {e}").format(e=last))
        self._log("  [" + label + "]")
        return None

    @staticmethod
    def _kill_stray_sigrok() -> None:
        """Clear any sigrok-cli that outlived its capture.

        `taskkill /IM sigrok-cli*.exe` matches nothing on Windows: the image name
        is truncated to 15 characters ("sigrok-cli"), and the bundled binary has a
        longer name. A stray then holds the USB device and every later capture
        fails with LIBUSB_ERROR_ACCESS -- a cascade that killed two full runs of
        this project's own matrix before the pattern was found.
        """
        kill_image("sigrok-cli-windows-x86_64.exe", "sigrok-cli.exe")

    def _capture_and_check(self, entry, cfg, vth, n_samples, eff_window_s,
                           checks, patterns, waveform):
        """Capture at one threshold and return (ok, verdicts|None)."""
        out_file = OUTPUT_DIR / ("%s_m%d_%dch_%s_%.1fV.bin"
                                 % (self.profile.id, entry.mode, cfg.channels,
                                    format_rate(cfg.samplerate_hz), vth))
        result = self._capture_retrying(
            cfg, vth, n_samples, out_file,
            "%s M%d %dch@%s %.1fV" % (self.profile.id, entry.mode, cfg.channels,
                                      format_rate(cfg.samplerate_hz), vth))
        if result is None:
            return False, None

        try:
            chans = waveform.load_capture_file(result.out_file, cfg.channels,
                                               result.unitsize)
            # Undo the harness permutation declared in the product profile, if any.
            # Identity by default, so a correctly wired fixture is unaffected.
            order = self.profile.signal_source.channel_order
            if order and list(order[:cfg.channels]) != list(range(cfg.channels)):
                chans = waveform.apply_channel_order(chans, order)
                self._log(t("Applied the profile's signal_source.channel_order "
                            "permutation to this capture"))
        except Exception as e:                      # noqa: BLE001
            self._log(t("Unpack failed: {e}").format(e=e))
            return False, None

        expects = patterns.expected(entry.mode, cfg.channels,
                                    cfg.samplerate_hz,
                                    window_s=eff_window_s)
        verdicts = checks.evaluate(entry.mode, chans, expects, cfg.samplerate_hz)
        return True, verdicts


if __name__ == "__main__":
    # engine self-test with fake steps: success path, prompt path (simulated), failure abort
    done = threading.Event()
    results = {}

    def finished(ok, report):
        results["ok"] = ok
        results["report"] = report
        done.set()

    cb = PipelineCallbacks(
        on_log=lambda s: print(f"  log: {s}"),
        on_step=lambda n, st: print(f"  step: {n} -> {st.value}"),
        on_user_prompt=lambda s: print(f"  PROMPT: {s}"),
        on_finished=finished)

    from .profiles import load_profiles
    profile = load_profiles()[0][0]

    print("== 序列计划 ==")
    all_profiles = load_profiles()[0]
    for prof in all_profiles:
        ids = [sid for sid, _ in sequence_plan(prof)]
        print(f"  {prof.id}: {ids}")
        # a board that switches modes by RECONFIG starts with the round trip
        head = ["mode:dfu-app"] if prof.reconfig else []
        assert ids[:len(head)] == head and ids[len(head)].startswith("link:check"), ids
        # one pattern run per link pass, later passes start
        # with a re-plug step
        assert sum(i.startswith("signal:all") for i in ids) == max(1, len(prof.links)), ids
        assert sum(i.startswith("link:replug") for i in ids) == max(0, len(prof.links) - 1), ids
        # the GUI list and the executed steps come from one table
        assert ids == [d.id for d in Pipeline(prof, None, cb, [])._sequence_defs()]

    print("== link:check 判据（假设备）==")
    from .device_watch import DeviceStatus, Mode
    p32 = next(x for x in all_profiles if x.min_link == 5)
    real_scan = device_watch.scan_devices

    def link_case(devs) -> bool:
        device_watch.scan_devices = lambda _profiles: devs
        try:
            pl = Pipeline(p32, None, cb, [])
            pl._all_profiles = all_profiles
            return pl._check_link(p32.min_link)
        finally:
            device_watch.scan_devices = real_scan

    app = lambda speed: DeviceStatus(p32, Mode.APP, p32.app_pid, "SN", speed=speed)
    assert link_case([app(5)]) is True                      # 10G -> ok
    assert link_case([app(4)]) is False                     # 5G < 10G -> FAIL
    assert link_case([app(5), app(5)]) is False             # two analyzers -> FAIL
    assert link_case([DeviceStatus(p32, Mode.DFU, p32.dfu_pid, None, speed=3)]) is False
    assert link_case([]) is False                           # nothing there

    print("== DFU/APP 来回切换（假设备）==")
    import dataclasses
    p32r = dataclasses.replace(p32, reconfig=True,
                               timeouts=dataclasses.replace(p32.timeouts, mode_switch_s=1))
    real_reconfig = device_watch.reconfig

    def trip_case(state: list, on_reconfig) -> bool:
        """state[0] = devices online now; on_reconfig(d) changes them."""
        device_watch.scan_devices = lambda _profiles: list(state[0])
        device_watch.reconfig = lambda d: (on_reconfig(d), "")[1]
        try:
            pl = Pipeline(p32r, None, cb, [])
            pl._all_profiles = all_profiles
            return pl._mode_round_trip()
        finally:
            device_watch.scan_devices = real_scan
            device_watch.reconfig = real_reconfig

    at = lambda mode, port: DeviceStatus(
        p32r, mode, p32r.app_pid if mode is Mode.APP else p32r.dfu_pid, "SN",
        bus=1, port_numbers=port, speed=5)

    def flip(state, serial="SN"):
        def go(d):
            # DFU comes up on another bus/port and without a serial, as measured
            if d.mode is Mode.APP:
                state[0] = [dataclasses.replace(at(Mode.DFU, (3,)), bus=2, serial=None)]
            else:
                state[0] = [dataclasses.replace(at(Mode.APP, (2,)), serial=serial)]
        return go
    st = [[at(Mode.APP, (2,))]]
    assert trip_case(st, flip(st)) is True                  # APP -> DFU -> APP
    st = [[at(Mode.APP, (2,))]]
    assert trip_case(st, lambda d: None) is False           # never leaves APP
    st = [[at(Mode.APP, (2,))]]
    assert trip_case(st, flip(st, serial="OTHER")) is False  # other firmware id back
    st = [[at(Mode.APP, (2,)), at(Mode.APP, (3,))]]
    assert trip_case(st, flip(st)) is False                 # two boards

    print("== 假步骤成功路径 ==")
    p = Pipeline(profile, None, cb, [
        StepDef("a", "A", lambda: True), StepDef("b", "B", lambda: True)])
    p.start(); done.wait(5)
    assert results["ok"] is True

    print("== 中途失败应中止 ==")
    done.clear()
    ran = []
    p = Pipeline(profile, None, cb, [
        StepDef("a", "A", lambda: (ran.append("a"), True)[1]),
        StepDef("boom", "Boom", lambda: False),
        StepDef("never", "Never", lambda: (ran.append("never"), True)[1])])
    p.start(); done.wait(5)
    assert results["ok"] is False and ran == ["a"], ran

    print("== capture 步骤失败不中止后续 capture ==")
    done.clear()
    ran2 = []
    p = Pipeline(profile, None, cb, [
        StepDef("capture:0", "C0", lambda: False, abort_on_fail=False),
        StepDef("capture:1", "C1", lambda: (ran2.append("c1"), True)[1],
                abort_on_fail=False)])
    p.start(); done.wait(5)
    assert results["ok"] is False and ran2 == ["c1"], ran2

    print("== 取消应立刻结束换口等待，并给出失败原因 ==")
    done.clear()
    notes = {}
    cb_note = PipelineCallbacks(on_log=cb.on_log, on_step=cb.on_step,
                                on_finished=finished,
                                on_step_note=lambda sid, n: notes.update({sid: n}))
    device_watch.scan_devices = lambda _profiles: []          # nobody plugged in
    try:
        p2 = Pipeline(p32, None, cb_note, [])
        p2.steps = [StepDef("link:replug@5G", "Replug", lambda: p2._await_link(4))]
        threading.Timer(1.0, p2.request_cancel).start()
        t0 = time.time()
        p2.start(); done.wait(15)
        elapsed = time.time() - t0
    finally:
        device_watch.scan_devices = real_scan
    assert results["ok"] is False and elapsed < 4, elapsed
    assert notes.get("link:replug@5G") == t("Cancelled"), notes
    print(f"  (等待+取消耗时 {elapsed:.1f}s, note={notes['link:replug@5G']!r})")
    print("== 采集重试：主机侧偶发自动重试，分析仪侧 stall 不重试 ==")

    class FakeSigrok:
        def __init__(self, failures):
            self.failures, self.calls = list(failures), 0

        def capture(self, **_kw):
            self.calls += 1
            if self.failures:
                raise CaptureError("sigrok-cli exit code 1", output=self.failures.pop(0))
            return "result"

    cfg = next(c for e in p32.plan for c in e.configs)
    pr = Pipeline(p32, FakeSigrok(["No devices found."]), cb, [])
    pr._kill_stray_sigrok = lambda: None
    real_sleep, time.sleep = time.sleep, (lambda _s: None)
    try:
        got = pr._capture_retrying(cfg, 1.5, "1000", OUTPUT_DIR / "x.bin", "M0 test")
        assert got == "result" and pr.sigrok.calls == 2, (got, pr.sigrok.calls)
        assert any("No devices found" in ln for ln in pr.report_lines), pr.report_lines
        ps = Pipeline(p32, FakeSigrok([
            "sr: sipeed-slogic-analyzer: USB stream did not start; re-arming.\n"
            "sr: sipeed-slogic-analyzer: acquisition stall: aborting capture."]), cb, [])
        assert ps._capture_retrying(cfg, 1.5, "1000", OUTPUT_DIR / "x.bin", "M0") is None
        assert ps.sigrok.calls == 1 and not ps.report_lines, ps.sigrok.calls
    finally:
        time.sleep = real_sleep
    print("pipeline 引擎自测 PASS")
