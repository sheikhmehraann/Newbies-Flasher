import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flasher.avb import (
    AVB_FLAG_DISABLE_VERIFICATION,
    AVB_FLAG_DISABLE_VERITY,
    is_top_level_vbmeta,
    patch_vbmeta_flags,
    read_vbmeta_flags,
)
from flasher.builder import (
    BuildConfig,
    build_package,
    format_output_filename,
    preview_scripts,
)
from flasher.partitions import (
    classify_partition,
    detect_build_props,
    get_zstd_uncompressed_size,
    read_zstd_frame_content_size,
    scan_partitions,
)
from flasher.platform_utils import get_host_zstd
from flasher.sparse import (
    CHUNK_TYPE_DONT_CARE,
    CHUNK_TYPE_FILL,
    CHUNK_TYPE_RAW,
    SPARSE_MAGIC,
    get_sparse_uncompressed_size,
    is_sparse_image,
    unsparse_image,
)


def _make_vbmeta(path: str, flags: int = 0) -> None:
    hdr = bytearray(256)
    hdr[0:4] = b"AVB0"
    struct.pack_into(">I", hdr, 120, flags)
    with open(path, "wb") as f:
        f.write(hdr)


def _make_ext4_dummy(path: str, size: int = 8192) -> None:
    buf = bytearray(size)
    buf[1080:1082] = b"\x53\xef"
    with open(path, "wb") as f:
        f.write(buf)


def _make_sparse_test_image(path: str) -> bytes:
    blk_sz = 4096
    raw_block = bytes([0x41] * blk_sz)
    fill_zero_block = bytes([0x00] * blk_sz)
    fill_pat = struct.pack("<I", 0xAA55AA55)
    fill_pat_block = fill_pat * (blk_sz // 4)
    dont_care_block = bytes([0x00] * blk_sz)

    header = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, blk_sz, 4, 4, 0)
    c1 = struct.pack("<HHII", CHUNK_TYPE_RAW, 0, 1, 12 + blk_sz) + raw_block
    c2 = struct.pack("<HHII", CHUNK_TYPE_FILL, 0, 1, 16) + struct.pack("<I", 0)
    c3 = struct.pack("<HHII", CHUNK_TYPE_FILL, 0, 1, 16) + fill_pat
    c4 = struct.pack("<HHII", CHUNK_TYPE_DONT_CARE, 0, 1, 12)

    with open(path, "wb") as f:
        f.write(header + c1 + c2 + c3 + c4)

    return raw_block + fill_zero_block + fill_pat_block + dont_care_block


