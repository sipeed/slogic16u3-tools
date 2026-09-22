"""External JTAG programmer operations, all driven by one CLI argv prefix.

Everything the external Gowin programmer does is the same command
(`programmer.cli`, declared in programmer.toml) + parameters + resource
files -- no wrapper shell scripts:

  probe / read device  <cli> --device D --cable-index c --run 0
  eFuse read (lock bit) <cli> --device D --cable-index c --keyread
  blank flash           <cli> --device D --cable-index c --run <run> --fsFile <image> --spiaddr <addr>
  eFuse write + lock    <cli> --device D --cable-index c --keywritefile --keyFile <key> --keylock
  DFU<->APP fallback    <cli> --device D --cable-index c <programmer.switch args>

probe() and eFuse read are read-only.  flash() and efuse_lock() write the
chip; the GUI gates them behind a successful probe (which supplies the
cable index).  eFuse write+lock is irreversible.
"""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .i18n import t
from .profiles import Programmer
from .sigrok import _popen_kwargs, watchdog
from .winexec import needs_reparent, run_reparented

EFuse = Literal["locked", "unlocked", "unknown"]


@dataclass
class ProbeResult:
    programmer_present: bool
    cable_index: int | None
    device: str | None
    efuse: EFuse
    detail: str


# --- output parsing (isolated for self-test / Gowin-version adaptation) ------

_FAIL_MARKERS = ("cable failed", "no valid jtag", "id code mismatch",
                 "error:", "unable to open", "not found")

# 写操作失败判据：Gowin 可能 rc=0 且末尾照样打 "Finished."（实测 32U3 烧录失败
# 输出 "Error: Program and Verify Flash Failed!\n Finished."），所以任何 Error:/
# Failed! 行都视为失败，不能只认结尾。
_WRITE_FAIL_MARKERS = ("cable failed", "no valid jtag", "id code mismatch",
                       "unable to open", "verify error", "verify failed",
                       "erase error", "program error", "user cancel", "timeout",
                       "error:", "failed!")


def _device_online(out: str) -> bool:
    """True when --run 0 read a real device IDCODE.

    注意 "Target Device: GW5AT-60B(0x0001481B)" 是对 --device 参数的**回显**，
    线缆打开、JTAG 链无响应（板未上电/线松）时也会打印，且此时读到
    "ID Code is: 0x00000000"（或全 F）并正常 "Finished."——必须解析真实 ID 值，
    不能见回显就当在线（实测 Windows 台架踩过：全零链被误判在线）。"""
    low = out.lower()
    if any(m in low for m in _FAIL_MARKERS):
        return False
    # Linux 版打 "ID 0x0001481B"，Windows 版打 "ID Code is: 0x00000000"
    m = re.search(r"id(?: code is)?[:\s]+0x([0-9a-f]{8})", low)
    if m:
        return m.group(1) not in ("00000000", "ffffffff")
    return False   # run 0 总会打印 ID；没有 ID 行即视为未读到器件


def _efuse_state(out: str) -> EFuse:
    """Locked check comes first: the locked --keyread reply carries both
    'Device Locked!' and an 'Error:' line, so the error markers must not win."""
    low = out.lower()
    if "device locked" in low:
        return "locked"
    if any(m in low for m in _FAIL_MARKERS):
        return "unknown"
    if "finished" in low or "key" in low:
        return "unlocked"
    return "unknown"


def _write_ok(rc: int, out: str) -> bool:
    """A flash / eFuse write succeeded.  Gowin may exit 0 even on failure, so
    require an explicit success marker AND no hard-failure marker."""
    low = out.lower()
    if any(m in low for m in _WRITE_FAIL_MARKERS):
        return False
    return rc == 0 and ("finished" in low or "success" in low)


# openFPGALoader failure markers -- benign blank-flash output ("flash chip
# unknown: use basic protection detection", Erasing/Writing/Reading/Done) has
# none of these; a verify mismatch or any hard failure prints "Error:"/"fail".
_OFL_FAIL_MARKERS = ("error", "fail", "mismatch")


