# Newbies Flasher

A fast, simple Android flashable package maker that turns raw, sparse, or Zstandard-compressed partition images into a single ZIP you can flash from custom recovery or Fastboot on Windows, Linux, macOS, and Termux.

Requires Python 3.8 or newer. No external Python packages are needed.

## What It Does

- Packages `.img`, sparse `.img`, and `.img.zst` files into a hybrid Recovery + Fastboot ZIP.
- Automatically detects Android sparse images and unsparses them before packing so recovery flashes clean raw images.
- Reads Zstandard uncompressed partition sizes directly from RFC 8878 frame headers in Python (no slow full-file scan).
- Compresses partitions in parallel with `zstd` (with XXH64 checksums enabled) and stores `.img.zst` entries uncompressed inside the ZIP so archives are never double-compressed.
- Generates a POSIX `META-INF/com/google/android/update-binary` for custom recovery (TWRP, OrangeFox, PBRP, Lineage Recovery) plus Fastboot scripts for Windows (`flash_windows.bat`), Linux (`flash_linux.sh`), macOS (`flash_macos.sh`), and Android Termux (`flash_termux.sh`).
- Writes a `META-INF/sha256sums.txt` manifest and verifies archive entries before touching any partition.
- Hardened recovery install order: runs pre-flight checks, dynamically unmounts `/proc/mounts` dm devices, runs `lptools unlimited-group`, checks `lptools free` against required space, and allocates/flashes `super` dynamic partitions before touching `boot`, `vbmeta`, or low-level firmware.
- Orders ZIP entries so `META-INF/`, scripts, and small boot/firmware images stay below the 4 GiB ZIP64 offset boundary before large dynamic partition images.
- Unlocks read-only block devices (`blockdev --setrw` and UFS `force_ro`) and verifies target partition capacity before writing.
- Separates low-level bootloader images (`preloader_raw`, `lk`, `xbl`, `abl`, etc.) so you can skip them in one click (`Safe (skip bootloader)` in GUI).
- Supports safe AVB 2.0 flag patching on top-level `vbmeta` only (leaves chained `vbmeta_system` and `vbmeta_vendor` flags at 0 so `libavb` does not reject them).
- Probes bundled binaries before using them and falls back to system `zstd` / `fastboot` or Python `zstandard` automatically on any CPU or OS.

## Quick Start

### Windows

Double-click `run.bat` for the desktop GUI, or `run_cli.bat` for the terminal prompt.

Or run directly:

```cmd
python gui.py
python main.py
```

### Linux and macOS

```bash
chmod +x run.sh
./run.sh
```

`run.sh` opens the desktop GUI when a display and Tkinter are available, or falls back to the terminal prompt. On Debian/Ubuntu, install Tkinter if you want the GUI:

```bash
sudo apt install python3-tk
```

On macOS, install Fastboot and Zstd via Homebrew if you plan to flash from macOS:

```bash
brew install android-platform-tools zstd
```

### Android Termux

```bash
chmod +x run_termux.sh
./run_termux.sh
```

`run_termux.sh` installs `python`, `zstd`, and `android-tools` if missing and starts the CLI.

## GUI Usage

1. Click **Browse...** and pick the folder containing your `.img` or `.img.zst` files.
2. Review the partition list. Click any row (or press Space) to include or exclude specific partitions.
3. Fill in Device Name, Codename, Version, and Maintainer (auto-filled if a `build.prop` is present).
4. Pick AVB mode (`skip`, `disable`, `enable`) and compression levels.
   - ZSTD Level `1` is the best balance of build speed and ZIP size. Set `0` to pack raw `.img` files.
   - ZIP Level only applies to raw images and scripts; `.img.zst` files are always stored uncompressed.
5. Click **Preview Scripts** if you want to inspect the generated `update-binary` and Fastboot scripts first.
6. Click **Build Flashable ZIP**. Finished packages are saved to `output/`.

## CLI Usage

Interactive mode (prompts step-by-step, type `b` to go back):

```bash
python main.py
```

Non-interactive mode:

```bash
python main.py \
  --imgs /path/to/images \
  --device "Infinix GT 20 Pro" \
  --codename "X6871" \
  --version "15.1.2" \
  --maintainer "yourname" \
  --vbmeta skip \
  --zstd-level 1 \
  --zip-level 1
```

Useful flags:

- `--exclude preloader,lk` : skip specific partitions.
- `--keep-super` : keep unrelated stale partitions inside `super` instead of clearing them.
- `--no-fastboot` : omit Fastboot scripts and host binaries from the ZIP.
- `--no-checksum` : omit `META-INF/sha256sums.txt`.
- `--print-scripts` : print all generated installer scripts without building a ZIP.
- `-o /path/to/out.zip` : set a custom output file path.
- `--gui` : open the desktop GUI.

## Flashing the Output ZIP

### Custom Recovery

Copy the `.zip` file to the phone and flash it in TWRP, OrangeFox, PBRP, or Lineage Recovery.

### Fastboot (PC or Termux OTG)

1. Extract the `.zip` file.
2. Boot the phone into Bootloader / Fastboot mode.
3. Run the script for your OS:
   - Windows: `flash_windows.bat`
   - Linux: `chmod +x flash_linux.sh && ./flash_linux.sh`
   - macOS: `chmod +x flash_macos.sh && ./flash_macos.sh`
   - Termux: `chmod +x flash_termux.sh && ./flash_termux.sh`

## Project Layout

```text
Newbies-Flasher/
  bin/
    device/              ARM64 recovery helpers (zstd-arm64, lptools, avbctl)
    linux/               Linux x86_64 fastboot and zstd
    windows/             Windows fastboot.exe, AdbWin DLLs, zstd.exe
  flasher/
    avb.py               AVB 2.0 header reader and top-level vbmeta flag patcher
    builder.py           Staging, unsparsing, parallel zstd, SHA-256 manifest, ZIP writer
    fastboot_scripts.py  Windows, Linux, macOS, and Termux fastboot script generators
    partitions.py        Scanner, RFC 8878 ZSTD size reader, build.prop parser
    platform_utils.py    OS detection and runtime binary probing
    recovery_script.py   POSIX recovery update-binary generator
    sparse.py            Pure-Python Android sparse image parser and unsparser
  tests/
    test_newbies_flasher.py
  gui.py                 Tkinter desktop GUI
  main.py                Interactive and flag-based CLI
  run.bat / run_cli.bat  Windows launchers
  run.sh                 Linux and macOS launcher
  run_termux.sh          Termux launcher
```

## Notes on Bundled Binaries

- `bin/linux/zstd` requires a modern x86_64 CPU (`x86-64-v3`) and Linux kernel 6.1+. On older Linux systems, ARM64 Linux, or macOS, Newbies Flasher detects this at startup and automatically uses the system `zstd` binary or Python `zstandard`.
- `bin/device/lptools` and `bin/device/avbctl` are dynamically linked ARM64 Android binaries; the generated `update-binary` tests them at runtime and falls back to recovery's own `lptools`/`avbctl` if linking fails.

## Running Tests

```bash
python -m unittest discover -s tests -v
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
