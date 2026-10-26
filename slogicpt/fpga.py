"""Tang Nano 9K 信号发生器控制（产测夹具的信号源）。

夹具里的 Tang Nano 9K 跑 `testgen` 设计，产生 10 种测试图案（M0..M9）。
本模块负责找到它的串口、切换模式、并确认切换成功。

协议（见 FPGA 侧 `rtl/cmd_rx.v`）
--------------------------------
    '0'..'9'   选择模式 0..9
    'n' / 'p'  下一个 / 上一个（环形）
    'r'        重新开始当前图案（帧复位脉冲）
    FPGA 在**模式发生变化时**回一行 `"MODE n\r\n"`；模式没变则不回。

串口 115200 8N1。加 `\n` 结尾也可以，FPGA 只取第一个字符、其余忽略。

实测要点（这些都踩过，别重复踩）
--------------------------------
* 端口是 **VID:PID = 0x0403:0x6010**（BL702 模拟 FTDI），序列号形如
  `BL702_FTDI_2038131017A8`（旧固件）或 `FactoryAIOT Pro`（新固件）。
  注意 **pyserial 把 vid/pid 报成十进制**（0x0403 显示为 1027），比较时务必
  同时按十六进制写。
* **串口必须常开**。早期版本每次切模式都新起一个 PowerShell 去发字符，
  单次约 1.7 秒；常开后降到约 0.1 秒。40 格的矩阵里这是 60 秒的差别。
* **模式没变就没有回复**，这是正常行为，不是故障。所以本模块缓存"已确认的
  模式"，目标与缓存一致时**直接跳过**，连字符都不发。
* "FPGA 死了" 和 "已经在该模式" 都会表现为没有回复，必须区分。用 `'p'`
  做存活检查：`'p'` 一定会改变模式，所以**一定会回复**。
* 切换后要给图案一点稳定时间（`settle_ms`），否则分析仪可能抓到切换瞬间的
  半截图案。
"""
from __future__ import annotations

import threading
import time
import re
from dataclasses import dataclass

try:                                    # 集成进 slogicpt 时走这条
    from .i18n import t
except ImportError:                     # 独立运行/自测时走这条
    def t(s, **kw):
        return s.format(**kw) if kw else s

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:                     # pragma: no cover
    serial = None
    list_ports = None


# 夹具 BL702 的 USB 标识。pyserial 报十进制（0x0403 == 1027）。
# 这两个值和下面的序列号关键字是**默认值**；正常运行取 resources/fixture.toml，
# 换批次/换固件只改那个文件。
FPGA_VID = 0x0403
FPGA_PID = 0x6010

# 光靠 VID:PID 不够：**Gowin 的 USB 烧录器也是 0403:6010**（FT2232H 双通道），
# 而且它的两个通道会各占一个串口。只要烧录器插着，自动查找就会同时匹配到
# Tang Nano 和烧录器的 A/B 通道，于是报"找到多个"。
#
# 实测端口的序列号：
#     COM20  2025030317A                  <- 烧录器通道 A（FT2232）
#     COM21  2025030317B                  <- 烧录器通道 B（FT2232）
#     COM30  BL702_FTDI_2038131017A8      <- Tang Nano 9K（旧 BL702 固件）
#     COM25  FactoryAIOT Pro              <- Tang Nano 9K（新 BL702 固件）
#
# Tang Nano 的 BL702 固件会在序列号里带 `BL702` 或 `FactoryAIOT`；普通 FT2232
# 不带。用它区分。注意新版固件的序列号**含空格**，Windows 的设备实例 ID 会把
# 空格换成下划线并转大写（pyserial 因此报 `FACTORYAIOT_PRO`），而 openFPGALoader
# 看到的是原始的 `FactoryAIOT Pro`——两处都要按"含 FactoryAIOT"来认。
FPGA_SERIAL_HINTS = ("BL702", "FACTORYAIOT")


@dataclass(frozen=True)
class FixtureUsb:
    """夹具的 USB 身份（resources/fixture.toml）。默认值 = 当前已知硬件。"""
    vid: int = FPGA_VID
    pid: int = FPGA_PID
    serial_hints: tuple[str, ...] = FPGA_SERIAL_HINTS


# 夹具固件 testgen 现在有 7 个模式（M0..M6）。以前是 10 个，自测曾因此
# 去问 M7..M9，FPGA 正确地拒绝确认，自测却把好硬件报成 FAIL。
MODE_COUNT = 7

BAUD = 115200


class SignalSourceNotFound(Exception):
    """找不到信号发生器的串口。相关测试项据此判 FAIL（原因：没有信号发生器）。"""


class SignalSourceError(Exception):
    """串口打开成功但通信异常（例如存活检查没有回复）。"""


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    serial_number: str | None
    location: str | None = None      # Linux: "1-7.4.4:1.1" (…:config.interface)


def find_ports(usb: FixtureUsb = FixtureUsb()) -> list[PortInfo]:
    """列出所有看起来像 Tang Nano 的串口。"""
    if list_ports is None:
        return []
    out = []
    for p in list_ports.comports():
        if p.vid == usb.vid and p.pid == usb.pid:
            out.append(PortInfo(p.device, p.description or "", p.serial_number,
                                getattr(p, "location", None)))
    return out


