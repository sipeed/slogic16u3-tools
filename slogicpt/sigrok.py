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
from .profiles import RESOURCES_DIR, format_rate

_PLATFORM_BINARIES = {
    ("Linux", "x86_64"): "sigrok-cli-linux-x86_64",
    ("Windows", "AMD64"): "sigrok-cli-windows-x86_64.exe",
    ("Darwin", "arm64"): "sigrok-cli-macos-arm64",
}


class CaptureError(Exception):
    pass


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


def _popen_kwargs() -> dict:
    kwargs: dict = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True   # own process group, see kill_tree
    env = os.environ.copy()
    env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")   # FUSE-less fallback
    _strip_pyinstaller_env(env)
    kwargs["env"] = env
    return kwargs


def _strip_pyinstaller_env(env: dict) -> None:
    """When we run frozen (PyInstaller onefile), the bootloader injects
    _PYI_* / _MEIPASS2 markers into the environment and points LD_LIBRARY_PATH
    at our extraction dir.  Hand every external tool a clean environment so it
    resolves its own libraries, not ours.  No-op from source
    (getattr(sys, "frozen") is False), so behaviour there is unchanged.

    NB: this does NOT cure Gowin programmer_cli's "MAINCMD module not found"
    when frozen -- that is an ancestry problem, not an environment one, and is
    fixed by re-parenting the tool via WMI (see winexec.py)."""
    if not getattr(sys, "frozen", False):
        return
    for k in [k for k in env if k.startswith("_PYI_") or k == "_MEIPASS2"]:
        del env[k]
    # PyInstaller overrode these and saved the originals as <VAR>_ORIG
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        orig = env.pop(var + "_ORIG", None)
        if orig is not None:
            env[var] = orig
        else:
            env.pop(var, None)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and its whole process group -- proc.kill() alone leaves
    grandchildren (e.g. `sh -c '...; sleep 30'`) holding the stdout pipe open,
    which keeps our readline blocked long past the timeout."""
    try:
        if os.name == "posix":
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


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
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **_popen_kwargs())
        wrapped_rate: str | None = None
        assert proc.stdout is not None
        with watchdog(proc, timeout_s, cancel) as fate:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                m = re.search(r"wrap to (\S+?Hz)", line, re.IGNORECASE)
                if m:
                    wrapped_rate = m.group(1)
                if log_cb:
                    log_cb(line)
            proc.wait()
        elapsed = time.time() - start
        if fate.killed_by == "cancel":
            raise CaptureError(t("Capture cancelled"))
        if fate.killed_by == "timeout":
            raise CaptureError(t("Capture timed out ({s:.0f}s)").format(s=timeout_s))

        if wrapped_rate is not None:
            raise CaptureError(
                t("Device does not support samplerate {rate} "
                  "(driver wrapped to {wrapped}). "
                  "Please fix the product profile capture.samplerates").format(
                    rate=format_rate(samplerate_hz), wrapped=wrapped_rate))
        if proc.returncode != 0:
            raise CaptureError(t("sigrok-cli exit code {code}").format(
                code=proc.returncode))
        if not out_file.is_file() or out_file.stat().st_size == 0:
            raise CaptureError(t("No capture data file produced: {path}").format(
                path=out_file))

        from .waveform import strip_frame_markers
        payload = strip_frame_markers(out_file.read_bytes())
        n_samples = len(payload) // unitsize
        if n_samples == 0:
            raise CaptureError(t("Capture data is less than one full sample"))
        return CaptureResult(
            out_file=out_file, unitsize=unitsize,
            num_channels=channels, samplerate_hz=samplerate_hz,
            n_samples=n_samples, elapsed_s=elapsed)


if __name__ == "__main__":
    import sys
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

    from .waveform import load_capture_file, verify_channels
    chans = load_capture_file(result.out_file, result.num_channels, result.unitsize)
    e = profile.expected
    ok, verdicts = verify_channels(chans, result.samplerate_hz, e.freq_hz,
                                   e.duty_pct, e.freq_tol_pct, e.duty_tol_pp)
    for v in verdicts:
        f = f"{v.freq_hz/1e6:.4f}MHz" if v.freq_hz else "N/A"
        d = f"{v.duty*100:.2f}%" if v.duty is not None else "N/A"
        print(f"  CH{v.channel}: {f} {d} {'ok' if v.ok else 'FAIL'}")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
