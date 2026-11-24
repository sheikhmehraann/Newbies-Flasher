import os
import re
import struct
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from .platform_utils import get_host_zstd
from .sparse import get_sparse_uncompressed_size, is_sparse_image

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ZSTD_SKIPPABLE_LOW = 0x184D2A50
ZSTD_SKIPPABLE_HIGH = 0x184D2A5F

DYNAMIC_PARTITIONS = {
    "system", "vendor", "product", "system_ext", "system_dlkm",
    "vendor_dlkm", "odm_dlkm", "odm", "mi_ext", "cust",
    "prism", "optics", "my_product", "my_stock", "my_heytap",
    "my_carrier", "my_region", "my_manifest", "my_preload",
    "my_company", "my_engineering", "my_bigball",
    "tr_carrier", "tr_company", "tr_mi", "tr_overlayfs", "tr_preload",
    "tr_product", "tr_region", "tr_theme", "tr_manifest", "tr_misc",
    "tr_vintf", "tr_vendor",
}

BOOTCHAIN_PARTITIONS = {
    "boot", "dtbo", "init_boot", "vendor_boot", "vendor_kernel_boot",
    "recovery", "vbmeta", "vbmeta_system", "vbmeta_vendor", "pvmfw",
}

# Low-level bootloader partitions that can hard-brick a device if interrupted or mismatched.
BOOTLOADER_PARTITIONS = {
    "preloader", "preloader_raw", "lk", "lk2", "xbl", "xbl_config",
    "xbl_ramdump", "abl", "sbl1", "sbl2", "sbl3", "rpm", "tz", "hyp", "uefi",
}

FIRMWARE_PARTITIONS = {
    "apusys", "cam_vpu1", "cam_vpu2", "cam_vpu3", "ccu", "connsys_bt",
    "connsys_gnss", "connsys_wifi", "dpm", "gpueb", "gz", "logo",
    "mcf_ota", "mcupm", "md1img", "mvpu_algo", "pi_img",
    "scp", "spmfw", "sspm", "tee", "tkv", "vcp",
    "modem", "bluetooth", "dsp", "aop", "devcfg", "featenabler",
    "imagefv", "keymaster", "qupfw", "shrm", "storsec", "uefisecapp",
}

_ZSTD_LV_BYTES_RE = re.compile(r"Decompressed Size\s*:\s*.*?\((\d+)\s*B\)", re.IGNORECASE)


@dataclass(frozen=True)
class PartitionImage:
    name: str
    path: str
    filename: str
    kind: str          # "dynamic", "super_raw", "bootchain", "firmware", "bootloader"
    fmt: str           # "raw", "sparse", "zstd"
    file_size: int
    raw_size: int
    selected: bool = True


def is_zstd_file(path: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) < 4:
        return False
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic == ZSTD_MAGIC:
                return True
            val = struct.unpack("<I", magic)[0]
            return ZSTD_SKIPPABLE_LOW <= val <= ZSTD_SKIPPABLE_HIGH
    except OSError:
        return False


def is_raw_filesystem(path: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) < 2048:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(1024)
            head = f.read(4)
            if head in (b"\xe2\xe1\xf5\xe0", b"\x10\x20\xf5\xf2"):
                return True
            f.seek(1080)
            if f.read(2) == b"\x53\xef":
                return True
    except OSError:
        pass
    return False


def read_zstd_frame_content_size(path: str) -> Optional[int]:
    """Parse RFC 8878 Zstandard frame header to extract Frame_Content_Size without external tools."""
    try:
        with open(path, "rb") as f:
            while True:
                magic_bytes = f.read(4)
                if len(magic_bytes) < 4:
                    return None
                if magic_bytes == ZSTD_MAGIC:
                    break
                magic_val = struct.unpack("<I", magic_bytes)[0]
                if ZSTD_SKIPPABLE_LOW <= magic_val <= ZSTD_SKIPPABLE_HIGH:
                    skip_len_raw = f.read(4)
                    if len(skip_len_raw) < 4:
                        return None
                    skip_len = struct.unpack("<I", skip_len_raw)[0]
                    f.seek(skip_len, os.SEEK_CUR)
                    continue
                return None

            fhd_raw = f.read(1)
            if not fhd_raw:
                return None
            fhd = fhd_raw[0]
            fcs_flag = (fhd >> 6) & 0x03
            single_segment = (fhd >> 5) & 0x01
            dict_id_flag = fhd & 0x03

            window_sz = 0 if single_segment else 1
            dict_id_sz = (0, 1, 2, 4)[dict_id_flag]
            if fcs_flag == 0:
                fcs_sz = 1 if single_segment else 0
            elif fcs_flag == 1:
                fcs_sz = 2
            elif fcs_flag == 2:
                fcs_sz = 4
            else:
                fcs_sz = 8

            if fcs_sz == 0:
                return None

            if window_sz + dict_id_sz:
                f.seek(window_sz + dict_id_sz, os.SEEK_CUR)

            fcs_bytes = f.read(fcs_sz)
            if len(fcs_bytes) < fcs_sz:
                return None

            if fcs_sz == 1:
                return fcs_bytes[0]
            if fcs_sz == 2:
                return struct.unpack("<H", fcs_bytes)[0] + 256
            if fcs_sz == 4:
                return struct.unpack("<I", fcs_bytes)[0]
            return struct.unpack("<Q", fcs_bytes)[0]
    except OSError:
        return None