def is_fixture_serial(serial: str | None,
                      usb: FixtureUsb = FixtureUsb()) -> bool:
    """True for the fixture's BL702 (its serial carries one of the configured
    hints); False for the Gowin programmer's FT2232H, which shares VID:PID."""
    s = (serial or "").upper()
    return any(h.upper() in s for h in usb.serial_hints if h)


def fixture_ports(usb: FixtureUsb = FixtureUsb()) -> list[str]:
    """Serial ports of the fixture (empty when absent or pyserial is missing).
    The single source for "is the fixture plugged in" -- pipeline and the GUI
    both ask here."""
    try:
        return [p.device for p in find_ports(usb)
                if is_fixture_serial(p.serial_number, usb)]
    except Exception:                       # noqa: BLE001  (enumeration can fail mid-replug)
        return []


def fixture_present(usb: FixtureUsb = FixtureUsb()) -> bool:
    return bool(fixture_ports(usb))


def _usb_interface(location: str | None) -> int | None:
    """USB interface number from pyserial's location ("1-7.4.4:1.1" ->
    config 1, interface 1).  None where the platform does not report it."""
    m = re.search(r":\d+\.(\d+)$", location or "")
    return int(m.group(1)) if m else None


def find_port(explicit: str | None = None,
              usb: FixtureUsb = FixtureUsb()) -> str:
    """定位信号发生器的串口。

    `explicit` 给出时直接用它（但仍要求存在）。否则按 VID:PID 自动查找。
    找不到、或有多个候选而无法区分时抛 SignalSourceNotFound。
    """
    if explicit:
        if list_ports is not None:
            known = {p.device.upper() for p in list_ports.comports()}
            if explicit.upper() not in known:
                raise SignalSourceNotFound(
                    t("Configured port {p} is not present").format(p=explicit))
        return explicit

    cands = find_ports(usb)
    if not cands:
        raise SignalSourceNotFound(
            t("No Tang Nano signal generator found (looking for {vid:#06x}:{pid:#06x})")
            .format(vid=usb.vid, pid=usb.pid))
    if len(cands) == 1:
        return cands[0].device

    # More than one matching port. The usual cause is the Gowin USB programmer,
    # which is an FT2232H with the same IDs and TWO serial ports of its own -- so
    # plugging it in for a production run makes this look ambiguous even though
    # only one of the ports is the fixture.
    #
    # The fixture's BL702 puts one of the configured serial hints in its serial
    # number; an FT2232 does not.
    bl702 = [c for c in cands if is_fixture_serial(c.serial_number, usb)]
    if len(bl702) == 1:
        return bl702[0].device
    # One fixture seen twice: on Linux its FT2232 emulation exposes BOTH interfaces
    # as ttyUSB -- A (interface 0) is JTAG, B (interface 1) is the UART that
    # speaks the mode protocol. Windows usually shows only one COM port.
    if len({c.serial_number for c in bl702}) == 1:
        uart = [c for c in bl702 if _usb_interface(c.location) == 1]
        if len(uart) == 1:
            return uart[0].device

    hints = " / ".join(usb.serial_hints)
    names = ", ".join("%s(%s)" % (c.device, c.serial_number) for c in cands)
    hint = (t("none of them reports a serial containing \"{h}\"")
            .format(h=hints) if not bl702 else
            t("{n} of them report a serial containing \"{h}\"")
            .format(n=len(bl702), h=hints))
    raise SignalSourceNotFound(
        t("Multiple {vid:#06x}:{pid:#06x} ports found ({names}); {hint}. "
          "The Gowin USB programmer is an FT2232H with the same IDs and two "
          "ports of its own, so unplug it if you do not need it, or set "
          "signal_source.serial explicitly to the fixture's port.")
        .format(vid=usb.vid, pid=usb.pid, names=names, hint=hint))


