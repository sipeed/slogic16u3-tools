"""Test-station state machine, separate from the window that shows it.

START arms the station: every newly connected board is recognised and run
automatically; the verdict stays up until the next board arrives.  STOP
disarms (and cancels a run).  While disarmed, single steps run on the last
recognised board.

    idle -> wait_insert -> running -> wait_remove -> wait_insert -> ...

The USB analyzers are polled (one board at a time: sigrok-cli cannot choose
between several analyzers).

Pipeline callbacks arrive on worker threads; they are re-emitted as Qt
signals, so receivers in the GUI thread get them queued.
"""
from __future__ import annotations

import threading
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .. import device_watch, env_setup, fpga
from .. import pipeline as pipeline_mod
from ..device_watch import Mode
from ..i18n import t
from ..profiles import ProductProfile


class StationController(QObject):
    log = pyqtSignal(str)
    step = pyqtSignal(str, object)               # step id, StepStatus
    step_note = pyqtSignal(str, str)             # step id, why it failed
    progress = pyqtSignal(str)                   # what the run is doing now
    mode_result = pyqtSignal(str, int, bool, str)  # link, mode, ok, reason
    prompt = pyqtSignal(str)
    prompt_clear = pyqtSignal()
    run_started = pyqtSignal(bool)              # True = whole-board run
    run_finished = pyqtSignal(bool, str, float)  # ok, detail report, elapsed s
    devices_changed = pyqtSignal()               # detected / last_detect updated
    state_changed = pyqtSignal()                 # armed / busy / arm_state
    notice = pyqtSignal(str)                     # armed but cannot start (why)
    board_removed = pyqtSignal()
    env_done = pyqtSignal(bool, str)             # ok, action
    reset_done = pyqtSignal()
    env_status_changed = pyqtSignal()            # env_status updated
    _pl_finished = pyqtSignal(bool, str)
    _scanned = pyqtSignal(bool, object, object)  # backend ok, fixture ports, devices

    def __init__(self, profiles: list[ProductProfile], sigrok, parent=None):
        super().__init__(parent)
        self.profiles = profiles
        self.sigrok = sigrok
        self.pipeline: pipeline_mod.Pipeline | None = None
        self.detected: list[device_watch.DeviceStatus] = []
        self.backend_ok = True
        self.fixture_present = False
        self.fixture_port_list: list[str] = []
        self.sigrok_version = ""                 # filled in by a background probe
        self.resetting = False                   # hardware reset in progress
        # udev rule / WinUSB binding: 'deployed' | 'absent' | 'unknown'.  Read
        # off the GUI thread (Windows asks PowerShell, which takes seconds)
        self.env_status = "unknown"
        self._scanning = False                   # a device poll is in flight
        self._scan_again = False                 # ...and another was asked for
        if sigrok is not None:
            threading.Thread(target=self._probe_sigrok, daemon=True).start()
        self.profile: ProductProfile | None = None   # last recognised board
        self.armed = False
        self.arm_state = "idle"
        self._t0: float | None = None
        self._pl_finished.connect(self._on_pipeline_finished)
        self.reset_done.connect(self._on_reset_done)
        self._scanned.connect(self._apply_scan)
        self.refresh_env_status()

        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(self.refresh_devices)
        self._device_timer.start(1500)

    def _probe_sigrok(self):
        """sigrok-cli's own version line, for the readiness checklist."""
        try:
            out = self.sigrok._run_lines(["--version"], 20)
        except Exception:                                   # noqa: BLE001
            out = []
        self.sigrok_version = next((ln.strip() for ln in out if ln.strip()), "?")

    # ------------------------------------------------------------- queries

    @property
    def running(self) -> bool:
        """A pipeline (test run, retry, single step) is running."""
        return self.pipeline is not None and self.pipeline.running

    @property
    def busy(self) -> bool:
        """Running, or resetting the hardware: nothing new may start."""
        return self.running or self.resetting

    @property
    def idle(self) -> bool:
        return not self.busy and not self.armed

    def missing(self) -> list[str]:
        """What the station still lacks before it can be armed."""
        return [] if self.sigrok is not None else ["sigrok-cli"]

    def preview_profile(self) -> ProductProfile | None:
        """Product whose steps to show while no board is recognised yet: the
        analyzer online; None when nothing is connected -- then no steps are
        shown at all."""
        return self.detected[0].profile if self.detected else None

    def test_link_ok(self) -> bool:
        d = self.detected
        return (len(d) == 1 and d[0].mode == Mode.APP
                and (d[0].profile.min_link or 0) <= (d[0].speed or 0))

    # -------------------------------------------------------------- arming

    def arm(self):
        if self.armed or self.missing():
            return
        self.armed = True
        self.arm_state = "wait_insert"
        self.log.emit("===== " + t("Armed") + " =====")
        self.state_changed.emit()
        self.refresh_devices()

    def disarm(self):
        if self.busy:
            self.cancel()
        if self.armed:
            self.log.emit(t("Disarmed"))
        self.armed = False
        self.arm_state = "idle"
        self.state_changed.emit()

    def cancel(self):
        if self.pipeline is not None:
            self.pipeline.request_cancel()
            self.log.emit(t("Stop requested…"))

    def _run_board(self, p: ProductProfile):
        """A new board was recognised while armed: run the whole test."""
        self.profile = p
        self.arm_state = "running"
        self.log.emit("===== " + p.display_name + " =====")
        self._start(pipeline_mod.Pipeline.for_board(
            p, self.sigrok, self._callbacks(), all_profiles=self.profiles), full=True)

    def _board_gone(self):
        self.arm_state = "wait_insert"
        self.log.emit(t("Board removed; waiting for the next board…"))
        self.board_removed.emit()

    # ------------------------------------------------------------- polling

    def _fixture_usb(self) -> fpga.FixtureUsb:
        """USB identity for the fixture poll (resources/fixture.toml, via the
        loaded product profiles; both products share it)."""
        return (self.profiles[0].signal_source.usb if self.profiles
                else fpga.FixtureUsb())

    def refresh_devices(self):
        """Poll the USB analyzers and the fixture.  The scan runs on a worker
        thread -- it repeats every 1.5 s, and pyusb plus the serial-port
        enumeration are slow enough on Windows to stall the window -- and its
        result is applied on the GUI thread (_apply_scan), which also drives
        the armed state machine (a new board starts a run, its removal
        re-arms)."""
        if self._scanning:
            self._scan_again = True
            return
        self._scanning = True

        def worker():
            ok, ports, found = self.backend_ok, self.fixture_port_list, self.detected
            try:
                ok = device_watch.backend_ok()
                ports = fpga.fixture_ports(self._fixture_usb())
                found = device_watch.scan_devices(self.profiles) if ok else []
            except Exception as e:                          # noqa: BLE001
                self.log.emit(f"[poll] {e}")
            self._scanned.emit(ok, ports, found)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_scan(self, ok: bool, ports: list, found: list):
        self._scanning = False
        plugged_in = bool(ports) and not self.fixture_present
        self.backend_ok = ok
        self.fixture_port_list = ports
        self.fixture_present = bool(ports)
        self.detected = found
        self.devices_changed.emit()
        if ok:
            self._arm_tick()
        if plugged_in and env_setup.requirement() == "winusb":
            self.refresh_env_status()     # the binding is per device: read it again
        if self._scan_again:
            self._scan_again = False
            self.refresh_devices()

    def refresh_env_status(self):
        """Read env_status on a worker thread: at start-up, when the fixture
        appears, and after a deploy/remove -- never per poll."""
        if not env_setup.requirement():
            return

        def worker():
            try:
                st = env_setup.status()
            except Exception:                               # noqa: BLE001
                st = "unknown"
            self.env_status = st
            self.env_status_changed.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _arm_tick(self):
        if not self.armed or self.busy:
            return
        present = self.detected
        if self.arm_state == "wait_remove":
            if not present:
                self._board_gone()
            return
        if self.arm_state != "wait_insert" or not present:
            return
        if len(present) > 1:
            # sigrok-cli would capture from any of them; never guess
            self.notice.emit(t("{n} analyzers online; keep only the board "
                               "under test connected").format(n=len(present)))
            return
        self._run_board(present[0].profile)

    # ------------------------------------------------------------- running

    def _callbacks(self) -> pipeline_mod.PipelineCallbacks:
        return pipeline_mod.PipelineCallbacks(
            on_log=self.log.emit,
            on_step=lambda sid, st: self.step.emit(sid, st),
            on_step_note=lambda sid, note: self.step_note.emit(sid, note),
            on_progress=self.progress.emit,
            on_mode_result=lambda link, mode, ok, note: self.mode_result.emit(
                link, mode, ok, note),
            on_user_prompt=self.prompt.emit,
            on_user_prompt_clear=self.prompt_clear.emit,
            on_finished=lambda ok, rep: self._pl_finished.emit(ok, rep))

    def _start(self, pl: pipeline_mod.Pipeline, full: bool = False):
        if self.busy:
            return
        self.pipeline = pl
        self._t0 = time.time()
        self.run_started.emit(full)
        pl.start()
        self.state_changed.emit()

    def run_single_step(self, step_id: str, label: str):
        if self.profile is None or not self.idle:
            return
        self.log.emit("===== " + t("Single step: {label}").format(label=label) + " =====")
        self._start(pipeline_mod.Pipeline.single_step(
            self.profile, self.sigrok, self._callbacks(), step_id,
            all_profiles=self.profiles))

    def can_retry(self) -> bool:
        """A failed step may be rerun by hand when nothing is running and the
        board it failed on is still the one in front of us (disarmed, or
        armed and waiting for that board to be removed)."""
        return (self.profile is not None and not self.busy
                and (not self.armed or self.arm_state == "wait_remove"))

    def retry_steps(self, step_ids: list[str], label: str):
        """Operator-requested retry: rerun these steps of the board's own
        sequence, in order.  Never started by the tool itself."""
        if not step_ids or not self.can_retry():
            return
        pl = pipeline_mod.Pipeline.for_board(
            self.profile, self.sigrok, self._callbacks(), all_profiles=self.profiles)
        pl.steps = [s for s in pl.steps if s.id in step_ids]
        if not pl.steps:
            return
        self.log.emit("===== " + t("Retry: {label}").format(label=label) + " =====")
        self._start(pl)

    def _on_pipeline_finished(self, ok: bool, report: str):
        elapsed = time.time() - self._t0 if self._t0 else 0.0
        self.pipeline = None
        if self.armed:
            # the verdict stays up until the next board arrives
            self.arm_state = "wait_remove"
        self.run_finished.emit(ok, report, elapsed)
        self.state_changed.emit()

    # ------------------------------------------------------------- reset

    def reset_hardware(self):
        """Operator Reset: put the bench back in a known state, off the UI
        thread -- any stray sigrok-cli killed (Windows), the fixture reopened
        and back in M0 (its bitstream reloaded into SRAM if it does not
        answer), then the devices rescanned.  Each outcome goes to the
        log; reset_done fires at the end.  Armed or not stays as it was; a
        board still plugged in is not started (the arm state is left waiting
        for its removal).

        The analyzer is deliberately NOT given a USB port reset: tried on the
        bench (SLogic32U3 on a 10G link), it dropped off the bus and did not
        come back until it was replugged."""
        if self.busy:
            return
        self.resetting = True
        self.state_changed.emit()
        ss_cfg = self.profiles[0].signal_source if self.profiles else None
        self.log.emit("===== " + t("Reset") + " =====")

        def worker():
            from ..procutil import kill_image
            kill_image("sigrok-cli-windows-x86_64.exe", "sigrok-cli.exe")
            if ss_cfg is not None:
                try:
                    from .. import fixture
                    src = fixture.ensure_running(ss_cfg, self.log.emit)
                    port = src.port
                    src.close()
                    self.log.emit("[reset] " + t("Fixture on {p} answered, back in M0")
                                  .format(p=port))
                except Exception as e:             # noqa: BLE001
                    self.log.emit("[reset] " + t("Fixture reset failed: {e}").format(e=e))
            self.reset_done.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _on_reset_done(self):
        self.resetting = False
        self.refresh_devices()
        self.state_changed.emit()

    # --------------------------------------------------- environment prep

    def run_env_setup(self, action: str):
        """Deploy/remove the platform prerequisite (udev / WinUSB) off the UI
        thread; both operations pop a system auth dialog (pkexec / UAC)."""
        # env logs stay ASCII/English on purpose: they interleave with output
        # from Windows tools (pnputil/wdi) that we can't force into one locale.
        self.log.emit(f"===== env setup: {action} ({env_setup.requirement()}) =====")

        def worker():
            try:
                fn = env_setup.deploy if action == "deploy" else env_setup.remove
                ok = fn(self.log.emit)
            except Exception as e:  # never let the worker die silently
                self.log.emit(f"[env] exception: {e}")
                ok = False
            self.log.emit(f"env setup {action}: {'OK' if ok else 'FAILED'}")
            try:
                self.env_status = env_setup.status()
            except Exception:                               # noqa: BLE001
                self.env_status = "unknown"
            self.env_done.emit(ok, action)
            self.env_status_changed.emit()

        threading.Thread(target=worker, daemon=True).start()
