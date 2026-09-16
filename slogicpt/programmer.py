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

import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Literal

from .profiles import Programmer
from .sigrok import _popen_kwargs, watchdog

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
    """True when --run 0 read a real device (IDCODE), not a cable/JTAG failure."""
    low = out.lower()
    if any(m in low for m in _FAIL_MARKERS):
        return False
    # a valid read prints "Target Device: ..." and/or a GW5A "ID 0x0001xxxx"
    return ("target device" in low) or ("id 0x0001" in low) or ("idcode" in low)


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


# --- subprocess -------------------------------------------------------------

def _run(prog: Programmer, args: list[str], log_cb: Callable[[str], None],
         cancel: threading.Event | None) -> tuple[int, str]:
    """Run `<cli> <args>`; return (returncode, combined stdout+stderr).
    Output is also streamed to log_cb.  rc = -1 on launch failure/timeout/cancel."""
    argv = [*prog.cli, *args]
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **_popen_kwargs())
    except OSError as e:
        log_cb(f"[programmer] 启动失败: {e}")
        return -1, ""
    lines: list[str] = []
    with watchdog(proc, prog.timeout_s, cancel) as fate:
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.rstrip()
            if s:
                lines.append(s)
                log_cb(s)
        proc.wait()
    if fate.killed_by:
        log_cb(f"[programmer] {'已取消' if fate.killed_by == 'cancel' else f'超时 ({prog.timeout_s:.0f}s)'}")
        return -1, "\n".join(lines)
    return proc.returncode, "\n".join(lines)


def _cable_args(device: str, cable: int) -> list[str]:
    return ["--device", device, "--cable-index", str(cable)]


def probe(prog: Programmer, log_cb: Callable[[str], None] = print,
          cancel: threading.Event | None = None) -> ProbeResult:
    """Try each cable candidate until one reads a device, then read its eFuse
    lock state.  Never writes the chip."""
    if not prog.cli:
        log_cb("[probe] 未找到烧录器 CLI（programmer.cli 未声明且无默认 AppImage）")
        return ProbeResult(False, None, None, "unknown", "未找到烧录器 CLI")
    dev = prog.device
    for cable in prog.cable_candidates:
        if cancel is not None and cancel.is_set():
            return ProbeResult(False, None, None, "unknown", "已取消")
        log_cb(f"[probe] 尝试 cable-index {cable}（device={dev}）读器件码…")
        _, out = _run(prog, [*_cable_args(dev, cable), "--run", "0"], log_cb, cancel)
        if not _device_online(out):
            continue
        log_cb(f"[probe] cable-index {cable} 读到器件，读 eFuse 锁定状态…")
        _, kout = _run(prog, [*_cable_args(dev, cable), "--keyread"], log_cb, cancel)
        efuse = _efuse_state(kout)
        log_cb(f"[probe] 结果：外置烧录器已连接，device={dev}，"
               f"cable-index={cable}，eFuse={efuse}")
        return ProbeResult(True, cable, dev, efuse,
                           f"cable-index {cable}, device {dev}, eFuse={efuse}")
    log_cb("[probe] 结果：未在任何 cable 上读到器件——外置烧录器未连接或未上电")
    return ProbeResult(False, None, None, "unknown",
                       "未在任何 cable 上读到器件（外置烧录器未连接？）")


def _resolve_cable(prog: Programmer, cable_index: int | None,
                   log_cb: Callable[[str], None],
                   cancel: threading.Event | None) -> int | None:
    """Use the given cable, or probe to find one (write ops need a cable)."""
    if cable_index is not None:
        return cable_index
    log_cb("[programmer] 未提供 cable-index，先探测…")
    res = probe(prog, log_cb, cancel)
    return res.cable_index if res.programmer_present else None


