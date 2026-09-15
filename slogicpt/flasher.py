"""OTA/DFU firmware flashing, wrapping the slogicpt.dfu library."""
from __future__ import annotations

import contextlib
import io
import threading
from pathlib import Path
from typing import Callable

from .dfu.spi_flash import flash_firmware


class FlashError(Exception):
    pass


class _LogBridge(io.TextIOBase):
    """Redirect dfu print() progress into a line callback."""

    def __init__(self, log_cb: Callable[[str], None]):
        self._log_cb = log_cb
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log_cb(line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._log_cb(self._buf)
        self._buf = ""


def flash_app_firmware(*, vid: int, pid: int, addr: int, firmware: Path,
                       verify: bool = True,
                       log_cb: Callable[[str], None] | None = None,
                       cancel: threading.Event | None = None) -> None:
    """Flash the application firmware onto a device in OTA mode.

    Raises FlashError with a readable message on any failure.
    """
    firmware = Path(firmware)
    if not firmware.is_file():
        raise FlashError(f"固件文件不存在: {firmware}")
    if cancel is not None and cancel.is_set():
        raise FlashError("已取消")
    data = firmware.read_bytes()
    if log_cb:
        log_cb(f"固件: {firmware} ({len(data)} bytes) -> {vid:#06x}:{pid:#06x} @ {addr:#x}")

    out = _LogBridge(log_cb) if log_cb else io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            flash_firmware(vid, pid, addr, data, verify=verify)
    except FlashError:
        raise
    except Exception as e:  # usb.core errors, RuntimeError, assertion...
        raise FlashError(f"OTA 烧写失败: {e}") from e
    finally:
        out.flush()


if __name__ == "__main__":
    # smoke test: imports resolve (run as python -m slogicpt.flasher)
    from .dfu.spi_flash import SPIFlashDevice  # noqa: F401
    print("flasher: dfu imports OK")
    print("用法示例: flash_app_firmware(vid=0x359F, pid=0x30F1, addr=0x0, firmware=Path('app.bin'))")
