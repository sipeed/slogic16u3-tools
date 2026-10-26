"""sigrok-cli wrapper for production-test captures.

Empirical notes (verified against sigrok-cli 0.8.0 SLogic build + SLogic16U3):
- channel list must be comma-separated (`D0,D1,...`); `D0-D15` range syntax
  is rejected by this driver;
- `-O binary` output starts with a textual `FRAME-BEGIN\\n` marker;
- the `logic_channels` config selects the channel-group mode and unlocks
  the higher samplerates of reduced groups; it must precede `samplerate`.
  The stream width follows the group: unitsize = ceil(logic_channels/8)
  (32U3 verified: 32ch->4B/sample @200M, 16ch->2B @400M, 8ch->1B @800M);
- an unsupported samplerate is silently wrapped by the driver (stderr says
  e.g. "wrap to 200MHz") -- we treat that as a capture failure, otherwise
  frequency verification would use the wrong sample rate.
"""
from __future__ import annotations

import contextlib
import os
import platform
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .i18n import t
from .procutil import kill_tree, popen_kwargs  # noqa: F401  (re-exported)
from .profiles import RESOURCES_DIR, format_rate, parse_rate

_PLATFORM_BINARIES = {
    ("Linux", "x86_64"): "sigrok-cli-linux-x86_64",
    ("Windows", "AMD64"): "sigrok-cli-windows-x86_64.exe",
}


