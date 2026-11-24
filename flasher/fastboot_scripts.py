import re
import shlex
from typing import List


def _bat_safe(text: str) -> str:
    # Strip batch control metacharacters so echoed labels cannot break cmd parsing.
    return re.sub(r'[&|<>^%"!()\r\n]+', " ", str(text or "")).strip()


def _sh_q(text: str) -> str:
    return shlex.quote(str(text or ""))


def _win_flash_block(part: str, use_zstd: bool) -> List[str]:
    if use_zstd:
        return [
            f'if exist "{part}.img.zst" (',
            f"    echo Flashing {part}...",
            f'    "%ZSTD%" -d -q -f -T0 "{part}.img.zst" -o "{part}.img"',
            f'    if errorlevel 1 ( echo [ERROR] Failed to decompress {part}.img.zst & pause & exit /b 1 )',
            f'    "%FASTBOOT%" flash "{part}" "{part}.img"',
            f'    if errorlevel 1 ( del /f /q "{part}.img" >nul 2>&1 & echo [ERROR] fastboot flash failed for {part} & pause & exit /b 1 )',
            f'    del /f /q "{part}.img" >nul 2>&1',
            f') else if exist "{part}.img" (',
            f"    echo Flashing {part}...",
            f'    "%FASTBOOT%" flash "{part}" "{part}.img"',
            f'    if errorlevel 1 ( echo [ERROR] fastboot flash failed for {part} & pause & exit /b 1 )',
            ")",
        ]
    return [
        f'if exist "{part}.img" (',
        f"    echo Flashing {part}...",
        f'    "%FASTBOOT%" flash "{part}" "{part}.img"',
        f'    if errorlevel 1 ( echo [ERROR] fastboot flash failed for {part} & pause & exit /b 1 )',
        ")",
    ]


def generate_windows_fastboot(
    device: str,
    codename: str,
    firmware_imgs: List[str],
    bootchain_imgs: List[str],
    dynamic_imgs: List[str],
    super_raw: bool = False,
    use_zstd: bool = True,
) -> str:
    dev_s = _bat_safe(device)
    code_s = _bat_safe(codename)

    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        'cd /d "%~dp0"',
        "title Newbies Flasher - Fastboot Installer (Windows)",
        'set "FASTBOOT=bin\\windows\\fastboot.exe"',
        'if not exist "%FASTBOOT%" (',
        '    set "FASTBOOT=fastboot.exe"',
        "    where fastboot.exe >nul 2>nul || (",
        "        echo [ERROR] fastboot.exe not found in bin\\windows or PATH.",
        "        pause",
        "        exit /b 1",
        "    )",
        ")",
        'set "ZSTD=bin\\windows\\zstd.exe"',
        'if not exist "%ZSTD%" set "ZSTD=zstd.exe"',
        "echo ==========================================",
        "echo       Newbies Flasher (Windows)",
        "echo ==========================================",
        f"echo Target: {dev_s} ({code_s or 'universal'})",
        "echo.",
        '"%FASTBOOT%" devices',
        "echo.",
    ]

    if code_s:
        lines.extend([
            f'"%FASTBOOT%" getvar product 2>&1 | findstr /i "{code_s}" >nul',
            "if errorlevel 1 (",
            f"    echo [WARNING] Connected device does not match expected codename ({code_s}).",
            '    set /p CONT="Continue anyway? (y/n): "',
            '    if /i "!CONT!" neq "y" exit /b 1',
            ")",
            "echo.",
        ])

    lines.append('set /p WIPE="Erase userdata and metadata after flashing? (y/n): "')
    lines.append("echo.")

    if firmware_imgs:
        lines.append("echo Flashing firmware partitions...")
        for fw in firmware_imgs:
            lines.extend(_win_flash_block(fw, use_zstd))
        lines.append("echo.")

    if bootchain_imgs:
        lines.append("echo Flashing bootchain partitions...")
        for part in bootchain_imgs:
            lines.extend(_win_flash_block(part, use_zstd))
        lines.append("echo.")

    if super_raw:
        lines.append("echo Flashing super image...")
        lines.extend(_win_flash_block("super", use_zstd))
        lines.append("echo.")

    if dynamic_imgs:
        lines.extend([
            "echo Rebooting to fastbootd for dynamic partitions...",
            '"%FASTBOOT%" reboot fastboot',
            "if errorlevel 1 ( echo [ERROR] Failed to reboot to fastbootd & pause & exit /b 1 )",
            "echo Waiting for fastbootd...",
            "set /a WAIT_TRIES=0",
            ":wait_fastbootd",
            "set /a WAIT_TRIES+=1",
            '"%FASTBOOT%" getvar is-userspace 2>&1 | findstr /i "yes" >nul',
            "if not errorlevel 1 goto fastbootd_ready",
            "if !WAIT_TRIES! geq 15 ( echo [ERROR] Timed out waiting for fastbootd & pause & exit /b 1 )",
            "timeout /t 2 /nobreak >nul",
            "goto wait_fastbootd",
            ":fastbootd_ready",
            '"%FASTBOOT%" snapshot-update cancel >nul 2>&1',
            'set "OTHER_SLOT="',
            'for /f "tokens=2" %%s in (\'"%FASTBOOT%" getvar current-slot 2^>^&1 ^| findstr /i "current-slot:"\') do (',
            '    if /i "%%s"=="a" set "OTHER_SLOT=_b"',
            '    if /i "%%s"=="b" set "OTHER_SLOT=_a"',
            ")",
            "echo Freeing inactive slot space and flashing dynamic partitions...",
        ])
        for sp in dynamic_imgs:
            lines.append(f'if defined OTHER_SLOT "%FASTBOOT%" resize-logical-partition "{sp}!OTHER_SLOT!" 0 >nul 2>&1')
            lines.extend(_win_flash_block(sp, use_zstd))
        lines.append("echo.")

    lines.extend([
        'if /i "%WIPE%"=="y" (',
        "    echo Erasing userdata and metadata...",
        '    "%FASTBOOT%" erase userdata',
        '    "%FASTBOOT%" erase metadata',
        ")",
        "echo Rebooting to system...",
        '"%FASTBOOT%" reboot',
        "echo ==========================================",
        "echo            Flashing Complete",
        "echo ==========================================",
        "pause",
        "exit /b 0",
    ])
    return "\r\n".join(lines) + "\r\n"


