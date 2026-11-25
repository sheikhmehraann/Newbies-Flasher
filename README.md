# Newbies Flasher

A fast, simple Android recovery flashable ZIP maker. It packages raw, sparse, or Zstandard-compressed partition images into a recovery-flashable ZIP on Windows, Linux, macOS, and Android Termux.

Requires Python 3.8 or newer. No external Python packages are needed.

## What It Does

- Packages `.img`, Android sparse `.img`, and `.img.zst` files into a clean recovery flashable ZIP (`META-INF/com/google/android/update-binary`).
- Automatically detects Android sparse images and streams unsparsed blocks directly into `zstd` without writing multi-GB temporary raw images to disk.
- Reads Zstandard uncompressed partition sizes in one batched `zstd -lv` pass (with multi-frame, streaming, and RFC 8878 header fallbacks).
- Compresses small images in parallel and saturates CPU cores across large partitions with `zstd` (XXH64 checksums enabled), storing `.img.zst` entries uncompressed inside the ZIP so archives are never double-compressed.
- Streams unchanged `.img.zst` and raw `.img` files directly into the ZIP using a reusable 16 MiB I/O buffer without extra temporary copies.
- Names output packages cleanly as `Codename-Version-Flashable.zip` (for example, `X6871-1.0-Flashable.zip`; falls back to `Device-Version-Flashable.zip` if Codename is left blank).
- Universal custom recovery install flow for TWRP, OrangeFox, PBRP, and Lineage Recovery across MediaTek, Qualcomm, Exynos, Tensor, Unisoc, and Kirin devices:
  - Probes working `unzip` (`unzip` / `/system/bin/unzip` / `/sbin/unzip` / `busybox unzip`), handles `OUTFD` fallback and ADB sideload `/sideload/package.zip`.
  - Detects active slot across `ro.boot.slot_suffix`, `ro.boot.slot`, and `/proc/cmdline` (`androidboot.slot_suffix` / `androidboot.slot`) and prints the detected SoC platform.
  - Supports Dynamic Virtual A/B, Dynamic A/B, Dynamic A-only, Retrofit Dynamic (`ro.boot.super_partition`), hybrid physical OEM partitions (`cust`, `prism`, `optics`), and automatic Static GPT fallback on non-dynamic devices.
  - Dynamically unmounts `/proc/mounts` device-mapper entries, detects pending Virtual A/B snapshot merges (`clear-cow` + `snapshotctl`), uses `dmctl` unmap fallback, aligns logical partition sizes to 4096-byte sectors, creates `/dev/block/by-name` compatibility symlinks when missing, and checks `lptools free` before touching `boot`, `vbmeta`, or firmware.
  - Only clears and recreates the dynamic partitions included in the ZIP, and removes group size caps with `lptools unlimited-group`.
  - Orders ZIP entries so `META-INF/` and small boot/firmware images stay below the 4 GiB ZIP64 boundary before large dynamic images.
  - Discovers block devices across all SoC platform trees (`/dev/block/by-name`, `bootdevice`, MTK/QCOM/Exynos/Tensor/Unisoc platform paths, eMMC `mmcblk0boot0`/`boot1`, and deep `find` fallback), unlocks `blockdev --setrw` and `force_ro` (UFS LUNs and eMMC partitions), and verifies partition capacity before writing.
  - Reports byte-weighted recovery progress and total flash time.
- Separates low-level bootloader images across all SoCs (`preloader_raw`, `lk`, `xbl`, `abl`, `sboot`, `splloader`, `uboot`, etc.) so you can skip them in one click (`Skip Bootloader` in GUI).
- Supports AVB 2.0 `skip`, `disable`, and `enable` modes both inside full ROM packages and as standalone flashable ZIPs:
  - Patches top-level `vbmeta` flags only (leaves chained `vbmeta_system`, `vbmeta_vendor`, and `vbmeta_*_dlkm` at 0 so `libavb` does not reject them).
  - Runs live `avbctl get-verity` / `get-verification` status checks on device before applying `--force disable/enable-verity` and `--force disable/enable-verification`, syncs the inactive A/B `vbmeta` slot, and falls back to direct `AVB0` header patching if `avbctl` is unavailable in recovery.
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
2. Review the color-coded partition list (click any column header to sort). Click any row (or press Space) to include or exclude specific partitions, or click **Skip Bootloader**.
3. Check Device Name, Codename, Version, and Maintainer (defaults to `Infinix GT 20 Pro` / `X6871` as an example, and auto-fills if a `build.prop` is present).
4. Pick AVB 2.0 mode (**Skip**, **Disable**, or **Enable**) and adjust the ZSTD / ZIP sliders (drag, click anywhere on the bar, scroll, or use `-` / `+`).
   - Selecting **Disable** or **Enable** with no partition images builds a lightweight standalone flashable ZIP (`Codename-Version-Flashable.zip`).
   - ZSTD Level `1` gives the best balance of build speed and ZIP size. Set `0` to pack raw `.img` files.
   - ZIP Level applies to scripts when ZSTD is enabled, or to raw `.img` files when ZSTD is `0`.
5. Check the **Output Folder** (type/paste a path or click **Browse...**) and the live `Codename-Version-Flashable.zip` filename preview.
6. Click **Preview Script** (`Ctrl+P`) to inspect, copy, or save the generated `update-binary` before building.
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
- `--codename ""` : skip the on-device codename check (universal ZIP).
- `--print-script` : print the generated `update-binary` without building a ZIP.
- `-O /path/to/folder` (`--output-dir`) : set the output folder (ZIP is auto-named `Codename-Version-Flashable.zip`).
- `-o /path/to/out.zip` : set a custom output file path (or existing folder).
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
    builder.py           Streaming sparse->zstd pipeline, parallel compression, ZIP writer
    partitions.py        Single-open scanner, RFC 8878 ZSTD size reader, build.prop parser
    platform_utils.py    OS detection and runtime binary probing
    recovery_script.py   POSIX recovery update-binary generator
    sparse.py            Pure-Python Android sparse image parser and streaming unsparser
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
