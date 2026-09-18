"""One-click platform prerequisites for the SLogic PT tool.

Two platform-specific concerns, each with a symmetric deploy / remove so the
operator never has to open a terminal:

* Linux  -- a udev rule granting the logged-in operator access to the SLogic
  USB device (VID 0x359F) and the FTDI JTAG cable (0x0403:0x6010 used by
  openFPGALoader).  Without it libusb can *enumerate* but ctrl_transfer
  (DFU<->APP), serial reads and flashing fail with LIBUSB_ERROR_ACCESS.
  Needs root -> runs via pkexec (graphical polkit prompt).

* Windows -- binding the FTDI A-channel (interface 0 of 0x0403:0x6010) to the
  WinUSB driver, which openFPGALoader's fast flash path requires.  Done with
  the bundled wdi-simple.exe (libwdi CLI); needs admin -> elevated through a
  UAC prompt.  Remove deletes the WinUSB driver package currently bound to
  that interface and rescans so Windows reinstalls the default FTDI driver.

deploy()/remove() stream progress through the given log callback and return a
bool; callers run them off the UI thread.  requirement() is "" on platforms
that need nothing (macOS), which is the GUI's cue to hide the whole group.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .i18n import t
from .profiles import PLATFORM_KEY, RESOURCES_DIR

# --- identities -------------------------------------------------------------
SLOGIC_VID = "359f"                 # SLogic DFU + APP
FTDI_VID, FTDI_PID = "0403", "6010"  # FT2232 JTAG cable (openFPGALoader/Gowin)
WINUSB_NAME = "USB Serial Converter A (WinUSB)"

# --- Linux udev -------------------------------------------------------------
UDEV_PATH = Path("/etc/udev/rules.d/60-sipeed-slogic.rules")
UDEV_RULES = f"""\
# SLogic logic analyzer -- operator access (installed by the SLogic PT tool)
# SLogic USB device (DFU + APP), VID 0x359F
SUBSYSTEM=="usb", ATTR{{idVendor}}=="{SLOGIC_VID}", MODE="0660", TAG+="uaccess"
# FTDI JTAG cable used by openFPGALoader / Gowin for blank-flash, 0x0403:0x6010
SUBSYSTEM=="usb", ATTR{{idVendor}}=="{FTDI_VID}", ATTR{{idProduct}}=="{FTDI_PID}", MODE="0660", TAG+="uaccess"
"""

# --- Windows WinUSB ---------------------------------------------------------
WDI_EXE = RESOURCES_DIR / "bin" / "openFPGALoader" / "wdi-simple.exe"


# --------------------------------------------------------------------------- #
#  what does this platform need?                                              #
# --------------------------------------------------------------------------- #
def requirement() -> str:
    """'udev' | 'winusb' | '' (nothing needed)."""
    if PLATFORM_KEY == "linux":
        return "udev"
    if PLATFORM_KEY == "windows":
        return "winusb"
    return ""


def title() -> str:
    return {"udev": t("Linux device permissions (udev)"),
            "winusb": t("Windows driver (WinUSB)")}.get(requirement(), "")


def hint() -> str:
    req = requirement()
    if req == "udev":
        return t("Install a udev rule for SLogic (359F) and the FTDI cable "
                 "(0403:6010) for root-free access; deploying pops up an "
                 "authorization dialog, then replug the device to take effect.")
    if req == "winusb":
        return t("Bind the FTDI A-channel (0403:6010 interface 0) to WinUSB, "
                 "required by openFPGALoader's high-speed flashing; deploying "
                 "pops up a UAC authorization dialog.")
    return ""


def status() -> str:
    """Current state: 'deployed' | 'absent' | 'unknown'.

    Linux: the udev rule file either exists or not.  Windows: read the driver
    service actually bound to the FTDI A-channel (interface 0) -- 'WinUSB' means
    deployed.  The query needs no elevation, so button states stay accurate.
    """
    if requirement() == "udev":
        return "deployed" if UDEV_PATH.is_file() else "absent"
    if requirement() == "winusb":
        svc = _winusb_service()
        if svc == "":
            return "unknown"          # device absent / query failed
        return "deployed" if svc.lower() == "winusb" else "absent"
    return "unknown"


# --------------------------------------------------------------------------- #
#  public entry points                                                        #
# --------------------------------------------------------------------------- #
def deploy(log_cb) -> bool:
    req = requirement()
    if req == "udev":
        return _udev(log_cb, install=True)
    if req == "winusb":
        return _winusb_deploy(log_cb)
    log_cb("No environment setup needed on this platform.")
    return True


def remove(log_cb) -> bool:
    req = requirement()
    if req == "udev":
        return _udev(log_cb, install=False)
    if req == "winusb":
        return _winusb_remove(log_cb)
    log_cb("No environment setup needed on this platform.")
    return True


# --------------------------------------------------------------------------- #
#  Linux: udev via pkexec                                                      #
# --------------------------------------------------------------------------- #
def _udev(log_cb, *, install: bool) -> bool:
    if install:
        log_cb(f"Deploying udev rule -> {UDEV_PATH}")
        # heredoc so the rule text is immune to shell quoting
        script = (
            f"install -d -m 0755 {UDEV_PATH.parent} && "
            f"cat > {UDEV_PATH} <<'SLOGIC_EOF'\n{UDEV_RULES}SLOGIC_EOF\n"
            f"udevadm control --reload-rules && udevadm trigger")
    else:
        log_cb(f"Removing udev rule <- {UDEV_PATH}")
        script = (f"rm -f {UDEV_PATH} && "
                  f"udevadm control --reload-rules && udevadm trigger")

    if not shutil.which("pkexec"):
        log_cb("pkexec not found. Run manually with sudo:")
        log_cb(f"    sudo sh -c '{script}'")
        return False

    log_cb("$ pkexec sh -c <script>   (an authorization dialog will appear)")
    try:
        proc = subprocess.run(["pkexec", "sh", "-c", script],
                              capture_output=True, text=True)
    except OSError as e:
        log_cb(f"pkexec failed: {e}")
        return False
    for stream in (proc.stdout, proc.stderr):
        if stream and stream.strip():
            log_cb(stream.strip())
    ok = proc.returncode == 0
    if install:
        log_cb("OK: udev rule deployed. Replug the device to take effect." if ok
               else "FAILED: deploy failed (authorization cancelled or error).")
    else:
        log_cb("OK: udev rule removed." if ok
               else "FAILED: remove failed (authorization cancelled or error).")
    return ok


# --------------------------------------------------------------------------- #
#  Windows: WinUSB via bundled wdi-simple.exe (elevated)                       #
# --------------------------------------------------------------------------- #
def _winusb_deploy(log_cb) -> bool:
    if not WDI_EXE.is_file():
        log_cb(f"{WDI_EXE} not found -- make sure the openFPGALoader bundle "
               "is in place.")
        return False
    # -d: extract the driver payload to an explicit temp dir.  This is the fix
    # for the GUI "Requested resource not found" failure: the elevated child
    # spawned by Start-Process -Verb RunAs inherits cwd C:\Windows\System32, so
    # wdi's default (cwd-relative) 'usb_driver' folder lands under System32,
    # where WOW64 file-system redirection makes wdi write and installer_x64.exe
    # read different paths -> the .inf isn't found and the install fails.  A
    # normal, non-redirected dir avoids that entirely.  (Verified on the bench:
    # cwd=System32 fails "cannot find usb_device.inf", cwd=temp succeeds.)
    # -o makes wdi wait out any driver install Windows is already running on the
    # interface (e.g. re-installing the default FTDI driver right after a remove)
    # before it binds WinUSB.  Even so, wdi can still return a scary "resource
    # not found / busy" while a concurrent Windows install is in flight yet bind
    # WinUSB a moment later, so we IGNORE wdi's exit code and judge by the real
    # driver state: poll the service bound to MI_00 until it becomes WinUSB.
    extract_dir = Path(tempfile.gettempdir()) / "slogic_wdi"
    wdi = (f'"{WDI_EXE}" -v 0x{FTDI_VID} -p 0x{FTDI_PID} -i 0 -t 0 '
           f'-o 15000 -d "{extract_dir}" -n "{WINUSB_NAME}"')
    log_cb("Installing WinUSB on the FTDI A-channel (interface 0)...")
    _run_elevated_bat(wdi, log_cb)
    if _wait_service(lambda s: s == "winusb", log_cb, timeout_s=15):
        log_cb("OK: WinUSB bound to the FTDI A-channel; probing/flashing now "
               "uses cable-index 5 (ftd2xx 4/1 stops working, as expected).")
        return True
    log_cb("FAILED: WinUSB is not bound to interface 0. Unplug and replug the "
           "cable, then deploy again.")
    return False


def _winusb_remove(log_cb) -> bool:
    # Find the oem*.inf currently bound to the FTDI A interface, delete that
    # driver package (/uninstall), then rescan so Windows reinstalls the
    # default FTDI driver.  Best-effort -- if it can't fully revert, the
    # operator can finish in Device Manager.
    # pnputil's own console text is localized (e.g. Chinese on zh-CN Windows)
    # and would arrive as mojibake through the redirected log, so silence it
    # (| Out-Null) and print our own ASCII status from the exit codes instead.
    ps = f"""\