def _unix_flash_block(part: str, use_zstd: bool) -> List[str]:
    p_q = _sh_q(part)
    if use_zstd:
        return [
            f'if [ -f "{part}.img.zst" ]; then',
            f'    echo "Flashing {part}..."',
            f'    "$ZSTD" -d -q -f -T0 "{part}.img.zst" -o "{part}.img"',
            f'    "$FASTBOOT" flash {p_q} "{part}.img"',
            f'    rm -f "{part}.img"',
            f'elif [ -f "{part}.img" ]; then',
            f'    echo "Flashing {part}..."',
            f'    "$FASTBOOT" flash {p_q} "{part}.img"',
            "fi",
        ]
    return [
        f'if [ -f "{part}.img" ]; then',
        f'    echo "Flashing {part}..."',
        f'    "$FASTBOOT" flash {p_q} "{part}.img"',
        "fi",
    ]


def _generate_unix_fastboot(
    target_os: str,
    device: str,
    codename: str,
    firmware_imgs: List[str],
    bootchain_imgs: List[str],
    dynamic_imgs: List[str],
    super_raw: bool = False,
    use_zstd: bool = True,
) -> str:
    shebang = "#!/data/data/com.termux/files/usr/bin/bash" if target_os == "termux" else "#!/usr/bin/env bash"
    label = {"linux": "Linux", "macos": "macOS", "termux": "Termux"}[target_os]

    lines = [
        shebang,
        "set -euo pipefail",
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'cd "$SCRIPT_DIR"',
        "",
    ]

    if target_os == "linux":
        lines.extend([
            'FASTBOOT="bin/linux/fastboot"',
            'if [ -f "$FASTBOOT" ]; then chmod +x "$FASTBOOT" 2>/dev/null || true; fi',
            'if ! "$FASTBOOT" --version >/dev/null 2>&1; then FASTBOOT="fastboot"; fi',
            'command -v "$FASTBOOT" >/dev/null 2>&1 || { echo "[ERROR] fastboot not found"; exit 1; }',
            "",
            'ZSTD="bin/linux/zstd"',
            'if [ -f "$ZSTD" ]; then chmod +x "$ZSTD" 2>/dev/null || true; fi',
            'if ! "$ZSTD" --version >/dev/null 2>&1; then ZSTD="zstd"; fi',
        ])
    elif target_os == "macos":
        lines.extend([
            'FASTBOOT="fastboot"',
            'command -v "$FASTBOOT" >/dev/null 2>&1 || { echo "[ERROR] fastboot not found. Install with: brew install android-platform-tools"; exit 1; }',
            'ZSTD="zstd"',
        ])
    else:
        lines.extend([
            'if ! command -v fastboot >/dev/null 2>&1; then',
            '    echo "Installing android-tools..."',
            "    pkg update -y && pkg install -y android-tools",
            "fi",
            'if ! command -v zstd >/dev/null 2>&1; then',
            '    echo "Installing zstd..."',
            "    pkg install -y zstd",
            "fi",
            'FASTBOOT="fastboot"',
            'ZSTD="zstd"',
        ])

    if use_zstd and target_os != "termux":
        lines.append('command -v "$ZSTD" >/dev/null 2>&1 || { echo "[ERROR] zstd not found"; exit 1; }')

    lines.extend([
        "",
        'echo "=========================================="',
        f'echo "      Newbies Flasher ({label})"',
        'echo "=========================================="',
        f'printf "Target: %s (%s)\\n\\n" {_sh_q(device)} {_sh_q(codename or "universal")}',
        '"$FASTBOOT" devices',
        'echo ""',
    ])

    if codename:
        lines.extend([
            'PRODUCT=$("$FASTBOOT" getvar product 2>&1 | awk \'/product:/ {print $2; exit}\' || true)',
            f'EXPECTED={_sh_q(codename)}',
            'if [ -n "$PRODUCT" ] && [ "$(printf \'%s\' "$PRODUCT" | tr \'A-Z\' \'a-z\')" != "$(printf \'%s\' "$EXPECTED" | tr \'A-Z\' \'a-z\')" ]; then',
            '    echo "[WARNING] Device mismatch: found $PRODUCT, expected $EXPECTED"',
            '    read -r -p "Continue anyway? (y/n): " CONT',
            '    [ "$CONT" = "y" ] || [ "$CONT" = "Y" ] || exit 1',
            "fi",
            'echo ""',
        ])

    lines.extend([
        'read -r -p "Erase userdata and metadata after flashing? (y/n): " WIPE',
        'echo ""',
    ])

    if firmware_imgs:
        lines.append('echo "Flashing firmware partitions..."')
        for fw in firmware_imgs:
            lines.extend(_unix_flash_block(fw, use_zstd))
        lines.append('echo ""')

    if bootchain_imgs:
        lines.append('echo "Flashing bootchain partitions..."')
        for part in bootchain_imgs:
            lines.extend(_unix_flash_block(part, use_zstd))
        lines.append('echo ""')

    if super_raw:
        lines.append('echo "Flashing super image..."')
        lines.extend(_unix_flash_block("super", use_zstd))
        lines.append('echo ""')

    if dynamic_imgs:
        lines.extend([
            'echo "Rebooting to fastbootd for dynamic partitions..."',
            '"$FASTBOOT" reboot fastboot',
            'echo "Waiting for fastbootd..."',
            "ready=0",
            "tries=0",
            'while [ "$tries" -lt 15 ]; do',
            "    tries=$((tries + 1))",
            '    if "$FASTBOOT" getvar is-userspace 2>&1 | grep -qi "yes"; then ready=1; break; fi',
            "    sleep 2",
            "done",
            '[ "$ready" -eq 1 ] || { echo "[ERROR] Timed out waiting for fastbootd"; exit 1; }',
            '"$FASTBOOT" snapshot-update cancel >/dev/null 2>&1 || true',
            'CUR_SLOT=$("$FASTBOOT" getvar current-slot 2>&1 | awk \'/current-slot:/ {print $2; exit}\' || true)',
            'OTHER_SLOT=""',
            '[ "$CUR_SLOT" = "a" ] && OTHER_SLOT="_b"',
            '[ "$CUR_SLOT" = "b" ] && OTHER_SLOT="_a"',
            'echo "Freeing inactive slot space and flashing dynamic partitions..."',
        ])
        for sp in dynamic_imgs:
            lines.append(f'[ -n "$OTHER_SLOT" ] && "$FASTBOOT" resize-logical-partition "{sp}${{OTHER_SLOT}}" 0 >/dev/null 2>&1 || true')
            lines.extend(_unix_flash_block(sp, use_zstd))
        lines.append('echo ""')

    lines.extend([
        'if [ "$WIPE" = "y" ] || [ "$WIPE" = "Y" ]; then',
        '    echo "Erasing userdata and metadata..."',
        '    "$FASTBOOT" erase userdata || true',
        '    "$FASTBOOT" erase metadata || true',
        "fi",
        'echo "Rebooting to system..."',
        '"$FASTBOOT" reboot',
        'echo "=========================================="',
        'echo "           Flashing Complete"',
        'echo "=========================================="',
        "",
    ])
    return "\n".join(lines)


def generate_linux_fastboot(**kwargs) -> str:
    return _generate_unix_fastboot("linux", **kwargs)


def generate_macos_fastboot(**kwargs) -> str:
    return _generate_unix_fastboot("macos", **kwargs)


def generate_termux_fastboot(**kwargs) -> str:
    return _generate_unix_fastboot("termux", **kwargs)
