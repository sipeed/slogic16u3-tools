"""Run blank-board flashing steps declared in a manifest.toml.

Commands run with cwd = the manifest's directory, so relative paths in
argv resolve against the admin-managed resource directory (never against
wherever the GUI happened to be launched from).
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .profiles import BlankFlashStep
from .sigrok import _popen_kwargs, watchdog


def run_step(step: BlankFlashStep, workdir: Path,
             log_cb: Callable[[str], None],
             cancel: threading.Event | None = None) -> bool:
    """Execute one step; stream output lines to log_cb.  True on rc==0."""
    log_cb(f"[blank_flash:{step.name}] $ {' '.join(step.argv)}  (cwd={workdir})")
    try:
        proc = subprocess.Popen(
            step.argv, cwd=str(workdir), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, **_popen_kwargs())
    except OSError as e:
        log_cb(f"[blank_flash:{step.name}] 启动失败: {e}")
        return False

    start = time.time()
    with watchdog(proc, step.timeout_s, cancel) as fate:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.rstrip():
                log_cb(line.rstrip())
        proc.wait()
    if fate.killed_by:
        log_cb(f"[blank_flash:{step.name}] "
               f"{'已取消' if fate.killed_by == 'cancel' else f'超时 ({step.timeout_s:.0f}s)，已终止'}")
        return False
    ok = proc.returncode == 0
    log_cb(f"[blank_flash:{step.name}] {'OK' if ok else f'失败 (退出码 {proc.returncode})'} "
           f"({time.time() - start:.1f}s)")
    return ok


if __name__ == "__main__":
    # self-test with a fake manifest (echo + sleep + failing step)
    import tempfile
    from .profiles import load_manifest

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "manifest.toml").write_text("""
schema_version = 1
[[steps]]
name = "hello"
label = "Hello"
argv = ["sh", "-c", "echo step-one; echo more"]
timeout_s = 5
in_pipeline = true
[[steps]]
name = "slow"
label = "Slow"
argv = ["sh", "-c", "echo start; sleep 30; echo never"]
timeout_s = 2
in_pipeline = true
[[steps]]
name = "fail"
label = "Fail"
argv = ["sh", "-c", "exit 3"]
timeout_s = 5
in_pipeline = false
""", encoding="utf-8")
        steps, probe, errs = load_manifest(d)
        assert steps is not None and not errs, (steps, errs)
        results = {s.name: run_step(s, d, print) for s in steps}
        print("results:", results)
        assert results == {"hello": True, "slow": False, "fail": False}
        print("blank_flash 自测 PASS（echo 成功 / 超时终止 / 非零退出码）")