class SignalSource:
    """保持串口常开的信号发生器控制器。

    线程安全（产测流程在独立线程里跑步骤）。所有公开方法都加锁。
    """

    def __init__(self, port: str | None = None, baud: int = BAUD,
                 settle_ms: int = 400, reply_timeout_ms: int = 1000,
                 usb: FixtureUsb | None = None):
        self._port_arg = port
        self._baud = baud
        self._settle_s = settle_ms / 1000.0
        self._reply_timeout_s = reply_timeout_ms / 1000.0
        self._usb = usb or FixtureUsb()
        self._ser = None
        self._lock = threading.RLock()
        self._mode: int | None = None      # 已确认的模式；None = 未知
        self.port = None

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        """找端口、打开、做存活检查、回到 M0。"""
        if serial is None:
            raise SignalSourceError(
                "pyserial is not installed (pip install pyserial)")
        with self._lock:
            if self._ser is not None:
                return
            self.port = find_port(self._port_arg, self._usb)
            try:
                self._ser = serial.Serial(self.port, self._baud, timeout=0.05,
                                          write_timeout=2.0)
            except Exception as e:                       # noqa: BLE001
                raise SignalSourceError(
                    t("Cannot open {p}: {e}").format(p=self.port, e=e)) from e

            time.sleep(0.2)
            self._drain()

            # 存活检查：'p' 一定会改变模式，所以一定会回复。
            if not self._liveness():
                self.close()
                raise SignalSourceError(
                    t("{p} opened but the FPGA did not answer; "
                      "is the bitstream loaded?").format(p=self.port))

            self._mode = None
            self.select_mode(0)

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:                        # noqa: BLE001
                    pass
                self._ser = None
            self._mode = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
        return False

    # -- mode control -------------------------------------------------------

    def select_mode(self, mode: int) -> bool:
        """切到指定模式。返回 True 表示已确认。

        目标与已确认模式一致时直接返回，不发字符 —— 因为模式没变 FPGA 不会回复，
        发了也确认不了，只会白等一个超时。
        """
        if not 0 <= mode <= 9:
            raise ValueError("mode must be 0..9, got %r" % (mode,))
        with self._lock:
            if self._ser is None:
                raise SignalSourceError("signal source is not open")
            if self._mode == mode:
                return True

            if not self._send_and_expect(mode):
                # 没有回复：可能本来就在该模式（并发/外部改过），
                # 用一次 'p' 强制变化再试，这样能确定 FPGA 还活着。
                self._write(b"p")
                self._drain()
                if not self._send_and_expect(mode):
                    raise SignalSourceError(
                        t("No confirmation for mode {m} on {p}")
                        .format(m=mode, p=self.port))

            self._mode = mode
            time.sleep(self._settle_s)
            return True

    @property
    def mode(self) -> int | None:
        """已确认的模式；None 表示未知。"""
        return self._mode

    # -- internals ----------------------------------------------------------

    def _write(self, data: bytes) -> None:
        self._ser.reset_input_buffer()
        self._ser.write(data)
        self._ser.flush()

    def _drain(self) -> None:
        """丢掉缓冲区里残留的内容（上一次回复可能还没读完）。"""
        try:
            self._ser.reset_input_buffer()
        except Exception:                                # noqa: BLE001
            pass

    def _read_reply(self, timeout_s: float) -> str:
        deadline = time.time() + timeout_s
        buf = bytearray()
        while time.time() < deadline:
            n = self._ser.in_waiting
            if n:
                buf += self._ser.read(n)
                if b"MODE" in buf:
                    # 收到完整一行就返回
                    if b"\n" in buf:
                        break
            else:
                time.sleep(0.005)
        return buf.decode("ascii", "replace")

    def _send_and_expect(self, mode: int) -> bool:
        self._write(bytes([ord("0") + mode]))
        reply = self._read_reply(self._reply_timeout_s)
        return ("MODE %d" % mode) in reply

    def _liveness(self, tries: int = 3) -> bool:
        for _ in range(tries):
            self._write(b"p")
            if "MODE" in self._read_reply(self._reply_timeout_s):
                return True
        return False


# ---------------------------------------------------------------------------
# 命令行自测：python -m slogicpt.fpga
# ---------------------------------------------------------------------------
def _selftest() -> int:
    usb = FixtureUsb()
    try:
        from .profiles import load_profiles
        profiles, _ = load_profiles()
        if profiles:
            usb = profiles[0].signal_source.usb
    except Exception:                                       # noqa: BLE001
        pass
    print("== 查找信号发生器 ==")
    print("  USB 身份: %#06x:%#06x  serial_hints=%s"
          % (usb.vid, usb.pid, ", ".join(usb.serial_hints)))
    cands = find_ports(usb)
    if not cands:
        print("  未找到（VID:PID %#06x:%#06x）" % (usb.vid, usb.pid))
        return 1
    for c in cands:
        print("  %s  %s  sn=%s" % (c.device, c.description, c.serial_number))

    print("== 打开 ==")
    src = SignalSource(usb=usb)
    try:
        src.open()
    except SignalSourceNotFound as e:
        print("  未找到: %s" % e)
        return 1
    except SignalSourceError as e:
        print("  打开失败: %s" % e)
        return 1
    print("  端口 %s 已打开，存活检查通过，当前模式 %s" % (src.port, src.mode))

    # 7 patterns now (M0..M6), not 10. The self-test asked for M7..M9, the FPGA
    # correctly refused to confirm them, and the test reported FAIL against
    # working hardware.
    print("== 依次切到 0..%d，记录耗时 ==" % (MODE_COUNT - 1))
    ok = True
    for m in range(MODE_COUNT):
        t0 = time.time()
        try:
            src.select_mode(m)
        except SignalSourceError as e:
            print("  M%d 失败: %s" % (m, e))
            ok = False
            continue
        print("  M%d  ok   %.3f s" % (m, time.time() - t0))

    print("== 重复切到同一模式应跳过（不发字符）==")
    t0 = time.time()
    src.select_mode(MODE_COUNT - 1)
    print("  重切 M%d  %.4f s" % (MODE_COUNT - 1, time.time() - t0))

    src.close()
    print("RESULT: %s" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
