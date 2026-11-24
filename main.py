#!/usr/bin/env python3
import argparse
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from flasher import __version__
from flasher.builder import (
    BuildConfig,
    BuildResult,
    build_package,
    format_output_filename,
    preview_scripts,
)
from flasher.partitions import detect_build_props, scan_partitions
from flasher.platform_utils import detect_host_os, get_host_zstd, has_python_zstandard


def clean_path(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    if not s:
        return ""
    return os.path.abspath(os.path.expanduser(s))


def print_summary(res: BuildResult) -> None:
    print("\n========================================================================")
    print("                     Newbies Flasher Build Summary")
    print("========================================================================")
    print(f"Device       : {res.device}")
    if res.codename:
        print(f"Codename     : {res.codename}")
    print(f"Version      : {res.version}")
    print(f"Maintainer   : {res.maintainer}")
    print(f"AVB 2.0      : {res.vbmeta_mode}")
    print(f"ZSTD Level   : {res.zstd_level}")
    print(f"ZIP Level    : {res.zip_level}")
    print("-" * 72)
    total = (
        len(res.dynamic_partitions)
        + len(res.bootchain_partitions)
        + len(res.firmware_partitions)
        + len(res.bootloader_partitions)
        + (1 if res.super_raw else 0)
    )
    print(f"Partitions   : {total}")
    if res.super_raw:
        print("  - Super    : 1 (super)")
    if res.dynamic_partitions:
        print(f"  - Dynamic  : {len(res.dynamic_partitions)} ({', '.join(res.dynamic_partitions)})")
    if res.bootchain_partitions:
        print(f"  - Boot     : {len(res.bootchain_partitions)} ({', '.join(res.bootchain_partitions)})")
    if res.firmware_partitions:
        print(f"  - Firmware : {len(res.firmware_partitions)} ({', '.join(res.firmware_partitions)})")
    if res.bootloader_partitions:
        print(f"  - Bootldr  : {len(res.bootloader_partitions)} ({', '.join(res.bootloader_partitions)})")
    print("-" * 72)
    print(f"Output File  : {res.output_zip}")
    print(f"Size         : {res.size_mb:.2f} MB")
    print(f"Build Time   : {res.elapsed_sec:.1f} s")
    print("========================================================================")


def interactive_flow() -> None:
    print("========================================================================")
    print(f"                        Newbies Flasher v{__version__}")
    print("========================================================================")
    print(f"Host OS: {detect_host_os()} | zstd: {'ready' if (get_host_zstd() or has_python_zstandard()) else 'not found'}")
    print("Type 'b' at any prompt to go back.\n")

    state = {
        "imgs_dir": "",
        "partitions": [],
        "device": "Android Device",
        "codename": "",
        "version": "1.0",
        "vbmeta": "skip",
        "maintainer": "User",
        "zstd_level": 1,
        "zip_level": 1,
    }

    step = 1
    return_to_review = False

    try:
        while True:
            if step == 1:
                raw = input("Images folder path : ").strip()
                if raw.lower() in ("b", "back"):
                    continue
                path_cand = clean_path(raw) if raw else state["imgs_dir"]
                if not path_cand or not os.path.isdir(path_cand):
                    print(f"Folder not found: {path_cand or raw}\n")
                    continue
                parts = scan_partitions(path_cand)
                if not parts:
                    print(f"No .img or .img.zst files found in: {path_cand}\n")
                    continue

                state["imgs_dir"] = path_cand
                state["partitions"] = parts
                print(f"Found {len(parts)} partition image(s).")

                props = detect_build_props(path_cand)
                if props["device"] and state["device"] == "Android Device":
                    state["device"] = props["device"]
                if props["codename"] and not state["codename"]:
                    state["codename"] = props["codename"]
                if props["version"] and state["version"] == "1.0":
                    state["version"] = props["version"]

                step = 9 if return_to_review else 2
                return_to_review = False

            elif step == 2:
                val = input(f"Device name ({state['device']}) : ").strip()
                if val.lower() in ("b", "back"):
                    step = 9 if return_to_review else 1
                    return_to_review = False
                    continue
                if val:
                    state["device"] = val
                step = 9 if return_to_review else 3
                return_to_review = False

            elif step == 3:
                cur = state["codename"] or "optional"
                val = input(f"Codename ({cur}) : ").strip()
                if val.lower() in ("b", "back"):
                    step = 9 if return_to_review else 2
                    return_to_review = False
                    continue
                if val:
                    state["codename"] = val
                step = 9 if return_to_review else 4
                return_to_review = False

            elif step == 4:
                val = input(f"ROM version ({state['version']}) : ").strip()
                if val.lower() in ("b", "back"):
                    step = 9 if return_to_review else 3
                    return_to_review = False
                    continue
                if val:
                    state["version"] = val
                step = 9 if return_to_review else 5
                return_to_review = False

            elif step == 5:
                val = input(f"AVB 2.0 vbmeta [skip/disable/enable] ({state['vbmeta']}) : ").strip().lower()
                if val in ("b", "back"):
                    step = 9 if return_to_review else 4
                    return_to_review = False
                    continue
                if val in ("skip", "s", "1"):
                    state["vbmeta"] = "skip"
                elif val in ("disable", "d", "2"):
                    state["vbmeta"] = "disable"
                elif val in ("enable", "e", "3"):
                    state["vbmeta"] = "enable"
                elif val:
                    print("Enter skip, disable, or enable.")
                    continue
                step = 9 if return_to_review else 6
                return_to_review = False

            elif step == 6:
                val = input(f"Maintainer ({state['maintainer']}) : ").strip()
                if val.lower() in ("b", "back"):
                    step = 9 if return_to_review else 5
                    return_to_review = False
                    continue
                if val:
                    state["maintainer"] = val
                step = 9 if return_to_review else 7
                return_to_review = False

            elif step == 7:
                val = input(f"ZSTD level 0-22 ({state['zstd_level']}) : ").strip()
                if val.lower() in ("b", "back"):
                    step = 9 if return_to_review else 6
                    return_to_review = False
                    continue
                if val:
                    try:
                        lvl = int(val)
                        if 0 <= lvl <= 22:
                            state["zstd_level"] = lvl
                        else:
                            print("Must be between 0 and 22.")
                            continue
                    except ValueError:
                        print("Enter a number between 0 and 22.")
                        continue
                step = 9 if return_to_review else 8
                return_to_review = False

            elif step == 8:
                val = input(f"ZIP level 0-9 ({state['zip_level']}) : ").strip()
                if val.lower() in ("b", "back"):
                    step = 9 if return_to_review else 7
                    return_to_review = False
                    continue
                if val:
                    try:
                        lvl = int(val)
                        if 0 <= lvl <= 9:
                            state["zip_level"] = lvl
                        else:
                            print("Must be between 0 and 9.")
                            continue
                    except ValueError:
                        print("Enter a number between 0 and 9.")
                        continue
                step = 9
                return_to_review = False

            elif step == 9:
                print("\n------------------------------------------------------------------------")
                print("Build Configuration")
                print(f"  1. Images folder : {state['imgs_dir']} ({len(state['partitions'])} files)")
                print(f"  2. Device name   : {state['device']}")
                print(f"  3. Codename      : {state['codename'] or '(none)'}")
                print(f"  4. Version       : {state['version']}")
                print(f"  5. AVB 2.0       : {state['vbmeta']}")
                print(f"  6. Maintainer    : {state['maintainer']}")
                print(f"  7. ZSTD level    : {state['zstd_level']}")
                print(f"  8. ZIP level     : {state['zip_level']}")
                print("------------------------------------------------------------------------")
                ans = input("Start build? (y/n, 1-8 to edit, b to go back) : ").strip().lower()
                if ans in ("", "y", "yes"):
                    break
                if ans in ("b", "back"):
                    step = 8
                    continue
                if ans in ("n", "no", "q", "quit"):
                    print("Build cancelled.")
                    return
                if ans in ("1", "2", "3", "4", "5", "6", "7", "8"):
                    step = int(ans)
                    return_to_review = True
                    continue
                print("Enter y, n, b, or a number from 1 to 8.")

        out_dir = os.path.join(ROOT_DIR, "output")
        os.makedirs(out_dir, exist_ok=True)
        out_zip = os.path.join(
            out_dir,
            format_output_filename(state["device"], state["version"], state["codename"]),
        )

        cfg = BuildConfig(
            imgs_dir=state["imgs_dir"],
            output_zip=out_zip,
            device=state["device"],
            codename=state["codename"],
            version=state["version"],
            maintainer=state["maintainer"],
            vbmeta_mode=state["vbmeta"],
            zstd_level=state["zstd_level"],
            zip_level=state["zip_level"],
            partitions=state["partitions"],
        )

        print(f"\nBuilding {out_zip}...\n")
        res = build_package(cfg)
        print_summary(res)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nError: {exc}")
        sys.exit(1)


def cli_flow() -> None:
    parser = argparse.ArgumentParser(
        prog="newbies-flasher",
        description="Build Android recovery and fastboot flashable ZIP packages from partition images.",
    )
    parser.add_argument("-i", "--imgs", "--imgs-path", dest="imgs_path", help="Folder containing .img or .img.zst files")
    parser.add_argument("-d", "--device", default="Android Device", help="Device name")
    parser.add_argument("-c", "--codename", default="", help="Device codename for flashing check")
    parser.add_argument("-v", "--version", default="1.0", help="ROM or firmware version")
    parser.add_argument("-m", "--maintainer", default="User", help="Maintainer name")
    parser.add_argument("--vbmeta", choices=("skip", "disable", "enable"), default="skip", help="AVB 2.0 vbmeta mode")
    parser.add_argument("--zstd-level", type=int, default=1, help="ZSTD compression level (0 disables zstd, 1-22 enables)")
    parser.add_argument("--zip-level", type=int, default=1, help="ZIP deflate level for raw files (0-9; .zst is always stored)")
    parser.add_argument("-o", "--output", help="Custom output ZIP path")
    parser.add_argument("--exclude", default="", help="Comma-separated partition names to skip")
    parser.add_argument("--no-fastboot", action="store_true", help="Build recovery-only ZIP without fastboot installers")
    parser.add_argument("--no-checksum", action="store_true", help="Skip generating SHA-256 verification manifest")
    parser.add_argument("--keep-super", action="store_true", help="Do not clear unrelated stale partitions in super")
    parser.add_argument("--print-scripts", action="store_true", help="Print generated scripts and exit without building")
    parser.add_argument("--gui", action="store_true", help="Launch desktop GUI")

    args = parser.parse_args()

    if args.gui:
        from gui import launch_gui
        launch_gui()
        return

    imgs_path = clean_path(args.imgs_path or "")
    if not imgs_path or not os.path.isdir(imgs_path):
        parser.error(f"Images directory not found: {args.imgs_path}")

    parts = scan_partitions(imgs_path)
    if not parts:
        print(f"Error: no .img or .img.zst files found in {imgs_path}")
        sys.exit(1)

    excluded = {p.strip().lower() for p in args.exclude.split(",") if p.strip()}
    if excluded:
        from dataclasses import replace
        parts = [replace(p, selected=(p.name.lower() not in excluded)) for p in parts]

    out_dir = os.path.join(ROOT_DIR, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_zip = clean_path(args.output) if args.output else os.path.join(
        out_dir, format_output_filename(args.device, args.version, args.codename)
    )

    cfg = BuildConfig(
        imgs_dir=imgs_path,
        output_zip=out_zip,
        device=args.device,
        codename=args.codename,
        version=args.version,
        maintainer=args.maintainer,
        vbmeta_mode=args.vbmeta,
        zstd_level=args.zstd_level,
        zip_level=args.zip_level,
        include_fastboot=not args.no_fastboot,
        verify_sha256=not args.no_checksum,
        clean_super=not args.keep_super,
        partitions=parts,
    )

    if args.print_scripts:
        for name, content in preview_scripts(cfg).items():
            print(f"===== {name} =====")
            print(content)
        return

    res = build_package(cfg)
    print_summary(res)


def main() -> None:
    if "--gui" in sys.argv:
        from gui import launch_gui
        launch_gui()
    elif len(sys.argv) == 1:
        interactive_flow()
    else:
        cli_flow()


if __name__ == "__main__":
    main()