def _openfpga_flash_ok(rc: int, out: str) -> bool:
    """openFPGALoader blank-flash succeeded.

    On a flash chip whose JEDEC id it does not recognise ("flash chip unknown")
    openFPGALoader still erases, writes and -- with --verify -- reads the data
    back and matches it, printing "Done", yet EXITS NON-ZERO.  So trust the
    read-back verify here, not rc.  A completed --verify prints "Verifying
    write..." then a trailing "Done"; require that Done to appear AFTER the
    verify banner, so a killed/truncated verify (no trailing Done) is never
    taken for success -- this is what keeps an unverified board from passing.
    argv_windows must pass --verify for that branch to engage (see
    resources/programmer.toml); without it we fall back to a clean exit code."""
    low = out.lower()
    if any(m in low for m in _OFL_FAIL_MARKERS):
        return False
    vpos = low.find("verifying write")
    if vpos != -1 and "done" in low[vpos:]:
        return True
    return rc == 0


# --- subprocess -------------------------------------------------------------

def _run_argv(argv: list[str], timeout_s: float,
              log_cb: Callable[[str], None],
              cancel: threading.Event | None) -> tuple[int, str]:
    """Run a full argv; return (returncode, combined stdout+stderr).
    Output is also streamed to log_cb.  rc = -1 on launch failure/timeout/cancel.

    以可执行文件自身所在目录为 CWD 运行（cwd = dirname(argv[0])）：Gowin 的
    programmer_cli.exe 内嵌 Python 3.6 + Qt，需以自身 bin 目录为工作目录装配
    qt.conf/DLL。传入的 image/输出路径均已 .resolve() 为绝对，改 CWD 不影响它们；
    对 openFPGALoader / AppImage 也无害（各自自包含）。

    打包（PyInstaller frozen）后必须经 WMI 重定父到 wmiprvse 服务再启动外部工具，
    否则 programmer_cli 只要祖先链里有 frozen 进程就报 "Error: MAINCMD module not
    found."（详见 winexec.py）。源码运行与非 Windows 不受影响，走普通 subprocess。"""
    exe = Path(argv[0])
    cwd = str(exe.parent) if exe.is_absolute() and exe.exists() else None
    if needs_reparent():
        return run_reparented(argv, cwd, timeout_s, log_cb, cancel)
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=cwd, **_popen_kwargs())
    except OSError as e:
        log_cb("[programmer] " + t("Launch failed: {e}").format(e=e))
        return -1, ""
    lines: list[str] = []
    with watchdog(proc, timeout_s, cancel) as fate:
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.rstrip()
            if s:
                lines.append(s)
                log_cb(s)
        proc.wait()
    if fate.killed_by:
        log_cb("[programmer] " + (t("cancelled") if fate.killed_by == 'cancel'
               else t("timed out ({secs}s)").format(secs=f"{timeout_s:.0f}")))
        return -1, "\n".join(lines)
    return proc.returncode, "\n".join(lines)


def _run(prog: Programmer, args: list[str], log_cb: Callable[[str], None],
         cancel: threading.Event | None,
         timeout_s: float | None = None) -> tuple[int, str]:
    """Run `<cli> <args>`（Gowin CLI 前缀形态）。`timeout_s` 覆盖
    prog.timeout_s——写操作（烧录/切换）远慢于探测，必须给更长的看门狗：实测
    Windows 烧 826KB 比 Linux 慢约 10 倍，30s 只走到 ~16% 就被旧看门狗杀掉
    （正是"烧到 16% 失败"的根因）。"""
    t = timeout_s if timeout_s is not None else prog.timeout_s
    return _run_argv([*prog.cli, *args], t, log_cb, cancel)


def _cable_args(device: str, cable: int) -> list[str]:
    return ["--device", device, "--cable-index", str(cable)]


