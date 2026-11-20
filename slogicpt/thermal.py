"""Chip temperature from R32_ADC_TEMP (0x0048) while a capture streams.

Register layout (SLogic USB protocol, `R32_ADC_TEMP`):
  [31:24] conversion counter (advances continuously)
  [16]    valid (sticky)
  [15:0]  signed Q2 degC (0.25 degC/LSB)

Reading it is one 4-byte vendor control transfer on endpoint 0.  Measured on
the bench: a read every 20 s during a 240 GB continuous capture disturbed
nothing (61 reads, no sample errors, no USB events).  valid = 0 means the
firmware does not report a temperature (older images).

The monitor runs on its own thread so a long soak can show start/peak/end
without touching the capture path.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .ddr import DdrDevice, DdrError
from .device_watch import DeviceStatus

TEMP_INDEX = 0x12           # 0x0048


def celsius(raw: int) -> float | None:
    """Decode R32_ADC_TEMP; None when the firmware marks it invalid."""
    if not (raw >> 16) & 1:
        return None
    t = raw & 0xFFFF
    if t & 0x8000:
        t -= 0x10000
    return t * 0.25


@dataclass
class ThermalStats:
    start_c: float | None = None
    last_c: float | None = None
    peak_c: float | None = None
    samples: int = 0
    errors: int = 0

    @property
    def valid(self) -> bool:
        return self.samples > 0

    def add(self, c: float | None) -> None:
        if c is None:
            return
        self.samples += 1
        if self.start_c is None:
            self.start_c = c
        self.last_c = c
        self.peak_c = c if self.peak_c is None else max(self.peak_c, c)


class ThermalMonitor:
    """Polls R32_ADC_TEMP on a worker thread.

    `on_sample(celsius | None)` fires on every read (throttle the logging in
    the callback, not here).  The board is reopened by its port path after a
    USB re-enumeration; a dead board just records errors and keeps trying."""

    def __init__(self, board: DeviceStatus, interval_s: float = 10.0,
                 cancel: threading.Event | None = None,
                 on_sample: Callable[[float | None], None] | None = None):
        self.board = board
        self.interval_s = max(0.5, float(interval_s))
        self.cancel = cancel
        self.on_sample = on_sample
        self.stats = ThermalStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dev: DdrDevice | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> ThermalStats:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_s))
        self._close()
        return self.stats

    def _close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:                                   # noqa: BLE001
                pass
            self._dev = None

    def _read(self) -> float | None:
        if self._dev is None:
            self._dev = DdrDevice.open(self.board)
        return celsius(self._dev.read(TEMP_INDEX))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                c = self._read()
                self.stats.add(c)
                if self.on_sample is not None:
                    self.on_sample(c)
            except (DdrError, OSError):
                # gone or re-enumerating: drop the handle, try again next tick
                self.stats.errors += 1
                self._close()
            if self._stop.wait(self.interval_s):
                break
            if self.cancel is not None and self.cancel.is_set():
                break