def get_zstd_uncompressed_size(path: str, zstd_bin: Optional[str] = None) -> int:
    hdr_size = read_zstd_frame_content_size(path)
    if hdr_size is not None and hdr_size > 0:
        return hdr_size

    z_bin = zstd_bin or get_host_zstd()
    if z_bin:
        try:
            proc = subprocess.run(
                [z_bin, "-lv", path],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                m = _ZSTD_LV_BYTES_RE.search(proc.stdout)
                if m:
                    val = int(m.group(1))
                    if val > 0:
                        return val
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    try:
        import zstandard
        dctx = zstandard.ZstdDecompressor()
        total = 0
        with open(path, "rb") as f:
            with dctx.stream_reader(f) as reader:
                while True:
                    chunk = reader.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
        if total > 0:
            return total
    except Exception:
        pass

    raise RuntimeError(
        f"Cannot determine uncompressed size of {os.path.basename(path)} "
        "(missing ZSTD Frame_Content_Size and no working zstd CLI/library available)"
    )


def strip_image_suffix(filename: str) -> Optional[str]:
    lower = filename.lower()
    if lower.endswith(".img.zst"):
        return lower[:-8]
    if lower.endswith(".img"):
        return lower[:-4]
    if lower.endswith(".zst"):
        return lower[:-4]
    return None


def classify_partition(name: str, path: str, fmt: str) -> str:
    n = name.lower()
    if n == "super":
        return "super_raw"
    if n in DYNAMIC_PARTITIONS:
        return "dynamic"
    if n in BOOTCHAIN_PARTITIONS:
        return "bootchain"
    if n in BOOTLOADER_PARTITIONS:
        return "bootloader"
    if n in FIRMWARE_PARTITIONS:
        return "firmware"
    if fmt == "raw" and is_raw_filesystem(path):
        return "dynamic"
    if fmt == "sparse":
        return "dynamic"
    return "firmware"


def scan_partitions(directory: str, zstd_bin: Optional[str] = None) -> List[PartitionImage]:
    if not directory or not os.path.isdir(directory):
        return []

    found: Dict[str, PartitionImage] = {}
    walk_entries = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        depth = os.path.relpath(root, directory).count(os.sep) if root != directory else -1
        for fname in sorted(files):
            walk_entries.append((depth, root, fname))
    walk_entries.sort(key=lambda item: (item[0], item[2].lower()))

    for _, root, fname in walk_entries:
        base = strip_image_suffix(fname)
        if not base or base in found:
            continue

        fpath = os.path.join(root, fname)
        if not os.path.isfile(fpath):
            continue

        file_size = os.path.getsize(fpath)
        if file_size == 0:
            continue

        if is_zstd_file(fpath):
            fmt = "zstd"
            raw_size = get_zstd_uncompressed_size(fpath, zstd_bin=zstd_bin)
        elif is_sparse_image(fpath):
            fmt = "sparse"
            raw_size = get_sparse_uncompressed_size(fpath) or file_size
        else:
            fmt = "raw"
            raw_size = file_size

        kind = classify_partition(base, fpath, fmt)
        found[base] = PartitionImage(
            name=base,
            path=fpath,
            filename=fname,
            kind=kind,
            fmt=fmt,
            file_size=file_size,
            raw_size=raw_size,
            selected=True,
        )

    order = {"super_raw": 0, "dynamic": 1, "bootchain": 2, "firmware": 3, "bootloader": 4}
    return sorted(found.values(), key=lambda p: (order.get(p.kind, 9), p.name))


def detect_build_props(directory: str) -> Dict[str, str]:
    result = {"device": "", "codename": "", "version": ""}
    if not directory or not os.path.isdir(directory):
        return result

    for root, _, files in os.walk(directory):
        for fname in sorted(files):
            if not (fname == "build.prop" or fname.endswith(".prop")):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = (part.strip() for part in line.split("=", 1))
                        if not v:
                            continue
                        if k in ("ro.product.device", "ro.build.product", "ro.product.board") and not result["codename"]:
                            result["codename"] = v
                        elif k in ("ro.product.marketname", "ro.product.model") and not result["device"]:
                            result["device"] = v
                        elif k in ("ro.build.display.id", "ro.build.version.incremental") and not result["version"]:
                            result["version"] = v
            except OSError:
                continue
        if all(result.values()):
            break
    return result