def probe(prog: Programmer, log_cb: Callable[[str], None] = print,
          cancel: threading.Event | None = None) -> ProbeResult:
    """Try each cable candidate until one reads a device, then read its eFuse
    lock state.  Never writes the chip."""
    if not prog.cli:
        log_cb("[probe] " + t("Programmer CLI not found (programmer.cli undeclared and no default AppImage)"))
        return ProbeResult(False, None, None, "unknown", t("Programmer CLI not found"))
    dev = prog.device
    for cable in prog.cable_candidates:
        if cancel is not None and cancel.is_set():
            return ProbeResult(False, None, None, "unknown", t("cancelled"))
        log_cb("[probe] " + t("Trying cable-index {cable} (device={dev}) to read the IDCODE...").format(cable=cable, dev=dev))
        _, out = _run(prog, [*_cable_args(dev, cable), "--run", "0"], log_cb, cancel)
        if not _device_online(out):
            continue
        log_cb("[probe] " + t("cable-index {cable} read a device, reading eFuse lock state...").format(cable=cable))
        _, kout = _run(prog, [*_cable_args(dev, cable), "--keyread"], log_cb, cancel)
        efuse = _efuse_state(kout)
        log_cb("[probe] " + t("Result: external programmer connected, device={dev}, "
               "cable-index={cable}, eFuse={efuse}").format(dev=dev, cable=cable, efuse=efuse))
        return ProbeResult(True, cable, dev, efuse,
                           f"cable-index {cable}, device {dev}, eFuse={efuse}")
    log_cb("[probe] " + t("Result: no device read on any cable -- external programmer not connected or unpowered"))
    return ProbeResult(False, None, None, "unknown",
                       t("No device read on any cable (external programmer not connected?)"))


def _resolve_cable(prog: Programmer, cable_index: int | None,
                   log_cb: Callable[[str], None],
                   cancel: threading.Event | None) -> int | None:
    """Use the given cable, or probe to find one (write ops need a cable)."""
    if cable_index is not None:
        return cable_index
    log_cb("[programmer] " + t("No cable-index provided, probing first..."))
    res = probe(prog, log_cb, cancel)
    return res.cable_index if res.programmer_present else None


def flash(prog: Programmer, cable_index: int | None = None,
          image: Path | None = None,
          log_cb: Callable[[str], None] = print,
          cancel: threading.Event | None = None) -> bool:
    """Blank-flash: program the DFU image into external SPI flash.
    `image` 覆盖 op.image（GUI 会话级资源路径覆盖）。"""
    op = prog.flash
    if op is None:
        log_cb("[flash] " + t("programmer.toml declares no [programmer.flash]; cannot flash"))
        return False
    img = image if image is not None else op.image
    if not img.is_file():
        log_cb("[flash] " + t("DFU image missing: {img}").format(img=img))
        return False
    if op.argv:
        # 自定义烧录命令（如 openFPGALoader，Windows 上比 Gowin exe 快 ~15 倍）：
        # 完整命令自带线缆参数，不依赖 Gowin cli/cable 探测。写入正确性已用
        # Gowin --run 66 (exFlash Verify) 交叉校验过。
        argv = [a.replace("{image}", str(img))
                 .replace("{spiaddr}", f"{op.spiaddr:#x}") for a in op.argv]
        log_cb("[flash] " + t("Blank-flash (custom command, watchdog {secs}s): ").format(secs=f"{op.timeout_s:.0f}")
               + ' '.join(argv))
        rc, out = _run_argv(argv, op.timeout_s, log_cb, cancel)
        ok = _openfpga_flash_ok(rc, out)
        log_cb("[flash] " + (t("flash complete") if ok else t("flash failed")))
        return ok
    if not prog.cli:
        log_cb("[flash] " + t("Programmer CLI not found; cannot flash"))
        return False
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[flash] " + t("No usable cable found; external programmer not connected?"))
        return False
    log_cb("[flash] " + t("Blank-flash: device={device} cable-index={cable} "
           "run={run} --fsFile={fsfile} spiaddr={spiaddr} (watchdog {secs}s)").format(
           device=prog.device, cable=cable, run=op.run, fsfile=img.name,
           spiaddr=f"{op.spiaddr:#08x}", secs=f"{op.timeout_s:.0f}"))
    # --fsFile 接受 .fs / .bin 位流；需绝对路径（op.image 已 resolve）
    rc, out = _run(prog, [*_cable_args(prog.device, cable),
                          "--run", str(op.run), "--fsFile", str(img),
                          "--spiaddr", f"{op.spiaddr:#08x}"], log_cb, cancel,
                   timeout_s=op.timeout_s)
    ok = _write_ok(rc, out)
    log_cb("[flash] " + (t("flash complete") if ok else t("flash failed")))
    return ok


