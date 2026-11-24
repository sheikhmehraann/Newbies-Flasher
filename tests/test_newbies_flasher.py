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
    _write_all,
    build_package,
    format_output_filename,
    preview_update_binary,
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
    unsparse_to_stream,
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

    def test_sparse_detection_unsparse_and_stream_identical(self) -> None:
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

        streamed = bytearray()
        stream_sz = unsparse_to_stream(sp_path, lambda mv: streamed.extend(mv))
        self.assertEqual(stream_sz, len(expected))
        self.assertEqual(bytes(streamed), expected)

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

    def test_zstd_multiframe_streaming_nofcs_and_corrupt_detection(self) -> None:
        zbin = get_host_zstd()
        self.assertIsNotNone(zbin)

        # Multi-frame .zst (e.g. pzstd / concatenated frames) must sum ALL frames, not only frame 1.
        r1 = os.path.join(self.tmp, "p1.raw")
        r2 = os.path.join(self.tmp, "p2.raw")
        z1 = os.path.join(self.tmp, "p1.zst")
        z2 = os.path.join(self.tmp, "p2.zst")
        multi = os.path.join(self.tmp, "multi.img.zst")
        with open(r1, "wb") as f:
            f.write(b"AAAA" * 4096)
        with open(r2, "wb") as f:
            f.write(b"BBBB" * 4096)
        subprocess.run([zbin, "-1", "-f", "-q", r1, "-o", z1], check=True)
        subprocess.run([zbin, "-1", "-f", "-q", r2, "-o", z2], check=True)
        with open(z1, "rb") as f1, open(z2, "rb") as f2, open(multi, "wb") as fm:
            fm.write(f1.read() + f2.read())
        self.assertEqual(get_zstd_uncompressed_size(multi), 32768)

        # Streaming .zst without Frame_Content_Size must resolve cleanly via CLI stream counting.
        nofcs = os.path.join(self.tmp, "nofcs.img.zst")
        proc = subprocess.run([zbin, "-1", "-c"], input=(b"STREAM" * 4096), capture_output=True, check=True)
        with open(nofcs, "wb") as f:
            f.write(proc.stdout)
        self.assertIsNone(read_zstd_frame_content_size(nofcs))
        self.assertEqual(get_zstd_uncompressed_size(nofcs), 24576)

        # Corrupt .zst with valid magic must raise a clear RuntimeError.
        bad_zst = os.path.join(self.tmp, "bad.img.zst")
        with open(bad_zst, "wb") as f:
            f.write(b"\x28\xb5\x2f\xfd" + b"\xff" * 64)
        with self.assertRaises(RuntimeError):
            get_zstd_uncompressed_size(bad_zst)

    def test_zip_compresslevel_honors_levels_0_1_6_9(self) -> None:
        import random
        rng = random.Random(42)
        raw_dir = os.path.join(self.tmp, "zip_lvl_imgs")
        os.makedirs(raw_dir)
        data = bytes((rng.randrange(256) if (i % 7 == 0) else ((i * 13) & 0x3F)) for i in range(512 * 1024))
        with open(os.path.join(raw_dir, "boot.img"), "wb") as f:
            f.write(data)

        sizes = {}
        for lvl in (0, 1, 6, 9):
            out = os.path.join(self.tmp, f"zip_l{lvl}.zip")
            res = build_package(BuildConfig(imgs_dir=raw_dir, output_zip=out, zstd_level=0, zip_level=lvl), on_log=lambda _m: None)
            sizes[lvl] = res.size_bytes
            with zipfile.ZipFile(res.output_zip, "r") as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(zf.read("boot.img"), data)
        self.assertGreater(sizes[0], sizes[1])
        self.assertGreater(sizes[1], sizes[6])
        self.assertGreater(sizes[6], sizes[9])

    def test_zstd_ultra_levels_python_fallback_and_write_all(self) -> None:
        class _ChunkedSink:
            def __init__(self) -> None:
                self.out = bytearray()

            def write(self, data) -> int:
                piece = bytes(data[:7])
                self.out.extend(piece)
                return len(piece)

        sink = _ChunkedSink()
        sample = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 3
        _write_all(sink, memoryview(sample))
        self.assertEqual(bytes(sink.out), sample)

        # Ultra level 20 + 22 build cleanly with sparse + raw images
        _make_sparse_test_image(os.path.join(self.imgs, "vendor.img"))
        for lvl in (20, 22):
            out = os.path.join(self.tmp, f"ultra_{lvl}.zip")
            res = build_package(BuildConfig(imgs_dir=self.imgs, output_zip=out, zstd_level=lvl, zip_level=1), on_log=lambda _m: None)
            with zipfile.ZipFile(res.output_zip, "r") as zf:
                self.assertIsNone(zf.testzip())

        # Missing both zstd binary and python zstandard must fail fast with actionable error
        with patch("flasher.builder.get_host_zstd", return_value=None), patch("flasher.builder.has_python_zstandard", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                build_package(BuildConfig(imgs_dir=self.imgs, output_zip=os.path.join(self.tmp, "noz.zip"), zstd_level=1), on_log=lambda _m: None)
            self.assertIn("zstd is not available", str(ctx.exception))

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

    def test_full_build_sparse_stream_no_stale_wipe_and_zip_order(self) -> None:
        sp_vendor = os.path.join(self.imgs, "vendor.img")
        expected_vendor = _make_sparse_test_image(sp_vendor)

        out_zip = os.path.join(self.tmp, "out", "My Test ROM.zip")
        parts = scan_partitions(self.imgs)
        cfg = BuildConfig(
            imgs_dir=self.imgs,
            output_zip=out_zip,
            device="Infinix GT 20 Pro",
            codename="X6871",
            version="2.5.0",
            maintainer="dev",
            vbmeta_mode="disable",
            zstd_level=1,
            zip_level=5,
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
            self.assertIn("META-INF/com/google/android/updater-script", names)

            # Fastboot scripts, host fastboot binaries, and SHA-256 manifest must not exist
            for forbidden in (
                "META-INF/sha256sums.txt",
                "flash_windows.bat",
                "flash_linux.sh",
                "flash_macos.sh",
                "flash_termux.sh",
                "bin/windows/fastboot.exe",
                "bin/linux/fastboot",
                "META-INF/bin/rapidflasher-arm64",
                "META-INF/bin/lpdump",
                "META-INF/bin/lpmake",
            ):
                self.assertNotIn(forbidden, names)
            self.assertFalse(any(n.startswith("bin/") for n in names))

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
            self.assertIn("lp_clear_one vendor", ub)
            self.assertIn("unlimited-group", ub)
            self.assertIn("/proc/mounts", ub)
            self.assertIn("unlock_block_rw", ub)
            self.assertIn("check_block_fits", ub)
            self.assertIn("END_TS=$(date +%s", ub)
            self.assertIn("Installation complete${ELAPSED}", ub)
            self.assertNotIn("lp_clear_all_stale", ub)
            self.assertNotIn("--no-check", ub)
            self.assertNotIn("sha256sums.txt", ub)
            self.assertNotIn("verify_manifest", ub)

            # Dynamic / super stage must execute BEFORE bootchain, firmware, and AVB stage
            self.assertLess(ub.index("[2/5] Dynamic / Super partitions"), ub.index("[3/5] Bootchain partitions"))
            self.assertLess(ub.index("[3/5] Bootchain partitions"), ub.index("[4/5] Firmware partitions"))
            self.assertLess(ub.index("[4/5] Firmware partitions"), ub.index("[5/5] AVB 2.0 Vbmeta (Disable)"))
            self.assertIn('apply_avb_mode "disable"', ub)
            self.assertIn("patch_vbmeta_block_dd", ub)

            # Verify direct sparse->zstd payload matches expected raw bytes and carries exact FCS header
            zbin = get_host_zstd()
            vnd_zst = os.path.join(self.tmp, "chk_vendor.zst")
            vnd_raw = os.path.join(self.tmp, "chk_vendor.img")
            with open(vnd_zst, "wb") as f:
                f.write(zf.read("vendor.img.zst"))
            self.assertEqual(read_zstd_frame_content_size(vnd_zst), len(expected_vendor))
            subprocess.run([zbin, "-d", "-f", "-q", vnd_zst, "-o", vnd_raw], check=True)
            with open(vnd_raw, "rb") as f:
                self.assertEqual(f.read(), expected_vendor)

            # Verify top-level vbmeta patched (flags=3) and chained vbmeta_system intact (flags=0)
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

    def test_raw_mode_parallel_unsparse_build(self) -> None:
        sp_vendor = os.path.join(self.imgs, "vendor.img")
        expected_vendor = _make_sparse_test_image(sp_vendor)
        out_zip = os.path.join(self.tmp, "raw_out.zip")
        cfg = BuildConfig(imgs_dir=self.imgs, output_zip=out_zip, zstd_level=0, zip_level=1)
        res = build_package(cfg)
        self.assertTrue(os.path.isfile(res.output_zip))
        with zipfile.ZipFile(res.output_zip, "r") as zf:
            self.assertEqual(zf.read("vendor.img"), expected_vendor)

    def test_pipelined_large_image_streaming_and_meta_first_order(self) -> None:
        sp_vendor = os.path.join(self.imgs, "vendor.img")
        expected_vendor = _make_sparse_test_image(sp_vendor)
        out_zip = os.path.join(self.tmp, "pipelined.zip")
        cfg = BuildConfig(imgs_dir=self.imgs, output_zip=out_zip, zstd_level=1, zip_level=1)
        # Lower large-image threshold to exercise overlapped compress + ZIP stream path.
        with patch("flasher.builder.LARGE_IMAGE_BYTES", 10240):
            res = build_package(cfg)
        self.assertTrue(os.path.isfile(res.output_zip))
        with zipfile.ZipFile(res.output_zip, "r") as zf:
            names = zf.namelist()
            self.assertLess(names.index("META-INF/com/google/android/update-binary"), names.index("system.img.zst"))
            self.assertLess(names.index("boot.img.zst"), names.index("system.img.zst"))
            zbin = get_host_zstd()
            vnd_zst = os.path.join(self.tmp, "pipe_vnd.zst")
            vnd_raw = os.path.join(self.tmp, "pipe_vnd.img")
            with open(vnd_zst, "wb") as f:
                f.write(zf.read("vendor.img.zst"))
            subprocess.run([zbin, "-d", "-f", "-q", vnd_zst, "-o", vnd_raw], check=True)
            with open(vnd_raw, "rb") as f:
                self.assertEqual(f.read(), expected_vendor)

    def test_zero_copy_repack_of_precompressed_zst(self) -> None:
        zst_dir = os.path.join(self.tmp, "zst_imgs")
        os.makedirs(zst_dir)
        raw_boot = os.path.join(self.tmp, "boot_src.img")
        zst_boot = os.path.join(zst_dir, "boot.img.zst")
        with open(raw_boot, "wb") as f:
            f.write(b"BOOTIMG_PAYLOAD" * 512)
        zbin = get_host_zstd()
        subprocess.run([zbin, "-1", "-f", "-q", raw_boot, "-o", zst_boot], check=True)

        out_zip = os.path.join(self.tmp, "repacked.zip")
        cfg = BuildConfig(imgs_dir=zst_dir, output_zip=out_zip, zstd_level=1, zip_level=1)
        res = build_package(cfg)
        self.assertTrue(os.path.isfile(res.output_zip))
        with zipfile.ZipFile(res.output_zip, "r") as zf:
            self.assertIn("boot.img.zst", zf.namelist())
            with open(zst_boot, "rb") as f:
                self.assertEqual(zf.read("boot.img.zst"), f.read())

    def test_adversarial_metadata_and_empty_codename(self) -> None:
        cfg = BuildConfig(
            imgs_dir=self.imgs,
            output_zip=os.path.join(self.tmp, "adv.zip"),
            device='Phone "X" $(id) & calc',
            codename="code;rm -rf / & whoami",
            version="1.0`uname`",
            maintainer="dev|evil",
        )
        ub = preview_update_binary(cfg)
        self.assertIn("expected_list='code;rm -rf / & whoami'", ub)

        cfg_any = BuildConfig(imgs_dir=self.imgs, output_zip=os.path.join(self.tmp, "any.zip"), codename="")
        ub_any = preview_update_binary(cfg_any)
        self.assertIn("expected_list=''", ub_any)
        self.assertIn("'(any)'", ub_any)

    def test_build_cancellation_cleans_output(self) -> None:
        ev = threading.Event()
        ev.set()
        out_zip = os.path.join(self.tmp, "cancelled.zip")
        cfg = BuildConfig(imgs_dir=self.imgs, output_zip=out_zip)
        with self.assertRaises(RuntimeError):
            build_package(cfg, cancel_event=ev)
        self.assertFalse(os.path.exists(out_zip))

    def test_output_filename_codename_version_format(self) -> None:
        self.assertEqual(
            format_output_filename("Infinix GT 20 Pro", "15.1.2", "X6871"),
            "X6871-15.1.2-Flashable.zip",
        )
        self.assertEqual(
            format_output_filename("Infinix GT 20 Pro", "1.0", "X6871"),
            "X6871-1.0-Flashable.zip",
        )
        self.assertEqual(
            format_output_filename("Infinix GT 20 Pro", "1.0", ""),
            "Infinix-GT-20-Pro-1.0-Flashable.zip",
        )
        self.assertNotIn("vbmeta", format_output_filename("Infinix GT 20 Pro", "1.0", "X6871").lower())

    def test_vbmeta_standalone_disable_enable_and_skip_modes(self) -> None:
        # Standalone Disable Vbmeta ZIP (no partition images required)
        out_dis = os.path.join(self.tmp, format_output_filename("Infinix GT 20 Pro", "1.0", "X6871"))
        self.assertNotIn("vbmeta", os.path.basename(out_dis).lower())
        res_dis = build_package(BuildConfig(imgs_dir="", output_zip=out_dis, vbmeta_mode="disable"), on_log=lambda _m: None)
        with zipfile.ZipFile(res_dis.output_zip, "r") as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            self.assertIn("META-INF/bin/avbctl", names)
            self.assertIn("META-INF/com/google/android/update-binary", names)
            self.assertNotIn("META-INF/zstd", names)
            self.assertNotIn("META-INF/bin/lptools", names)
            ub = zf.read("META-INF/com/google/android/update-binary").decode("utf-8")
            self.assertIn('apply_avb_mode "disable"', ub)
            self.assertIn("patch_vbmeta_block_dd", ub)
            self.assertNotIn("unmount_dynamic", ub)

        # Standalone Enable Vbmeta ZIP
        out_en = os.path.join(self.tmp, format_output_filename("Infinix GT 20 Pro", "1.1", "X6871"))
        self.assertNotIn("vbmeta", os.path.basename(out_en).lower())
        res_en = build_package(BuildConfig(imgs_dir="", output_zip=out_en, vbmeta_mode="enable"), on_log=lambda _m: None)
        with zipfile.ZipFile(res_en.output_zip, "r") as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("META-INF/bin/avbctl", zf.namelist())
            ub = zf.read("META-INF/com/google/android/update-binary").decode("utf-8")
            self.assertIn('apply_avb_mode "enable"', ub)

        # Skip mode with no images must fail cleanly
        with self.assertRaises(ValueError):
            build_package(BuildConfig(imgs_dir="", output_zip=os.path.join(self.tmp, "empty.zip"), vbmeta_mode="skip"), on_log=lambda _m: None)

        # Skip mode with images must not bundle avbctl or modify vbmeta.img
        out_skip = os.path.join(self.tmp, "skip.zip")
        res_skip = build_package(BuildConfig(imgs_dir=self.imgs, output_zip=out_skip, vbmeta_mode="skip", zstd_level=0), on_log=lambda _m: None)
        with zipfile.ZipFile(res_skip.output_zip, "r") as zf:
            self.assertNotIn("META-INF/bin/avbctl", zf.namelist())
            ub = zf.read("META-INF/com/google/android/update-binary").decode("utf-8")
            self.assertNotIn("apply_avb_mode", ub)
            self.assertNotIn("avbctl", ub)
            vb_tmp = os.path.join(self.tmp, "skip_vb.img")
            with open(vb_tmp, "wb") as f:
                f.write(zf.read("vbmeta.img"))
            self.assertEqual(read_vbmeta_flags(vb_tmp), 0)

    def test_gui_defaults_sliders_naming_fits_768p_and_safe_select(self) -> None:
        import tkinter as tk
        from gui import NewbiesFlasherApp

        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available for Tkinter GUI test")
        root.withdraw()
        try:
            app = NewbiesFlasherApp(root)
            self.assertEqual(app.device_var.get(), "Infinix GT 20 Pro")
            self.assertEqual(app.codename_var.get(), "X6871")
            self.assertFalse(hasattr(app, "fastboot_var"))
            self.assertFalse(hasattr(app, "sha256_var"))
            self.assertFalse(hasattr(app, "clean_super_var"))
            self.assertEqual(app.out_name_lbl.cget("text"), "X6871-1.0-Flashable.zip")

            app.version_var.set("15.1.2")
            self.assertEqual(app.out_name_lbl.cget("text"), "X6871-15.1.2-Flashable.zip")

            # Verify AVB 2.0 Skip/Disable/Enable segmented pills never inject 'vbmeta' into ZIP filename
            app.vbmeta_pills["disable"]._invoke()
            self.assertEqual(app.vbmeta_var.get(), "disable")
            self.assertEqual(app.out_name_lbl.cget("text"), "X6871-15.1.2-Flashable.zip")
            self.assertNotIn("vbmeta", app.out_name_lbl.cget("text").lower())
            app.vbmeta_pills["enable"]._invoke()
            self.assertEqual(app.vbmeta_var.get(), "enable")
            self.assertEqual(app.out_name_lbl.cget("text"), "X6871-15.1.2-Flashable.zip")
            app.vbmeta_pills["skip"]._invoke()
            self.assertEqual(app.vbmeta_var.get(), "skip")
            self.assertEqual(app.out_name_lbl.cget("text"), "X6871-15.1.2-Flashable.zip")

            # Verify ZSTD and ZIP sliders step cleanly without snapping back and sync labels
            app.zstd_slider.step(1)
            self.assertEqual(app.zstd_var.get(), 2)
            self.assertIn("Level 2", app.zstd_lbl.cget("text"))
            self.assertIn(".zst stored", app.zip_title_lbl.cget("text"))

            app.zstd_var.set(0)
            self.assertEqual(app.zstd_var.get(), 0)
            self.assertIn("Off", app.zstd_lbl.cget("text"))
            self.assertIn("raw images", app.zip_title_lbl.cget("text"))

            app.zip_slider.step(3)
            self.assertEqual(app.zip_var.get(), 4)
            self.assertIn("Deflate 4", app.zip_lbl.cget("text"))

            # Verify clamping at bounds
            app.zip_slider.step(50)
            self.assertEqual(app.zip_var.get(), 9)
            app.zip_slider.step(-50)
            self.assertEqual(app.zip_var.get(), 0)

            app.partitions = scan_partitions(self.imgs)
            app._refresh_tree()
            app._sort_partitions("raw")
            root.update_idletasks()

            # Ensure requested UI height fits inside a standard 768p laptop screen
            self.assertLessEqual(root.winfo_reqheight(), 680)
            self.assertEqual(len(app.tree.get_children()), len(app.partitions))

            # Verify PillButton controls are enabled, clickable, and unsquished
            self.assertTrue(app.build_btn.enabled)
            self.assertFalse(app.cancel_btn.enabled)
            app.build_btn.set_text("Building...")
            self.assertEqual(app.build_btn.text, "Building...")
            app.build_btn.set_text("Build Flashable ZIP")

            # Verify in-place row toggle preserves Treeview items and updates selection state
            app._toggle_index(0)
            self.assertFalse(app.partitions[0].selected)
            self.assertEqual(app.tree.item("0", "values")[0], "[ ]")
            app._toggle_index(0)
            self.assertTrue(app.partitions[0].selected)
            self.assertEqual(app.tree.item("0", "values")[0], "[x]")

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
