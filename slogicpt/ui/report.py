"""The session report shown in the Report tab (plain text, pasted into
feedback as-is)."""
from __future__ import annotations

import time

from .. import version
from ..i18n import t
from ..pipeline import StepStatus

# English keys; translated where read so a language switch picks them up.
STATUS_TEXT = {
    StepStatus.PENDING: "Pending",
    StepStatus.RUNNING: "Running…",
    StepStatus.WAITING_USER: "Waiting",
    StepStatus.PASSED: "Pass",
    StepStatus.FAILED: "Fail",
}


def render(ok: bool, detail: str, elapsed: float, profile,
           devices: list, steps: list[tuple[StepStatus, str, float | None, int]]) -> str:
    """steps: (status, "n. name", duration, operator retries) in run order."""
    lines = [
        "==== " + t("SLogic Production Test Report") + " ====",
        t("Time: {ts}").format(ts=time.strftime('%Y-%m-%d %H:%M:%S')),
        t("Program: {v}").format(v=version.get()),
        (t("Product: {name} ({id})").format(name=profile.display_name, id=profile.id)
         if profile else t("Product: -")),
        t("Device: {devs}").format(
            devs=', '.join(str(d) for d in devices) or t("not detected")),
        t("Overall: {result}  (elapsed {s:.1f}s)").format(
            result='PASS' if ok else 'FAIL', s=elapsed),
        "",
        "-- " + t("Step Status") + " --",
    ]
    for status, name, duration, retries in steps:
        dur = f" ({duration:.1f}s)" if duration else ""
        again = t(" (retried {n}x)").format(n=retries) if retries else ""
        lines.append(f"[{t(STATUS_TEXT[status])}] {name}{dur}{again}")
    if detail:
        lines += ["", "-- " + t("Details") + " --", detail]
    lines += ["", t("To report an issue, copy this report and attach the "
                    "relevant run-log excerpts.")]
    return "\n".join(lines)