def efuse_state(prog: Programmer, cable_index: int | None = None,
                log_cb: Callable[[str], None] = print,
                cancel: threading.Event | None = None
                ) -> tuple[EFuse, int | None]:
    """读 eFuse 锁定状态（--keyread，只读）。返回 (状态, 实际使用的 cable)。"""
    if not prog.cli:
        log_cb("[efuse] " + t("Programmer CLI not found"))
        return "unknown", None
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[efuse] " + t("No usable cable found; external programmer not connected?"))
        return "unknown", None
    _, out = _run(prog, [*_cable_args(prog.device, cable), "--keyread"],
                  log_cb, cancel)
    state = _efuse_state(out)
    log_cb("[efuse] " + t("state: {state} (cable-index={cable})").format(state=state, cable=cable))
    return state, cable


def switch(prog: Programmer, direction: str, cable_index: int | None = None,
           log_cb: Callable[[str], None] = print,
           cancel: threading.Event | None = None) -> bool:
    """DFU<->APP 保底切换：执行 [programmer.switch] 声明的 dfu2app / app2dfu
    参数（如经 JTAG 向 SRAM 写入另一份位流）。未声明该方向 -> False（调用方
    回退弹窗人工）。"""
    args = prog.switch.get(direction)
    if not prog.cli or not args:
        return False
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[switch] " + t("No usable cable found; external programmer not connected?"))
        return False
    log_cb("[switch] " + t("Switching via external programmer ({direction}): device={device} "
           "cable-index={cable}").format(direction=direction, device=prog.device, cable=cable))
    # 切换多为 SRAM 写位流（写操作），给比探测长的看门狗
    rc, out = _run(prog, [*_cable_args(prog.device, cable), *args],
                   log_cb, cancel, timeout_s=max(prog.timeout_s, 300))
    ok = _write_ok(rc, out)
    log_cb("[switch] " + (t("switch command complete") if ok else t("switch command failed")))
    return ok


def efuse_lock(prog: Programmer, cable_index: int | None = None,
               key_file: Path | None = None,
               log_cb: Callable[[str], None] = print,
               cancel: threading.Event | None = None) -> bool:
    """Write the AES key eFuse and lock it (IRREVERSIBLE).
    `key_file` 覆盖 prog.efuse_key_file（GUI 会话级资源路径覆盖）。"""
    if not prog.cli:
        log_cb("[efuse] " + t("Programmer CLI not found; cannot write-lock"))
        return False
    key = key_file if key_file is not None else prog.efuse_key_file
    if key is None:
        log_cb("[efuse] " + t("programmer.toml declares no [programmer.efuse]; cannot write-lock"))
        return False
    if not key.is_file():
        log_cb("[efuse] " + t("Key file missing: {key}").format(key=key))
        return False
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[efuse] " + t("No usable cable found; external programmer not connected?"))
        return False
    log_cb("[efuse] " + t("Writing and locking AES key (irreversible): device={device} "
           "cable-index={cable} keyFile={keyfile}").format(device=prog.device, cable=cable, keyfile=key.name))
    # 写操作看门狗下限：不可逆操作中途被杀风险最大，宁可多等
    rc, out = _run(prog, [*_cable_args(prog.device, cable),
                          "--keywritefile", "--keyFile", str(key),
                          "--keylock"], log_cb, cancel,
                   timeout_s=max(prog.timeout_s, 120))
    ok = _write_ok(rc, out)
    log_cb("[efuse] " + (t("write-lock complete (key written and locked)") if ok else t("write-lock failed")))
    return ok


