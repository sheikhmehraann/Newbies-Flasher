# Newbies Flasher

A fast, simple Android recovery flashable ZIP maker. It packages raw, sparse, or Zstandard-compressed partition images into a recovery-flashable ZIP on Windows, Linux, macOS, and Android Termux.

Requires Python 3.8 or newer. No external Python packages are needed.

## What It Does

- Packages `.img`, Android sparse `.img`, and `.img.zst` files into a clean recovery flashable ZIP (`META-INF/com/google/android/update-binary`).
- Automatically detects Android sparse images and unsparses them before packing so custom recovery writes clean raw images.
- Reads Zstandard uncompressed partition sizes directly from RFC 8878 frame headers in Python (no slow full-file scan).
- Compresses partitions in parallel with `zstd` (with built-in XXH64 integrity checksums enabled) and stores `.img.zst` entries uncompressed inside the ZIP so archives are never double-compressed.
- Streams unchanged `.img.zst` and raw `.img` files directly into the ZIP without extra temporary copies.
- Hardened recovery install flow for TWRP, OrangeFox, PBRP, and Lineage Recovery:
  - Matches device codenames cleanly across OEM prefixes (for example, `Infinix-X6871` matches `X6871`) and accepts comma-separated codenames.
  - Dynamically unmounts `/proc/mounts` device-mapper entries before editing `super`.
  - Clears stale logical partitions via `lpdump` and `/dev/block/mapper`, removes group size caps with `lptools unlimited-group`, and checks `lptools free` before touching `boot`, `vbmeta`, or firmware.
  - Orders ZIP entries so `META-INF/` and small boot/firmware images stay below the 4 GiB ZIP64 boundary before large dynamic images.
  - Unlocks read-only block devices (`blockdev --setrw` and UFS `force_ro`) and verifies target partition capacity before writing.
  - Reports real byte-weighted recovery progress and total flash time.
- Separates low-level bootloader images (`preloader_raw`, `lk`, `xbl`, `abl`, etc.) so you can skip them in one click (`Safe (skip bootloader)` in GUI).
- Supports safe AVB 2.0 flag patching on top-level `vbmeta` only (leaves chained `vbmeta_system` and `vbmeta_vendor` flags at 0 so `libavb` does not reject them).
- Probes bundled binaries before using them and falls back to system `zstd` or Python `zstandard` automatically on any CPU or OS.

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

On macOS, install Zstd via Homebrew for faster compression:

```bash
brew install zstd
```

### Android Termux

```bash
chmod +x run_termux.sh
./run_termux.sh
```

`run_termux.sh` installs `python` and `zstd` if missing and starts the CLI.

## GUI Usage

1. Click **Browse...** (`Ctrl+O`) and pick the folder containing your `.img` or `.img.zst` files.
2. Review the color-coded partition list. Click any row (or press Space) to include or exclude specific partitions, or click **Safe (skip bootloader)**.
3. Check Device Name, Codename, Version, and Maintainer (defaults to `Infinix GT 20 Pro` / `X6871` as an example, and auto-fills if a `build.prop` is present).
4. Pick AVB mode (`skip`, `disable`, `enable`) and compression levels.
   - ZSTD Level `1` gives the best balance of build speed and ZIP size. Set `0` to pack raw `.img` files.
   - ZIP Level only applies to raw images and scripts; `.img.zst` files are always stored uncompressed.
5. Check the live Output ZIP filename preview (click **Change Folder...** to pick a custom output directory).
6. Click **Preview Script** to inspect or copy the generated `update-binary` before building.
7. Click **Build Flashable ZIP** (`Ctrl+B`).

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

- `--exclude preloader_raw,lk` : skip specific partitions.
- `--keep-super` : keep unrelated stale partitions inside `super` instead of clearing them.
- `--codename ""` : skip the on-device codename check (universal ZIP).
- `--print-script` : print the generated `update-binary` without building a ZIP.
- `-o /path/to/out.zip` : set a custom output file path.
- `--gui` : open the desktop GUI.

## Flashing the Output ZIP

Copy the generated `.zip` file to the phone and flash it in TWRP, OrangeFox, PBRP, or Lineage Recovery. Format Data if switching ROMs or Android versions.

## Project Layout

```text
Newbies-Flasher/
  bin/
    device/              ARM64 recovery helpers (zstd-arm64, lptools, avbctl)
    linux/               Linux x86_64 zstd
    windows/             Windows zstd.exe
  flasher/
    avb.py               AVB 2.0 header reader and top-level vbmeta flag patcher
    builder.py           Staging, unsparsing, parallel zstd, ZIP writer
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
- `bin/device/lptools` and `bin/device/avbctl` are dynamically linked ARM64 Android binaries; the generated `update-binary` checks recovery's native binaries first and falls back to the bundled copies if needed.

## Running Tests

```bash
python -m unittest discover -s tests -v
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
