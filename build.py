"""Build the production-test GUI into a single distributable binary.

Distribution layout (resources stay OUTSIDE the binary so admins can
update product profiles / firmware / flashing scripts without repacking):

    SLogicPT/
    ├── slogic-pt-<platform>[.exe]   <- this build's output (dist/)
    ├── resources/                   <- copy from repo, admin-maintained
    └── out/                         <- created at runtime (captures)

Usage:  python build.py        (run inside the project venv)
Windows note: pyusb needs libusb-1.0.dll, which PyInstaller can't auto-detect
(it isn't a Python module).  This script bundles it automatically when found
in resources/bin/ or on the system; otherwise ship it next to the exe.
PyInstaller cannot cross-build -- run this on each target platform.
Requires Python 3.11+ (the app depends on the stdlib tomllib).
"""
import os
import platform
import subprocess
import sys
from pathlib import Path

# CI and legacy Windows consoles default to a code page (e.g. cp1252) that can't
# encode our Chinese status lines, which would abort the build on print() even
# after the binary built fine.  Force UTF-8 so output never crashes the build.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent

NAME_BY_PLATFORM = {
    ("Linux", "x86_64"): "slogic-pt-linux-x86_64",
    ("Windows", "AMD64"): "slogic-pt-windows-x86_64",
    ("Darwin", "arm64"): "slogic-pt-macos-arm64",
}


def _windows_libusb_dll() -> Path | None:
    """Locate libusb-1.0.dll to bundle (pyusb's backend).  Prefer the
    admin-placed copy in resources/bin/, then whatever ctypes would load."""
    cand = REPO / "resources" / "bin" / "libusb-1.0.dll"
    if cand.is_file():
        return cand
    import ctypes.util
    found = (ctypes.util.find_library("libusb-1.0")
             or ctypes.util.find_library("libusb"))
    if found:
        p = Path(found)
        if p.is_file():
            return p
        for d in os.environ.get("PATH", "").split(os.pathsep):
            q = Path(d) / found
            if q.is_file():
                return q
    return None


def build() -> int:
    key = (platform.system(), platform.machine())
    name = NAME_BY_PLATFORM.get(key, f"slogic-pt-{key[0].lower()}-{key[1]}")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--clean", "--noconfirm",
        "--name", name,
        "--paths", str(REPO),
        str(REPO / "slogicpt" / "__main__.py"),
    ]
    if platform.system() in ("Windows", "Darwin"):
        cmd.insert(cmd.index("--name"), "--windowed")
    if platform.system() == "Windows":
        dll = _windows_libusb_dll()
        if dll is not None:
            # --add-binary "src<sep>." puts the DLL at the bundle root, where
            # the onefile _MEIPASS dir is on pyusb's DLL search path at runtime
            cmd[cmd.index("--name"):cmd.index("--name")] = [
                "--add-binary", f"{dll}{os.pathsep}."]
            print(f"bundling libusb backend: {dll}")
        else:
            print("警告: 未找到 libusb-1.0.dll —— pyusb 打包后可能无法枚举设备。"
                  "把它放到 resources/bin/ 或安装到系统后重打包，"
                  "否则分发时须手动放到 exe 同级。")
    print("$", " ".join(cmd))
    try:
        subprocess.check_call(cmd, cwd=str(REPO))
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        return 1
    dist = REPO / "dist" / (name + (".exe" if platform.system() == "Windows" else ""))
    print(f"\nBuild OK: {dist}")
    print("分发时把二进制与 resources/ 放在同一层（见本文件顶部注释），"
          "resources/ 由管理员按 resources/README.md 维护。")
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        # freeze with 3.11+ or the exe crashes on `import tomllib` at startup
        print(f"需要 Python 3.11+（应用依赖标准库 tomllib），"
              f"当前为 {sys.version.split()[0]}。请用 3.11+ 解释器重打包。")
        sys.exit(1)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("请先安装 PyInstaller: pip install pyinstaller")
        sys.exit(1)
    sys.exit(build())