def flash(prog: Programmer, cable_index: int | None = None,
          log_cb: Callable[[str], None] = print,
          cancel: threading.Event | None = None) -> bool:
    """Blank-flash: program the DFU image into external SPI flash."""
    if not prog.cli:
        log_cb("[flash] 未找到烧录器 CLI，无法烧录")
        return False
    op = prog.flash
    if op is None:
        log_cb("[flash] programmer.toml 未声明 [programmer.flash]，无法烧录")
        return False
    if not op.image.is_file():
        log_cb(f"[flash] DFU 镜像缺失: {op.image}")
        return False
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[flash] 未找到可用 cable，外置烧录器未连接？")
        return False
    log_cb(f"[flash] 烧空板：device={prog.device} cable-index={cable} "
           f"run={op.run} --fsFile={op.image.name} spiaddr={op.spiaddr:#08x}")
    # --fsFile 接受 .fs / .bin 位流；需绝对路径（op.image 已 resolve）
    rc, out = _run(prog, [*_cable_args(prog.device, cable),
                          "--run", str(op.run), "--fsFile", str(op.image),
                          "--spiaddr", f"{op.spiaddr:#08x}"], log_cb, cancel)
    ok = _write_ok(rc, out)
    log_cb(f"[flash] {'烧录完成' if ok else '烧录失败'}")
    return ok


def efuse_state(prog: Programmer, cable_index: int | None = None,
                log_cb: Callable[[str], None] = print,
                cancel: threading.Event | None = None
                ) -> tuple[EFuse, int | None]:
    """读 eFuse 锁定状态（--keyread，只读）。返回 (状态, 实际使用的 cable)。"""
    if not prog.cli:
        log_cb("[efuse] 未找到烧录器 CLI")
        return "unknown", None
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[efuse] 未找到可用 cable，外置烧录器未连接？")
        return "unknown", None
    _, out = _run(prog, [*_cable_args(prog.device, cable), "--keyread"],
                  log_cb, cancel)
    state = _efuse_state(out)
    log_cb(f"[efuse] 状态: {state}（cable-index={cable}）")
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
        log_cb("[switch] 未找到可用 cable，外置烧录器未连接？")
        return False
    log_cb(f"[switch] 经外置烧录器切换（{direction}）：device={prog.device} "
           f"cable-index={cable}")
    rc, out = _run(prog, [*_cable_args(prog.device, cable), *args],
                   log_cb, cancel)
    ok = _write_ok(rc, out)
    log_cb(f"[switch] {'切换命令完成' if ok else '切换命令失败'}")
    return ok


def efuse_lock(prog: Programmer, cable_index: int | None = None,
               log_cb: Callable[[str], None] = print,
               cancel: threading.Event | None = None) -> bool:
    """Write the AES key eFuse and lock it (IRREVERSIBLE)."""
    if not prog.cli:
        log_cb("[efuse] 未找到烧录器 CLI，无法写锁")
        return False
    key = prog.efuse_key_file
    if key is None:
        log_cb("[efuse] programmer.toml 未声明 [programmer.efuse]，无法写锁")
        return False
    if not key.is_file():
        log_cb(f"[efuse] 密钥文件缺失: {key}")
        return False
    cable = _resolve_cable(prog, cable_index, log_cb, cancel)
    if cable is None:
        log_cb("[efuse] 未找到可用 cable，外置烧录器未连接？")
        return False
    log_cb(f"[efuse] 写入并锁定 AES 密钥（不可逆）：device={prog.device} "
           f"cable-index={cable} keyFile={key.name}")
    rc, out = _run(prog, [*_cable_args(prog.device, cable),
                          "--keywritefile", "--keyFile", str(key),
                          "--keylock"], log_cb, cancel)
    ok = _write_ok(rc, out)
    log_cb(f"[efuse] {'写锁完成（密钥已写入并锁定）' if ok else '写锁失败'}")
    return ok


if __name__ == "__main__":
    # pure-parsing self-test using captured Gowin Programmer output
    run0_ok = ("op 0: Target Device: GW5AT-60B(0x0001481B); ID 0x0001481B; "
               "User Code 0x00004946; Status Code 0x7002E020; Finished.")
    run0_bad = "Error: Cable failed to open via the channel"
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
    assert _efuse_state(kr_locked) == "locked"
    assert _efuse_state(kr_unlocked) == "unlocked"
    assert _efuse_state(kr_err) == "unknown"
    assert _write_ok(0, flash_ok) is True
    assert _write_ok(0, flash_bad) is False
    assert _write_ok(0, flash_bad2) is False      # Error:+Finished 同现 -> 失败
    assert _write_ok(1, flash_ok) is False        # nonzero rc, even with "Finished"
    print("programmer 解析自测 PASS（online / locked / unlocked / 未连接 / 烧录成败）")