if __name__ == "__main__":
    # pure-parsing self-test using captured Gowin Programmer output
    run0_ok = ("op 0: Target Device: GW5AT-60B(0x0001481B); ID 0x0001481B; "
               "User Code 0x00004946; Status Code 0x7002E020; Finished.")
    run0_bad = "Error: Cable failed to open via the channel"
    # Windows 台架实抓：线缆能开、板未上电/JTAG 链无响应——回显 Target Device
    # 且正常 Finished，但真实 ID 全零，必须判离线
    run0_dead = (" Target Cable: USB Debugger A/1/None/null@2.5MHz\n"
                 " Target Device: GW5AT-60B(0x0001481B)\n"
                 " ID Code is: 0x00000000\n Finished.\n Cost 0.07 second(s)")
    kr_locked = "Key1 Sel.\nError: Device Locked!\nValue: 1"
    kr_unlocked = "Key1 Sel.\nValue: 0123456789ABCDEF0123456789ABCDEF\nFinished."
    kr_err = "Error: Cable failed to open via the channel"
    flash_ok = "Erase Flash...\nProgram Flash...\nVerify Flash...\nFinished."
    flash_bad = "Program Flash...\nVerify Error at 0x1000\nUser Cancel!"
    # 32U3 真机抓包：.bin 误经 --fsFile 载荷为 0，rc=0 且带 "Finished." 但中间报错
    flash_bad2 = (" Programmning Flash starts from 0x800000.\n"
                  " Programmning Flash ends at 0x0800000.\n"
                  "Error: Program and Verify Flash Failed!\n Finished.")
    assert _device_online(run0_ok) is True
    assert _device_online(run0_bad) is False
    assert _device_online(run0_dead) is False     # 全零 IDCODE = 链无响应
    assert _efuse_state(kr_locked) == "locked"
    assert _efuse_state(kr_unlocked) == "unlocked"
    assert _efuse_state(kr_err) == "unknown"
    # 自定义烧录命令的模板替换（openFPGALoader 形态）
    _tpl = ["openFPGALoader.exe", "-c", "ft2232", "--external-flash",
            "-o", "{spiaddr}", "{image}"]
    _sub = [a.replace("{image}", "/x/dfu.bin").replace("{spiaddr}", "0x800000")
            for a in _tpl]
    assert _sub[-2:] == ["0x800000", "/x/dfu.bin"]
    assert _write_ok(0, flash_ok) is True
    assert _write_ok(0, flash_bad) is False
    assert _write_ok(0, flash_bad2) is False      # Error:+Finished 同现 -> 失败
    assert _write_ok(1, flash_ok) is False        # nonzero rc, even with "Finished"
    # openFPGALoader 烧空板：未识别 flash 上擦+写+校验全过却硬返回非零码（同事实测），
    # 故以 "Verifying write" 后出现 Done（回读通过）为准，不认退出码；校验被截断或
    # 不符时绝不放行，防止没真正写对的板流过产测。
    ofl_ok = ("flash chip unknown: use basic protection detection\n"
              "Writing: [==================================================] 100.00%\nDone\n"
              "Verifying write (May take time)\n"
              "Reading: [==================================================] 100.00%\nDone")
    ofl_killed = ("Writing: [====] 100.00%\nDone\n"
                  "Verifying write (May take time)\nReading: [======      ] 42.00%")
    ofl_mismatch = ofl_ok + "\nError: Verification failed at 0x0"
    ofl_no_verify = "Erasing: [====] 100.00%\nDone\nWriting: [====] 100.00%\nDone"
    assert _openfpga_flash_ok(1, ofl_ok) is True        # 非零码但回读通过 -> 成功
    assert _openfpga_flash_ok(-1, ofl_killed) is False  # 校验被截断（无 Done）-> 失败
    assert _openfpga_flash_ok(1, ofl_mismatch) is False # 校验不符（Error）-> 失败
    assert _openfpga_flash_ok(0, ofl_no_verify) is True # 无 --verify 时回退认 rc==0
    assert _openfpga_flash_ok(1, ofl_no_verify) is False
    print("programmer 解析自测 PASS（online / locked / unlocked / 未连接 / 烧录成败 / openFPGALoader 校验）")
