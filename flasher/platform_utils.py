import os
import platform
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"


def detect_host_os() -> str:
    if "TERMUX_VERSION" in os.environ or os.path.isdir("/data/data/com.termux"):
        return "termux"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def host_arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return m or "unknown"


@lru_cache(maxsize=32)
def probe_executable(path: str, test_args: tuple = ("--version",)) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if not sys.platform.startswith("win"):
        try:
            if not os.access(path, os.X_OK):
                os.chmod(path, 0o755)
        except OSError:
            pass
    try:
        proc = subprocess.run(
            [path, *test_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except OSError:
        return False
    except subprocess.SubprocessError:
        return False


def get_host_zstd() -> Optional[str]:
    host = detect_host_os()
    arch = host_arch()

    candidates: List[str] = []
    if host == "windows" and arch in ("x86_64", "arm64"):
        candidates.append(str(BIN_DIR / "windows" / "zstd.exe"))
    elif host == "linux" and arch == "x86_64":
        candidates.append(str(BIN_DIR / "linux" / "zstd"))

    sys_zstd = shutil.which("zstd.exe") if host == "windows" else shutil.which("zstd")
    if sys_zstd:
        candidates.append(sys_zstd)

    for cand in candidates:
        if probe_executable(cand, ("--version",)):
            return cand
    return None


def get_host_fastboot() -> Optional[str]:
    host = detect_host_os()
    arch = host_arch()

    candidates: List[str] = []
    if host == "windows":
        candidates.append(str(BIN_DIR / "windows" / "fastboot.exe"))
    elif host == "linux" and arch == "x86_64":
        candidates.append(str(BIN_DIR / "linux" / "fastboot"))

    sys_fb = shutil.which("fastboot.exe") if host == "windows" else shutil.which("fastboot")
    if sys_fb:
        candidates.append(sys_fb)

    for cand in candidates:
        if probe_executable(cand, ("--version",)):
            return cand
    return None


def has_python_zstandard() -> bool:
    try:
        import zstandard  # noqa: F401
        return True
    except ImportError:
        return False


def get_device_binaries() -> dict:
    dev_dir = BIN_DIR / "device"
    names = ("zstd-arm64", "lptools", "avbctl")
    return {name: dev_dir / name for name in names if (dev_dir / name).is_file()}


def get_fastboot_bundle_assets() -> dict:
    win_dir = BIN_DIR / "windows"
    lin_dir = BIN_DIR / "linux"
    win_files = [
        win_dir / name
        for name in ("fastboot.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "zstd.exe")
        if (win_dir / name).is_file()
    ]
    lin_files = [
        lin_dir / name
        for name in ("fastboot", "zstd")
        if (lin_dir / name).is_file()
    ]
    return {"windows": win_files, "linux": lin_files}


def reveal_in_file_manager(target_path: str) -> None:
    target = os.path.abspath(target_path)
    is_file = os.path.isfile(target)
    folder = os.path.dirname(target) if is_file else target

    if sys.platform == "win32":
        if is_file:
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            os.startfile(folder)
    elif sys.platform == "darwin":
        if is_file:
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])
