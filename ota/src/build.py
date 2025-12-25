import os
import sys
import subprocess
import platform

def build():
    system = platform.system()
    script_name = "spi_flash.py"
    
    if system == "Linux":
        executable_name = "spi_flash_linux"
    elif system == "Windows":
        executable_name = "spi_flash_windows"
    else:
        executable_name = f"spi_flash_{system}"

    print(f"Building for {system}...")
    
    # PyInstaller command
    # Use python -m PyInstaller to ensure we use the installed module
    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--onefile",
        "--clean",
        "--name", executable_name,
        script_name
    ]
    
    try:
        subprocess.check_call(cmd)
        print(f"Build successful! Executable is in the 'dist' folder: dist/{executable_name}")
        if system == "Windows":
             print(f"Note: On Windows, ensure you have the necessary libusb drivers installed or bundled if pyusb requires them.")
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")

if __name__ == "__main__":
    # Ensure pyinstaller is installed
    try:
        import PyInstaller
    except ImportError:
        print("PyInstaller not found. Please install it using: pip install pyinstaller")
        sys.exit(1)
    
    build()