class TestNewbiesFlasher(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="nf_test_")
        self.imgs = os.path.join(self.tmp, "imgs")
        os.makedirs(self.imgs)

        with open(os.path.join(self.imgs, "boot.img"), "wb") as f:
            f.write(b"BOOTDATA" * 1024)
        with open(os.path.join(self.imgs, "apusys.img"), "wb") as f:
            f.write(b"APUSYS" * 1024)
        with open(os.path.join(self.imgs, "preloader_raw.img"), "wb") as f:
            f.write(b"PRELOADER" * 512)
        _make_vbmeta(os.path.join(self.imgs, "vbmeta.img"), flags=0)
        _make_vbmeta(os.path.join(self.imgs, "vbmeta_system.img"), flags=0)
        _make_ext4_dummy(os.path.join(self.imgs, "system.img"), size=32768)
        with open(os.path.join(self.imgs, "build.prop"), "w", encoding="utf-8") as f:
            f.write("ro.product.marketname=Test Phone Pro\nro.product.device=testphone\nro.build.display.id=2.5.0\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sparse_detection_and_unsparse_zero_fill(self) -> None:
        sp_path = os.path.join(self.tmp, "sparse_vendor.img")
        raw_path = os.path.join(self.tmp, "vendor_raw.img")
        expected = _make_sparse_test_image(sp_path)

        with open(raw_path, "wb") as f:
            f.write(b"\xff" * len(expected))

        self.assertTrue(is_sparse_image(sp_path))
        self.assertEqual(get_sparse_uncompressed_size(sp_path), len(expected))
        out_sz = unsparse_image(sp_path, raw_path)
        self.assertEqual(out_sz, len(expected))
        with open(raw_path, "rb") as f:
            self.assertEqual(f.read(), expected)

    def test_zstd_frame_header_size_without_cli_or_python_lib(self) -> None:
        raw_path = os.path.join(self.tmp, "sample.img")
        zst_path = os.path.join(self.tmp, "sample.img.zst")
        payload = b"ANDROID_PARTITION_BLOCK_" * 1500
        with open(raw_path, "wb") as f:
            f.write(payload)

        zbin = get_host_zstd()
        self.assertIsNotNone(zbin)
        subprocess.run([zbin, "-1", "-f", "-q", raw_path, "-o", zst_path], check=True)

        self.assertEqual(read_zstd_frame_content_size(zst_path), len(payload))
        with patch("flasher.partitions.get_host_zstd", return_value=None):
            self.assertEqual(get_zstd_uncompressed_size(zst_path), len(payload))

    def test_avb_only_patches_top_level_vbmeta(self) -> None:
        self.assertTrue(is_top_level_vbmeta("vbmeta"))
        self.assertFalse(is_top_level_vbmeta("vbmeta_system"))
        self.assertFalse(is_top_level_vbmeta("vbmeta_vendor"))

        vb = os.path.join(self.imgs, "vbmeta.img")
        self.assertEqual(read_vbmeta_flags(vb), 0)
        self.assertTrue(patch_vbmeta_flags(vb, "disable"))
        self.assertEqual(read_vbmeta_flags(vb), AVB_FLAG_DISABLE_VERITY | AVB_FLAG_DISABLE_VERIFICATION)
        self.assertTrue(patch_vbmeta_flags(vb, "enable"))
        self.assertEqual(read_vbmeta_flags(vb), 0)

    def test_classification_bootloader_and_super_raw(self) -> None:
        self.assertEqual(classify_partition("super", "", "raw"), "super_raw")
        self.assertEqual(classify_partition("preloader_raw", "", "raw"), "bootloader")
        self.assertEqual(classify_partition("lk", "", "raw"), "bootloader")
        self.assertEqual(classify_partition("apusys", "", "raw"), "firmware")
        props = detect_build_props(self.imgs)
        self.assertEqual(props["device"], "Test Phone Pro")
        self.assertEqual(props["codename"], "testphone")
        self.assertEqual(props["version"], "2.5.0")

    def test_full_build_hardening_zip_order_and_chained_vbmeta(self) -> None:
        sp_vendor = os.path.join(self.imgs, "vendor.img")
        expected_vendor = _make_sparse_test_image(sp_vendor)

        out_zip = os.path.join(self.tmp, "out", "My Test ROM.zip")
        parts = scan_partitions(self.imgs)
        cfg = BuildConfig(
            imgs_dir=self.imgs,
            output_zip=out_zip,
            device="Test Phone",
            codename="testphone",
            version="2.5.0",
            maintainer="dev",
            vbmeta_mode="disable",
            zstd_level=1,
            zip_level=5,
            include_fastboot=True,
            verify_sha256=True,
            clean_super=True,
            partitions=parts,
        )

        res1 = build_package(cfg)
        self.assertTrue(os.path.isfile(res1.output_zip))
        self.assertNotIn(" ", os.path.basename(res1.output_zip))
        self.assertIn("preloader_raw", res1.bootloader_partitions)

        # Second build reusing the same partitions list must succeed
        res2 = build_package(cfg)
        self.assertTrue(os.path.isfile(res2.output_zip))

        with zipfile.ZipFile(res1.output_zip, "r") as zf:
            names_list = zf.namelist()
            names = set(names_list)
            self.assertIn("META-INF/com/google/android/update-binary", names)
            self.assertIn("META-INF/sha256sums.txt", names)
            self.assertNotIn("META-INF/bin/rapidflasher-arm64", names)
            self.assertNotIn("META-INF/bin/lpdump", names)
            self.assertNotIn("META-INF/bin/lpmake", names)

            # META-INF entries must be packed before payload images
            ub_idx = names_list.index("META-INF/com/google/android/update-binary")
            sys_idx = names_list.index("system.img.zst")
            self.assertLess(ub_idx, sys_idx)

            # .zst entries must use ZIP_STORED
            for info in zf.infolist():
                if info.filename.endswith(".zst"):
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED, info.filename)

            ub = zf.read("META-INF/com/google/android/update-binary").decode("utf-8")
            self.assertIn(f"lp_create_slot vendor {len(expected_vendor)}", ub)
            self.assertIn("unlimited-group", ub)
            self.assertIn("lp_clear_all_stale", ub)
            self.assertIn("/proc/mounts", ub)
            self.assertIn("unlock_block_rw", ub)
            self.assertIn("check_block_fits", ub)
            self.assertNotIn("--no-check", ub)

            # Dynamic / super stage must execute BEFORE bootchain and firmware
            self.assertLess(ub.index("[2/4] Dynamic / Super partitions"), ub.index("[3/4] Bootchain & AVB 2.0"))
            self.assertLess(ub.index("[3/4] Bootchain & AVB 2.0"), ub.index("[4/4] Firmware partitions"))

            # Verify top-level vbmeta patched (flags=3) and chained vbmeta_system intact (flags=0)
            zbin = get_host_zstd()
            vb_zst = os.path.join(self.tmp, "chk_vb.zst")
            vb_raw = os.path.join(self.tmp, "chk_vb.img")
            vbs_zst = os.path.join(self.tmp, "chk_vbs.zst")
            vbs_raw = os.path.join(self.tmp, "chk_vbs.img")
            with open(vb_zst, "wb") as f:
                f.write(zf.read("vbmeta.img.zst"))
            with open(vbs_zst, "wb") as f:
                f.write(zf.read("vbmeta_system.img.zst"))
            subprocess.run([zbin, "-d", "-f", "-q", vb_zst, "-o", vb_raw], check=True)
            subprocess.run([zbin, "-d", "-f", "-q", vbs_zst, "-o", vbs_raw], check=True)
            self.assertEqual(read_vbmeta_flags(vb_raw), 3)
            self.assertEqual(read_vbmeta_flags(vbs_raw), 0)

    def test_adversarial_metadata_and_parentheses_zip_name(self) -> None:
        cfg = BuildConfig(
            imgs_dir=self.imgs,
            output_zip=os.path.join(self.tmp, "adv.zip"),
            device='Phone "X" $(id) & calc',
            codename="code;rm -rf / & whoami",
            version="1.0`uname`",
            maintainer="dev|evil",
        )
        scripts = preview_scripts(cfg)
        ub = scripts["META-INF/com/google/android/update-binary"]
        bat = scripts["flash_windows.bat"]
        self.assertIn("expected='code;rm -rf / & whoami'", ub)
        self.assertNotIn("& calc", bat)
        self.assertIn("if errorlevel 1", bat)
        self.assertIn("resize-logical-partition", bat)

    def test_build_cancellation_cleans_output(self) -> None:
        ev = threading.Event()
        ev.set()
        out_zip = os.path.join(self.tmp, "cancelled.zip")
        cfg = BuildConfig(imgs_dir=self.imgs, output_zip=out_zip)
        with self.assertRaises(RuntimeError):
            build_package(cfg, cancel_event=ev)
        self.assertFalse(os.path.exists(out_zip))

    def test_output_filename_sanitization(self) -> None:
        self.assertEqual(
            format_output_filename("Infinix GT 20 Pro", "15.1.2", "X6871"),
            "Infinix-GT-20-Pro-X6871-15.1.2-Flashable.zip",
        )
        self.assertEqual(
            format_output_filename("X6871", "1.0", "X6871"),
            "X6871-1.0-Flashable.zip",
        )

    def test_gui_fits_768p_screen_and_safe_select(self) -> None:
        import tkinter as tk
        from gui import NewbiesFlasherApp

        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available for Tkinter GUI test")
        root.withdraw()
        try:
            app = NewbiesFlasherApp(root)
            app.partitions = scan_partitions(self.imgs)
            app._refresh_tree()
            root.update_idletasks()

            # Ensure requested UI height fits inside a standard 768p laptop screen
            self.assertLessEqual(root.winfo_reqheight(), 680)
            self.assertEqual(len(app.tree.get_children()), len(app.partitions))

            # Safe select must uncheck bootloader partitions (preloader_raw) and keep others
            app._select_safe_only()
            by_name = {p.name: p.selected for p in app.partitions}
            self.assertFalse(by_name["preloader_raw"])
            self.assertTrue(by_name["boot"])
            self.assertTrue(by_name["system"])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
