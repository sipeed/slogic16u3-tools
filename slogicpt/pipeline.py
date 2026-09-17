"""Production-test pipeline engine.

Full flow:  eFuse ensure (write+lock the AES key if unlocked -- an encrypted
DFU bitstream cannot boot without it) -> blank-flash the DFU image ->
wait DFU device -> flash app firmware (+verify) -> wait APP device ->
capture+verify each configured test point -> PASS/FAIL summary.

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

from . import device_watch
from . import flasher
from . import mode_switch as mode_switch_mod
from . import programmer as programmer_mod
from . import waveform
from .profiles import OUTPUT_DIR, ProductProfile, format_rate
from .sigrok import CaptureError, SigrokCli


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
    # 模式切换只剩人工方案（按板载 MODE 键）时的弹窗提示（GUI 弹 QMessageBox）
    on_manual_switch: Callable[[str], None] = lambda s: None
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
    prog = profile.programmer
    if prog is not None and prog.flash is not None:
        # 加密 DFU 位流须先把 AES 密钥写入 eFuse 才能启动（lock 防密钥读出），
        # 故 eFuse 写锁是烧空板的前置步骤：未锁 -> 写入并锁定；已锁 -> 跳过。
        if prog.efuse_key_file is not None:
            plan.append(("blank:efuse", "烧空板 · eFuse 密钥写锁"))
        plan.append(("blank:flash", "烧空板 · 烧写 DFU 镜像"))
    plan.append(("wait_dfu", "等待 DFU 设备"))
    plan.append(("flash_app", "DFU 烧写应用固件"))
    plan.append(("switch_app", "切换到 APP 模式"))
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
        self.app_serial: str | None = None   # APP USB SerialNumber (version id)

    # ---- construction helpers -------------------------------------------

    def _sequence_defs(self) -> list[StepDef]:
        defs: list[StepDef] = []
        prog = self.profile.programmer
        if prog is not None and prog.flash is not None:
            if prog.efuse_key_file is not None:
                defs.append(StepDef("blank:efuse", "烧空板 · eFuse 密钥写锁",
                                    self._efuse_ensure))
            defs.append(StepDef("blank:flash", "烧空板 · 烧写 DFU 镜像",
                                self._flash_blank))
        defs.append(StepDef("wait_dfu", "等待 DFU 设备", self._wait_dfu))
        defs.append(StepDef("flash_app", "DFU 烧写应用固件", self._flash_app))
        defs.append(StepDef("switch_app", "切换到 APP 模式", self._switch_to_app))
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
                  firmware_path=None, cable_index: int | None = None,
                  image_override=None, efuse_key_override=None) -> "Pipeline":
        p = cls(profile, sigrok, callbacks, [])
        p._firmware_override = firmware_path
        p._cable_index = cable_index
        p._image_override = image_override
        p._efuse_key_override = efuse_key_override
        p.steps = p._sequence_defs()
        return p

    @classmethod
    def single_step(cls, profile: ProductProfile, sigrok: SigrokCli | None,
                    callbacks: PipelineCallbacks, step_id: str,
                    firmware_path=None, cable_index: int | None = None,
                    image_override=None, efuse_key_override=None) -> "Pipeline":
        """Manual mode: run exactly one sequence step by id."""
        p = cls(profile, sigrok, callbacks, [])
        p._firmware_override = firmware_path
        p._cable_index = cable_index
        p._image_override = image_override
        p._efuse_key_override = efuse_key_override
        matches = [d for d in p._sequence_defs() if d.id == step_id]
        if not matches:
            raise ValueError(f"未知步骤: {step_id}")
        p.steps = matches
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
                firmware_path=None, cable_index: int | None = None) -> "Pipeline":
        """返修复烧：（若设备在 APP 模式则）切回 DFU -> 等待 DFU 设备（超时提示
        人工操作）-> 重写应用固件 -> 切换到 APP -> 等待应用模式回归。"""
        p = cls(profile, None, callbacks, [])
        p._firmware_override = firmware_path
        p._cable_index = cable_index
        p.steps = [StepDef("switch_dfu", "切换到 DFU 模式", p._switch_to_dfu),
                   StepDef("wait_dfu", "等待 DFU 设备", p._wait_dfu),
                   StepDef("flash_app", "DFU 烧写应用固件", p._flash_app),
                   StepDef("switch_app", "切换到 APP 模式", p._switch_to_app),
                   StepDef("wait_app", "等待 APP 设备", p._wait_app)]
        return p

    @classmethod
    def switch_mode(cls, profile: ProductProfile,
                    callbacks: PipelineCallbacks, *, to_mode: str,
                    cable_index: int | None = None) -> "Pipeline":
        """辅助操作：在 DFU（烧录模式）与 APP（应用模式）之间手动切换当前
        设备，切换后等待目标模式就绪。to_mode='app' 即 DFU->APP，'dfu' 即
        APP->DFU（方向由 GUI 依当前设备模式决定）。"""
        p = cls(profile, None, callbacks, [])
        p._cable_index = cable_index
        if to_mode == "app":
            p.steps = [StepDef("switch_app", "切换到 APP 模式", p._switch_to_app),
                       StepDef("wait_app", "等待 APP 设备", p._wait_app)]
        else:
            p.steps = [StepDef("switch_dfu", "切换到 DFU 模式", p._switch_to_dfu),
                       StepDef("wait_dfu", "等待 DFU 设备", p._wait_dfu)]
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
            lines = list(self.report_lines)
            if self.app_serial:
                lines.insert(0, f"APP SN: {self.app_serial}")
            self.cb.on_finished(all_pass, "\n".join(lines))

    # ---- steps -----------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.cb.on_log(msg)

    _cable_index = None         # external-programmer cable index (from the GUI probe)
    _image_override = None      # GUI 会话级：覆盖 DFU 镜像路径
    _efuse_key_override = None  # GUI 会话级：覆盖 eFuse 密钥路径

    def _efuse_ensure(self) -> bool:
        """烧空板前置：加密 DFU 位流须以 eFuse 中的 AES 密钥启动（write），
        lock 防止密钥被读出。已锁 -> 密钥已在，跳过；未锁 -> 写入并锁定
        （不可逆），随后回读锁定位校验。"""
        prog = self.profile.programmer
        state, cable = programmer_mod.efuse_state(
            prog, self._cable_index, log_cb=self._log, cancel=self.cancel)
        if cable is not None:
            self._cable_index = cable   # 后续烧写步骤复用同一 cable
        if state == "locked":
            self._log("eFuse 已锁定（密钥已写入），跳过写锁")
            self.report_lines.append("eFuse 写锁: SKIP（已锁定）")
            return True
        if state != "unlocked":
            self._log("无法确认 eFuse 状态（外置烧录器未连接/线缆异常？），中止")
            self.report_lines.append("eFuse 写锁: FAIL（状态未知）")
            return False
        self._log("eFuse 未锁：写入 AES 密钥并锁定（加密 DFU 启动前提，不可逆）…")
        if not programmer_mod.efuse_lock(
                prog, self._cable_index, key_file=self._efuse_key_override,
                log_cb=self._log, cancel=self.cancel):
            self.report_lines.append("eFuse 写锁: FAIL（写入失败）")
            return False
        # 回读校验：写锁后 --keyread 应报 Device Locked
        state2, _ = programmer_mod.efuse_state(
            prog, self._cable_index, log_cb=self._log, cancel=self.cancel)
        ok = state2 == "locked"
        self.report_lines.append(
            "eFuse 写锁: " + ("OK（回读确认已锁定）" if ok else "FAIL（回读未确认锁定）"))
        if not ok:
            self._log("回读 eFuse 未确认锁定，判定失败")
        return ok

    def _flash_blank(self) -> bool:
        op = self.profile.programmer.flash
        img = self._image_override if self._image_override is not None else op.image
        ok = programmer_mod.flash(
            self.profile.programmer, self._cable_index, image=self._image_override,
            log_cb=self._log, cancel=self.cancel)
        self.report_lines.append(
            f"烧空板 DFU 镜像: {'OK' if ok else 'FAIL'} "
            f"({img.name} @ {op.spiaddr:#x})")
        return ok

    def _wait_mode(self, pid: int, timeout_s: float, what: str) -> bool:
        self._log(f"等待 {what} 设备 ({self.profile.vid:#06x}:{pid:#06x})...")

        def ready(manual: bool) -> bool:
            self._log(f"{what} 设备已就绪" + ("（人工介入后）" if manual else ""))
            self._note_serial(pid, what)
            return True

        if device_watch.wait_for_pid(self.profile.vid, pid, timeout_s,
                                     cancel=self.cancel):
            return ready(False)
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
                    return ready(True)
                time.sleep(0.5)
            return False
        finally:
            self.cb.on_user_prompt_clear()

    def _note_serial(self, pid: int, what: str) -> None:
        """Log the APP device's USB SerialNumber (version identifier); it is
        also prepended to the final report."""
        if what != "APP":
            return
        sn = device_watch.read_serial(self.profile.vid, pid)
        self.app_serial = sn
        self._log(f"APP SerialNumber: {sn}（用于版本识别/区分）" if sn
                  else "APP 设备未提供 SerialNumber 描述符")

    def _switch_to_app(self) -> bool:
        """DFU->APP：向 DFU 设备触发切换（真正就绪由随后的 wait_app 判定）。"""
        if self.profile.dfu_pid is None:
            self._log("dfu_pid 未配置，无法切换到 APP")
            return False
        return self._switch(from_pid=self.profile.dfu_pid, to_what="APP")

    def _switch_to_dfu(self) -> bool:
        """APP->DFU（复烧前置）：若设备当前在 APP 模式则触发切回 DFU；否则跳过，
        交由 wait_dfu 处理（人工插拔/上电）。"""
        if device_watch.find_pid(self.profile.vid, self.profile.app_pid):
            return self._switch(from_pid=self.profile.app_pid, to_what="DFU")
        self._log("未检测到 APP 设备，跳过自动切换，直接等待 DFU")
        return True

    def _switch(self, from_pid: int, to_what: str) -> bool:
        """DFU<->APP 切换，按可用方案依次尝试：
        1. usb_reconfig（product.toml 声明的产品自身能力）：USB 控制传输
           RECONFIG，无需外置硬件；
        2. programmer.switch（programmer.toml 声明的保底方案）：经外置烧录器
           CLI 执行；
        3. 都不可用/都失败 -> 弹窗提示按板载 MODE 按键，随后的等待步骤
        （含超时人工提示）确认真正就绪。"""
        p = self.profile
        direction = "dfu2app" if to_what == "APP" else "app2dfu"
        if p.switch_usb_reconfig:
            self._log(f"发送 RECONFIG 切换到 {to_what} 模式 "
                      f"（{p.vid:#06x}:{from_pid:#06x}）...")
            try:
                mode_switch_mod.reconfig(p.vid, from_pid, self._log)
                return True
            except mode_switch_mod.ModeSwitchError as e:
                self._log(f"RECONFIG 切换失败: {e}，尝试保底方案…")
        prog = p.programmer
        if prog is not None and direction in prog.switch:
            if programmer_mod.switch(prog, direction,
                                     cable_index=self._cable_index,
                                     log_cb=self._log, cancel=self.cancel):
                return True
            self._log("外置烧录器切换失败，转人工…")
        # 只剩硬件操作：弹窗提示工人按板载 MODE 键，等待步骤兜底确认
        msg = (f"请按板载 MODE 按键，将 {p.display_name} 切换到 {to_what} 模式；"
               "检测到目标模式设备后自动继续")
        self._log(msg)
        self.cb.on_manual_switch(msg)
        return True

    def _wait_dfu(self) -> bool:
        if self.profile.dfu_pid is None:
            self._log(f"{self.profile.display_name}: dfu_pid 未配置")
            return False
        return self._wait_mode(self.profile.dfu_pid,
                               self.profile.timeouts.wait_dfu_device_s, "DFU")

    def _wait_app(self) -> bool:
        return self._wait_mode(self.profile.app_pid,
                               self.profile.timeouts.wait_app_device_s, "APP")

    _firmware_override = None

    def _flash_app(self) -> bool:
        fw = self._firmware_override or self.profile.app_firmware
        if fw is None or self.profile.dfu_pid is None:
            self._log("固件或 dfu_pid 未配置，无法烧写")
            return False
        try:
            flasher.flash_app_firmware(
                vid=self.profile.vid, pid=self.profile.dfu_pid,
                addr=self.profile.app_flash_addr, firmware=fw,
                verify=self.profile.verify_after_flash,
                log_cb=self._log, cancel=self.cancel)
            self.report_lines.append(f"DFU 烧写: OK ({fw})")
            return True
        except flasher.FlashError as e:
            self._log(str(e))
            self.report_lines.append(f"DFU 烧写: FAIL ({e})")
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
        try:
            result = self.sigrok.capture(
                driver=self.profile.driver, channels=channels,
                samplerate_hz=samplerate_hz, samples=samples,
                voltage_threshold_v=voltage_threshold_v, out_file=out_file,
                timeout_s=self.profile.timeouts.capture_s,
                log_cb=self._log, cancel=self.cancel)
        except CaptureError as e:
            self._log(f"采样失败: {e}")
            self.report_lines.append(f"{label}: FAIL (采样失败: {e})")
            return False

        self._log(f"采样完成: {result.n_samples} samples, {result.elapsed_s:.2f}s -> {result.out_file.name}")
        chans = waveform.load_capture_file(result.out_file, channels, result.unitsize)
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

    from .profiles import load_profiles
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
    p2.steps = [StepDef("wait", "Wait", lambda: p2._wait_mode(0x7FFF, 1.0, "DFU"))]
    threading.Timer(2.5, p2.request_cancel).start()
    t0 = time.time()
    p2.start(); done.wait(15)
    elapsed = time.time() - t0
    assert results["ok"] is False and elapsed < 6, elapsed
    print(f"  (等待+提示+取消耗时 {elapsed:.1f}s)")
    print("pipeline 引擎自测 PASS")