$ErrorActionPreference = 'Continue'
$hw = 'USB\\VID_{FTDI_VID.upper()}&PID_{FTDI_PID.upper()}&MI_00'
$devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object {{ $_.InstanceId -like "$hw*" }}
if (-not $devs) {{ Write-Output 'no FTDI A-channel device present' }}
foreach ($d in $devs) {{
  $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId `
          -KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue).Data
  Write-Output "device $($d.InstanceId) inf=$inf"
  if ($inf -like 'oem*.inf') {{
    pnputil /delete-driver $inf /uninstall /force | Out-Null
    if ($LASTEXITCODE -eq 0) {{ Write-Output "  deleted driver package $inf" }}
    else {{ Write-Output "  pnputil /delete-driver failed (exit $LASTEXITCODE)" }}
  }}
}}
pnputil /scan-devices | Out-Null
Write-Output '  device tree rescanned'
"""
    log_cb("Uninstalling the FTDI A-channel WinUSB driver and rescanning "
           "(restores the default FTDI driver)...")
    ps_path = Path(tempfile.gettempdir()) / "slogic_winusb_remove.ps1"
    try:
        ps_path.write_text(ps, encoding="utf-8")
    except OSError as e:
        log_cb(f"Failed to write temp script: {e}")
        return False
    cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps_path}"'
    _run_elevated_bat(cmd, log_cb)
    # Judge by outcome, not exit code: MI_00 should fall back off WinUSB (to the
    # FTDI driver, or briefly to no driver while it re-enumerates).
    if _wait_service(lambda s: s != "winusb", log_cb, timeout_s=15):
        log_cb("OK: WinUSB removed from interface 0; the default FTDI driver is "
               "restored. Replug the cable if flashing tools don't see it.")
        return True
    log_cb("FAILED: WinUSB is still bound to interface 0 (UAC cancelled or "
           "error); you can remove the driver manually in Device Manager.")
    return False


# --------------------------------------------------------------------------- #
#  Windows: read the driver service bound to the FTDI A-channel (interface 0)  #
# --------------------------------------------------------------------------- #
def _winusb_service() -> str:
    """Service name bound to MI_00, e.g. 'WinUSB' or 'FTDIBUS'; '' if the
    device is absent or the query fails.  Needs no elevation (read-only)."""
    hw = f"USB\\VID_{FTDI_VID.upper()}&PID_{FTDI_PID.upper()}&MI_00"
    ps = (f"$d = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.InstanceId -like '{hw}*' }} | Select-Object -First 1; "
          f"if ($d) {{ (Get-PnpDeviceProperty -InstanceId $d.InstanceId "
          f"-KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data }}")
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or "").strip()


def _wait_service(pred, log_cb, *, timeout_s: int) -> bool:
    """Poll _winusb_service() until pred(service.lower()) holds or we time out.

    Windows may finish the (un)install asynchronously after wdi/pnputil return,
    so we give it a few seconds rather than reading the state just once."""
    deadline = time.monotonic() + timeout_s
    last = None
    while True:
        svc = _winusb_service()
        if svc and svc != last:
            log_cb(f"interface 0 driver service = {svc}")
            last = svc
        if svc and pred(svc.lower()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.5)


def _run_elevated_bat(cmdline: str, log_cb) -> tuple[int, str]:
    """Run cmdline elevated (UAC) and return (exit_code, captured_output).

    Start-Process -Verb RunAs is the only unattended way to raise a UAC
    prompt; the elevated child can't inherit our pipes, so it redirects to a
    temp log we read back.  Requires a foreground desktop session (fine for a
    GUI tool)."""
    tmp = Path(tempfile.gettempdir())
    bat = tmp / "slogic_env.bat"
    logf = tmp / "slogic_env.log"
    try:
        logf.unlink()
    except OSError:
        pass
    # chcp 65001 -> any child that honours the console codepage emits UTF-8,
    # so its output survives the redirected log without mojibake.
    bat.write_text(f'@echo off\r\nchcp 65001 >nul\r\n{cmdline} > "{logf}" 2>&1\r\n',
                   encoding="utf-8")
    ps = (f"$p = Start-Process -FilePath '{bat}' -Verb RunAs -Wait -PassThru; "
          f"exit $p.ExitCode")
    log_cb("Approve the UAC dialog to run as administrator...")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", ps], capture_output=True, text=True)
    except OSError as e:
        log_cb(f"powershell failed: {e}")
        return 1, ""
    out = ""
    try:
        out = logf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    for line in out.splitlines():
        if line.strip():
            log_cb(line.rstrip())
    if proc.stderr and proc.stderr.strip():
        log_cb(proc.stderr.strip())
    return proc.returncode, out
