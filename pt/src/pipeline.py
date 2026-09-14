"""Production-test pipeline engine.

Full flow:  blank_flash (in_pipeline steps) -> wait OTA device ->
flash app firmware (+verify) -> wait APP device -> capture+verify each
configured test point -> PASS/FAIL summary.

Mode-switch policy (per user decision): after each flashing stage, wait for
the target PID; on timeout, raise a non-blocking operator prompt ("please
replug") and KEEP polling until the device shows up or the run is cancelled.
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import blank_flash as blank_flash_mod
import device_watch
import flasher
import waveform
from profiles import OUTPUT_DIR, ProductProfile, format_rate
from sigrok import CaptureError, SigrokCli


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PipelineCallbacks:
    on_log: Callable[[str], None] = lambda s: None
    on_step: Callable[[str, StepStatus], None] = lambda name, st: None
    on_user_prompt: Callable[[str], None] = lambda s: None
    on_user_prompt_clear: Callable[[], None] = lambda: None
    on_finished: Callable[[bool, str], None] = lambda ok, report: None


@dataclass
class StepDef:
    id: str                     # stable id, GUI step rows key on this
    label: str                  # operator-facing name
    run: Callable[[], bool]     # True = passed
    abort_on_fail: bool = True  # capture steps keep going to aid diagnosis


class PipelineAbort(Exception):
    pass


def sequence_plan(profile: ProductProfile) -> list[tuple[str, str]]:
    """(id, label) of the linear production sequence, without a Pipeline
    instance -- the GUI builds its step list from this so ids always match."""
    plan: list[tuple[str, str]] = []
    for s in (profile.blank_flash_steps or []):
        if s.in_pipeline:
            plan.append((f"blank:{s.name}", f"烧空板 · {s.label}"))
    plan.append(("wait_ota", "等待 OTA 设备"))
    plan.append(("flash_app", "OTA 烧写应用固件"))
    plan.append(("wait_app", "等待 APP 设备"))
    for i, t in enumerate(profile.capture_tests):
        plan.append((f"capture:{i}",
                     f"采样验证 {t.channels}ch@{format_rate(t.samplerate_hz)}"))
    return plan


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

    # ---- construction helpers -------------------------------------------

    def _sequence_defs(self) -> list[StepDef]:
        defs: list[StepDef] = []
        for s in (self.profile.blank_flash_steps or []):
            if s.in_pipeline:
                defs.append(StepDef(f"blank:{s.name}", f"烧空板 · {s.label}",
                                    lambda s=s: self._run_blank_step(s)))
        defs.append(StepDef("wait_ota", "等待 OTA 设备", self._wait_ota))
        defs.append(StepDef("flash_app", "OTA 烧写应用固件", self._flash_app))
        defs.append(StepDef("wait_app", "等待 APP 设备", self._wait_app))
        for i, t in enumerate(self.profile.capture_tests):
            defs.append(StepDef(
                f"capture:{i}",
                f"采样验证 {t.channels}ch@{format_rate(t.samplerate_hz)}",
                lambda t=t: self._capture_one(
                    t.channels, t.samplerate_hz, t.samples,
                    self.profile.voltage_threshold_v, None),
                abort_on_fail=False))
        return defs

    @classmethod
    def full_test(cls, profile: ProductProfile, sigrok: SigrokCli,
                  callbacks: PipelineCallbacks,
                  firmware_path=None) -> "Pipeline":
        p = cls(profile, sigrok, callbacks, [])
        p._firmware_override = firmware_path
        p.steps = p._sequence_defs()
        return p

    @classmethod
    def single_step(cls, profile: ProductProfile, sigrok: SigrokCli | None,
                    callbacks: PipelineCallbacks, step_id: str,
                    firmware_path=None) -> "Pipeline":
        """Manual mode: run exactly one sequence step by id."""
        p = cls(profile, sigrok, callbacks, [])
        p._firmware_override = firmware_path
        matches = [d for d in p._sequence_defs() if d.id == step_id]
        if not matches:
            raise ValueError(f"未知步骤: {step_id}")
        p.steps = matches
        return p

    @classmethod
    def manifest_step(cls, profile: ProductProfile,
                      callbacks: PipelineCallbacks, step) -> "Pipeline":
        """Any manifest step (also non-pipeline ones, e.g. eFuse lock)."""
        p = cls(profile, None, callbacks, [])
        p.steps = [StepDef(f"blank:{step.name}", step.label,
                           lambda: p._run_blank_step(step))]
        return p

    @classmethod
    def capture_only(cls, profile: ProductProfile, sigrok: SigrokCli,
                     callbacks: PipelineCallbacks, *,
                     channels: int, samplerate_hz: int, samples: str,
                     voltage_threshold_v: float,
                     expected_rows: list[tuple[float, float]] | None = None
                     ) -> "Pipeline":
        """Custom capture+verify with operator-tweaked parameters."""
        p = cls(profile, sigrok, callbacks, [])
        label = f"自定义采样 {channels}ch@{format_rate(samplerate_hz)}"
        p.steps = [StepDef("capture:custom", label, lambda: p._capture_one(
            channels, samplerate_hz, samples, voltage_threshold_v, expected_rows),
            abort_on_fail=False)]
        return p

    @classmethod
    def reflash(cls, profile: ProductProfile, callbacks: PipelineCallbacks,
                firmware_path=None) -> "Pipeline":
        """返修复烧：等待设备进入 OTA 模式（超时提示人工操作）-> 重写应用
        固件 -> 等待应用模式回归。"""
        p = cls(profile, None, callbacks, [])
        p._firmware_override = firmware_path
        p.steps = [StepDef("wait_ota", "等待 OTA 设备", p._wait_ota),
                   StepDef("flash_app", "OTA 烧写应用固件", p._flash_app),
                   StepDef("wait_app", "等待 APP 设备", p._wait_app)]
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
                    raise PipelineAbort("已取消")
                ok = step.run()
                self.cb.on_step(step.id,
                                StepStatus.PASSED if ok else StepStatus.FAILED)
                if not ok:
                    all_pass = False
                    if step.abort_on_fail:
                        raise PipelineAbort(f"步骤「{step.label}」失败")
        except PipelineAbort as e:
            all_pass = False
            self._log(f"流程中止: {e}")
            self.report_lines.append(f"流程中止: {e}")
        except Exception:
            all_pass = False
            self._log("流程异常:\n" + traceback.format_exc())
            self.report_lines.append("流程异常，详见日志")
        finally:
            self.cb.on_user_prompt_clear()
            self.cb.on_finished(all_pass, "\n".join(self.report_lines))

    # ---- steps -----------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.cb.on_log(msg)

    def _run_blank_step(self, step) -> bool:
        return blank_flash_mod.run_step(
            step, self.profile.blank_flash_dir, self._log, self.cancel)

    def _wait_mode(self, pid: int, timeout_s: float, what: str) -> bool:
        self._log(f"等待 {what} 设备 ({self.profile.vid:#06x}:{pid:#06x})...")
        if device_watch.wait_for_pid(self.profile.vid, pid, timeout_s,
                                     cancel=self.cancel):
            self._log(f"{what} 设备已就绪")
            return True
        if self.cancel.is_set():
            return False
        # timeout -> prompt operator, keep polling indefinitely until cancel
        self.cb.on_user_prompt(
            f"等待 {what} 设备超时，请重新插拔/上电设备…  "
            f"(Waiting for {what} device timed out -- please replug)")
        self.cb.on_step(f"wait_{what.lower()}", StepStatus.WAITING_USER)
        try:
            while not self.cancel.is_set():
                if device_watch.find_pid(self.profile.vid, pid):
                    self._log(f"{what} 设备已就绪（人工介入后）")
                    return True
                time.sleep(0.5)
            return False
        finally:
            self.cb.on_user_prompt_clear()

    def _wait_ota(self) -> bool:
        if self.profile.ota_pid is None:
            self._log(f"{self.profile.display_name}: ota_pid 未配置")
            return False
        return self._wait_mode(self.profile.ota_pid,
                               self.profile.timeouts.wait_ota_device_s, "OTA")

    def _wait_app(self) -> bool:
        return self._wait_mode(self.profile.app_pid,
                               self.profile.timeouts.wait_app_device_s, "APP")

    _firmware_override = None

    def _flash_app(self) -> bool:
        fw = self._firmware_override or self.profile.app_firmware
        if fw is None or self.profile.ota_pid is None:
            self._log("固件或 ota_pid 未配置，无法烧写")
            return False
        try:
            flasher.flash_app_firmware(
                vid=self.profile.vid, pid=self.profile.ota_pid,
                addr=self.profile.app_flash_addr, firmware=fw,
                verify=self.profile.verify_after_flash,
                log_cb=self._log, cancel=self.cancel)
            self.report_lines.append(f"OTA 烧写: OK ({fw})")
            return True
        except flasher.FlashError as e:
            self._log(str(e))
            self.report_lines.append(f"OTA 烧写: FAIL ({e})")
            return False

    def _capture_all(self) -> bool:
        ok_all = True
        for t in self.profile.capture_tests:
            ok = self._capture_one(t.channels, t.samplerate_hz, t.samples,
                                   self.profile.voltage_threshold_v, None)
            ok_all = ok_all and ok
            if self.cancel.is_set():
                return False
        return ok_all

    def _capture_one(self, channels: int, samplerate_hz: int, samples: str,
                     voltage_threshold_v: float,
                     expected_rows: list[tuple[float, float]] | None) -> bool:
        assert self.sigrok is not None
        label = f"{channels}ch@{format_rate(samplerate_hz)}"
        out_file = OUTPUT_DIR / f"{self.profile.id}_{channels}ch_{format_rate(samplerate_hz)}_wave.bin"
        self._log(f"== 采样 {label} ({samples} samples) ==")
        # sigrok-cli refuses to capture when its scan matches several
        # devices, and this driver build does not implement the `conn`
        # selector yet ("Not supported now!") -- so detect the ambiguity
        # up front and tell the operator exactly what to do.
        found = self.sigrok.scan(self.profile.driver)
        if len(found) > 1:
            msg = ("检测到多台 SLogic 设备同时在线，当前 sigrok 驱动暂不支持指定设备，"
                   "请只保留被测设备后重试:\n  " + "\n  ".join(found))
            self._log(msg)
            self.report_lines.append(f"{label}: FAIL (多设备歧义)")
            return False
        pattern = self.profile.patterns.get(channels)
        if pattern:
            self._log(f"切换分组 pattern={pattern}")
        try:
            result = self.sigrok.capture(
                driver=self.profile.driver, channels=channels,
                samplerate_hz=samplerate_hz, samples=samples,
                voltage_threshold_v=voltage_threshold_v,
                device_unitsize=self.profile.unitsize, out_file=out_file,
                timeout_s=self.profile.timeouts.capture_s, pattern=pattern,
                log_cb=self._log, cancel=self.cancel)
        except CaptureError as e:
            self._log(f"采样失败: {e}")
            self.report_lines.append(f"{label}: FAIL (采样失败: {e})")
            return False

        self._log(f"采样完成: {result.n_samples} samples, {result.elapsed_s:.2f}s -> {result.out_file.name}")
        chans = waveform.load_capture_file(result.out_file, channels, self.profile.unitsize)
        e = self.profile.expected
        if expected_rows is None:
            ok, verdicts = waveform.verify_channels(
                chans, samplerate_hz, e.freq_hz, e.duty_pct,
                e.freq_tol_pct, e.duty_tol_pp)
        else:
            verdicts = []
            for ch in range(channels):
                exp_f, exp_d = expected_rows[ch]
                _, v = waveform.verify_channels(
                    chans[ch:ch + 1], samplerate_hz, exp_f, exp_d,
                    e.freq_tol_pct, e.duty_tol_pp)
                verdicts.append(waveform.ChannelVerdict(
                    ch, v[0].freq_hz, v[0].duty, v[0].freq_ok, v[0].duty_ok))
            ok = all(v.ok for v in verdicts)
        for v in verdicts:
            f = f"{v.freq_hz / 1e6:.4f}MHz" if v.freq_hz else "N/A"
            d = f"{v.duty * 100:.2f}%" if v.duty is not None else "N/A"
            self._log(f"  CH{v.channel}: freq={f} duty={d} {'ok' if v.ok else '** FAIL **'}")
        self.report_lines.append(f"{label}: {'PASS' if ok else 'FAIL'} "
                                 f"({sum(v.ok for v in verdicts)}/{len(verdicts)} 通道通过)")
        return ok


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

    from profiles import load_profiles
    profile = load_profiles()[0][0]

    print("== 序列计划 ==")
    for sid, label in sequence_plan(profile):
        print(f"  {sid}: {label}")

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

    print("== 取消应立刻结束等待 ==")
    done.clear()
    p2 = Pipeline(profile, None, cb, [])
    p2.steps = [StepDef("wait", "Wait", lambda: p2._wait_mode(0x7FFF, 1.0, "OTA"))]
    threading.Timer(2.5, p2.request_cancel).start()
    t0 = time.time()
    p2.start(); done.wait(15)
    elapsed = time.time() - t0
    assert results["ok"] is False and elapsed < 6, elapsed
    print(f"  (等待+提示+取消耗时 {elapsed:.1f}s)")
    print("pipeline 引擎自测 PASS")
