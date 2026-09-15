"""Build the production-test GUI into a single distributable binary.

Distribution layout (resources stay OUTSIDE the binary so admins can
update product profiles / firmware / flashing scripts without repacking):

    SLogicPT/
    ├── slogic-pt-<platform>[.exe]   <- this build's output
    ├── resources/                   <- copy from repo, admin-maintained
    │   ├── products/*.toml
    │   ├── firmware/<id>/app.bin
    │   ├── blank_flash/<id>/...
    │   └── bin/sigrok-cli-<platform>
    └── out/                         <- created at runtime (captures)

Usage:  python pt/build.py        (run inside the project venv)
Windows note: pyusb needs libusb-1.0.dll available (same requirement as
running from source); ship it next to the exe or install the driver via
the usual production-station setup.
"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PT_SRC = Path(__file__).resolve().parent / "src"
REPO = Path(__file__).resolve().parents[1]

NAME_BY_PLATFORM = {
    ("Linux", "x86_64"): "slogic-pt-linux-x86_64",
    ("Windows", "AMD64"): "slogic-pt-windows-x86_64",
    ("Darwin", "arm64"): "slogic-pt-macos-arm64",
}


def build() -> int:
    key = (platform.system(), platform.machine())
    name = NAME_BY_PLATFORM.get(key, f"slogic-pt-{key[0].lower()}-{key[1]}")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--clean", "--noconfirm",
        "--name", name,
        "--paths", str(PT_SRC),
        "--paths", str(REPO / "ota" / "src"),
        # flasher.py imports these via a runtime sys.path insert, which
        # PyInstaller cannot trace -- declare them explicitly
        "--hidden-import", "spi_flash",
        "--hidden-import", "spi_device",
        "--hidden-import", "usb_device",
        "--hidden-import", "spi_data_packet",
        str(PT_SRC / "gui.py"),
    ]
    if platform.system() in ("Windows", "Darwin"):
        cmd.insert(cmd.index("--name"), "--windowed")
    print("$", " ".join(cmd))
    try:
        subprocess.check_call(cmd, cwd=str(REPO / "pt"))
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
        return 1
    dist = REPO / "pt" / "dist" / (name + (".exe" if platform.system() == "Windows" else ""))
    print(f"\nBuild OK: {dist}")
    print("分发时把二进制与 resources/ 目录放在同一层（见本文件顶部注释的目录布局），")
    print("resources/ 内容（产品档案/固件/刷机脚本/sigrok-cli）由管理员按 resources/README.md 放置。")
    return 0


if __name__ == "__main__":
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("请先安装 PyInstaller: pip install pyinstaller")
        sys.exit(1)
    sys.exit(build())
