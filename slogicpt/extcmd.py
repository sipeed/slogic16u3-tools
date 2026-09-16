"""Run an external command step, streaming its output.

Used for the DFU<->APP mode-switch "script" method (e.g. 16U3 external JTAG
reconfig).  Blank-flash and eFuse operations no longer run scripts -- they
are driven directly by the programmer CLI (see slogicpt/programmer.py).

Commands run with cwd = the product directory, so relative paths in argv
resolve against the admin-managed resource directory (never against wherever
the GUI happened to be launched from).
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from .sigrok import _popen_kwargs, watchdog


def run_command(argv: Sequence[str], workdir: Path, timeout_s: float,
                name: str, log_cb: Callable[[str], None],
                cancel: threading.Event | None = None) -> bool:
    """Execute one command; stream output lines to log_cb.  True on rc==0."""
    argv = [str(a) for a in argv]
    log_cb(f"[{name}] $ {' '.join(argv)}  (cwd={workdir})")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(workdir), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, **_popen_kwargs())
    except OSError as e:
        log_cb(f"[{name}] 启动失败: {e}")
        return False

    start = time.time()
    with watchdog(proc, timeout_s, cancel) as fate:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.rstrip():
                log_cb(line.rstrip())
        proc.wait()
    if fate.killed_by:
        log_cb(f"[{name}] "
               f"{'已取消' if fate.killed_by == 'cancel' else f'超时 ({timeout_s:.0f}s)，已终止'}")
        return False
    ok = proc.returncode == 0
    log_cb(f"[{name}] {'OK' if ok else f'失败 (退出码 {proc.returncode})'} "
           f"({time.time() - start:.1f}s)")
    return ok


if __name__ == "__main__":
    # self-test: echo (rc 0) / timeout kill / non-zero exit
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        assert run_command(["sh", "-c", "echo step-one; echo more"], d, 5,
                           "hello", print) is True
        assert run_command(["sh", "-c", "echo start; sleep 30; echo never"], d, 2,
                           "slow", print) is False
        assert run_command(["sh", "-c", "exit 3"], d, 5, "fail", print) is False
        print("extcmd 自测 PASS（echo 成功 / 超时终止 / 非零退出码）")