class CaptureError(Exception):
    """A failed capture.  `output` is what sigrok-cli printed, so callers can
    tell a busy/absent device from a real analyzer fault by its own words."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def find_sigrok_binary(bin_dir: Path = RESOURCES_DIR / "bin") -> Path | None:
    name = _PLATFORM_BINARIES.get((platform.system(), platform.machine()))
    if name is None:
        return None
    path = bin_dir / name
    if not path.is_file():
        return None
    if os.name == "posix" and not os.access(path, os.X_OK):
        return None
    return path


@dataclass(frozen=True)
class CaptureResult:
    out_file: Path
    unitsize: int
    num_channels: int          # channels enabled for this capture
    samplerate_hz: int
    n_samples: int
    elapsed_s: float


# Kept under the old private name for older call sites.
_popen_kwargs = popen_kwargs


# Lines sigrok-cli emits that carry no information about the capture. They are
# normal on every run of this driver, and the fastest configurations emit them
# hundreds of times, which buries the lines that matter.
_NOISE = (
    "waitformultipleobjectsex failed",
    "device instance not active, can't set config",
    "usb link slower than the analyzer",
)


def _is_benign_noise(line: str) -> bool:
    low = line.lower()
    return any(n in low for n in _NOISE)


class _WatchdogFate:
    killed_by: str | None = None   # None | "timeout" | "cancel"


@contextlib.contextmanager
def watchdog(proc: subprocess.Popen, timeout_s: float,
             cancel: threading.Event | None = None, poll_s: float = 0.2):
    """Kill `proc` on deadline or cancel, independent of its output flow.

    A plain `for line in proc.stdout` loop blocks while the child is silent,
    so timeout checks inside the loop only fire when output arrives; this
    sidecar thread enforces them unconditionally.
    """
    fate = _WatchdogFate()
    deadline = time.time() + timeout_s
    done = threading.Event()

    def _watch():
        while not done.wait(poll_s):
            if cancel is not None and cancel.is_set():
                fate.killed_by = "cancel"
            elif time.time() > deadline:
                fate.killed_by = "timeout"
            else:
                continue
            kill_tree(proc)
            return

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    try:
        yield fate
    finally:
        done.set()
        t.join()


class SigrokCli:
    def __init__(self, binary: Path):
        self.binary = Path(binary)

    def _run_lines(self, args: list[str], timeout_s: float) -> list[str]:
        proc = subprocess.run(
            [str(self.binary), *args], capture_output=True, text=True,
            timeout=timeout_s, **_popen_kwargs())
        return (proc.stdout + proc.stderr).splitlines()

    def scan(self, driver: str, timeout_s: float = 20) -> list[str]:
        lines = self._run_lines(["-d", driver, "--scan"], timeout_s)
        return [ln.strip() for ln in lines if ln.strip().startswith(driver)]

    def show(self, driver: str, timeout_s: float = 20) -> str:
        return "\n".join(self._run_lines(["-d", driver, "--show"], timeout_s))

    def capture(self, *, driver: str, channels: int, samplerate_hz: int,
                samples: str, voltage_threshold_v: float,
                out_file: Path, timeout_s: float,
                conn: str | None = None,
                log_cb: Callable[[str], None] | None = None,
                cancel: threading.Event | None = None) -> CaptureResult:
        out_file = Path(out_file)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.unlink(missing_ok=True)

        # `logic_channels` selects the channel-group mode and must be set
        # BEFORE samplerate: higher rates only exist in reduced groups
        # (e.g. 32U3: 32ch@200M / 16ch@400M / 8ch@800M).  The stream width
        # follows the group, so unitsize = ceil(group/8) (verified on real
        # 32U3: 32ch->4B, 16ch->2B, 8ch/4ch->1B per sample).
        unitsize = (channels + 7) // 8
        vt = f"{voltage_threshold_v:.1f}"
        config = (f"logic_channels={channels}"
                  f":samplerate={format_rate(samplerate_hz)}"
                  f":voltage_threshold={vt}-{vt}")
        cmd = [
            str(self.binary),
            "-d", f"{driver}:conn={conn}" if conn else driver,
            "--config", config,
            "--channels", ",".join(f"D{i}" for i in range(channels)),
            "--samples", samples,
            "-O", "binary",
            "-o", str(out_file),
        ]
        if log_cb:
            log_cb("$ " + " ".join(cmd))

        start = time.time()
        # Output goes to a FILE and is read afterwards, NOT through a pipe.
        #
        # WHY: sigrok-cli draws a progress indicator with carriage returns and no
        # newlines. A thread reading that pipe line-by-line therefore yields
        # nothing for the whole capture, the 64 KB pipe buffer fills, and the child
        # blocks forever writing to it -- the capture then only ends when the
        # watchdog kills it, which the operator sees as "capture timed out (120s)"
        # on the fastest configurations. The same trap earlier wedged two matrix
        # runs in this project, and the fixture loader avoids it the same way.
        #
        # A pipe also cannot be given a hard timeout reliably: subprocess.run's
        # timeout does not fire while a grandchild still holds the write end.
        self._capture_log = out_file.with_suffix(out_file.suffix + ".log")
        with open(self._capture_log, "wb") as lf:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    **_popen_kwargs())
            with watchdog(proc, timeout_s, cancel) as fate:
                proc.wait()

        text = ""
        try:
            text = self._capture_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        wrapped_rate: str | None = None
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            m = re.search(r"wrap to (\S+?Hz)", line, re.IGNORECASE)
            if m:
                wrapped_rate = m.group(1)
            if log_cb and not _is_benign_noise(line):
                log_cb(line)
        elapsed = time.time() - start
        if fate.killed_by == "cancel":
            raise CaptureError(t("Capture cancelled"), output=text)
        if fate.killed_by == "timeout":
            raise CaptureError(t("Capture timed out ({s:.0f}s)").format(s=timeout_s),
                               output=text)

        if wrapped_rate is not None:
            raise CaptureError(
                t("Device does not support samplerate {rate} "
                  "(driver wrapped to {wrapped}). "
                  "Please fix the product profile capture.samplerates").format(
                    rate=format_rate(samplerate_hz), wrapped=wrapped_rate), output=text)
        if proc.returncode != 0:
            raise CaptureError(t("sigrok-cli exit code {code}").format(
                code=proc.returncode), output=text)
        if not out_file.is_file() or out_file.stat().st_size == 0:
            raise CaptureError(t("No capture data file produced: {path}").format(
                path=out_file), output=text)

        from .waveform import strip_frame_markers
        payload = strip_frame_markers(out_file.read_bytes())
        n_samples = len(payload) // unitsize
        if n_samples == 0:
            raise CaptureError(t("Capture data is less than one full sample"), output=text)
        # sigrok-cli returns exactly --samples (measured at every size the plan
        # uses); fewer means data was lost on the way and the window is short
        try:
            want = parse_rate(samples)
        except ValueError:
            want = 0
        if want and n_samples < want:
            raise CaptureError(t("Capture came back short: {got} of {want} samples "
                                 "(data lost on the way?)").format(got=n_samples, want=want),
                               output=text)
        return CaptureResult(
            out_file=out_file, unitsize=unitsize,
            num_channels=channels, samplerate_hz=samplerate_hz,
            n_samples=n_samples, elapsed_s=elapsed)


if __name__ == "__main__":
    from .profiles import OUTPUT_DIR, load_profiles

    binary = find_sigrok_binary()
    if binary is None:
        print("未找到 sigrok-cli 二进制（resources/bin/，见 resources/README.md）")
        sys.exit(1)
    print(f"sigrok-cli: {binary}")

    profiles, _ = load_profiles()
    profile = profiles[0]
    print(f"--scan ({profile.driver}):")
    found = SigrokCli(binary).scan(profile.driver)
    for ln in found:
        print(f"  {ln}")
    if not found:
        print("  未发现设备")
        sys.exit(1)

    out = OUTPUT_DIR / "sigrok_selftest.bin"
    cli = SigrokCli(binary)
    result = cli.capture(
        driver=profile.driver, channels=16, samplerate_hz=200_000_000,
        samples="1M", voltage_threshold_v=profile.voltage_threshold_v,
        out_file=out, timeout_s=60,
        log_cb=lambda s: print(f"  | {s}"))
    print(f"采样完成: {result.n_samples} samples, {result.elapsed_s:.2f}s, {result.out_file}")

    ok = result.n_samples > 0
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
