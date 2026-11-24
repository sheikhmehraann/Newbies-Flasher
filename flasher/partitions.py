import os
import re
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .platform_utils import get_host_zstd, subprocess_flags
from .sparse import parse_sparse_header_bytes

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ZSTD_SKIPPABLE_LOW = 0x184D2A50
ZSTD_SKIPPABLE_HIGH = 0x184D2A5F

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "META-INF"}

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


def _is_zstd_magic(head: bytes) -> bool:
    if len(head) < 4:
        return False
    if head[:4] == ZSTD_MAGIC:
        return True
    val = struct.unpack_from("<I", head, 0)[0]
    return ZSTD_SKIPPABLE_LOW <= val <= ZSTD_SKIPPABLE_HIGH


def is_zstd_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return _is_zstd_magic(f.read(4))
    except OSError:
        return False


def _is_raw_fs_header(head: bytes) -> bool:
    if len(head) >= 1028 and head[1024:1028] in (b"\xe2\xe1\xf5\xe0", b"\x10\x20\xf5\xf2"):
        return True
    if len(head) >= 1082 and head[1080:1082] == b"\x53\xef":
        return True
    return False


def is_raw_filesystem(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return _is_raw_fs_header(f.read(1088))
    except OSError:
        return False


def _parse_zstd_fcs_from_bytes(buf: bytes) -> Optional[int]:
    """Fast in-memory RFC 8878 ZSTD Frame_Content_Size parser for single-frame headers."""
    pos = 0
    n = len(buf)
    while pos + 4 <= n:
        magic = buf[pos:pos + 4]
        if magic == ZSTD_MAGIC:
            pos += 4
            break
        val = struct.unpack_from("<I", buf, pos)[0]
        if ZSTD_SKIPPABLE_LOW <= val <= ZSTD_SKIPPABLE_HIGH:
            if pos + 8 > n:
                return None
            skip_len = struct.unpack_from("<I", buf, pos + 4)[0]
            pos += 8 + skip_len
            continue
        return None
    else:
        return None

    if pos >= n:
        return None
    fhd = buf[pos]
    pos += 1
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
    pos += window_sz + dict_id_sz
    if pos + fcs_sz > n:
        return None
    if fcs_sz == 1:
        return buf[pos]
    if fcs_sz == 2:
        return struct.unpack_from("<H", buf, pos)[0] + 256
    if fcs_sz == 4:
        return struct.unpack_from("<I", buf, pos)[0]
    return struct.unpack_from("<Q", buf, pos)[0]


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


def _count_zstd_bytes_via_cli(z_bin: str, path: str) -> Optional[int]:
    """Stream-count decompressed bytes via zstd CLI when Frame_Content_Size is absent (zero Python deps)."""
    try:
        with subprocess.Popen(
            [z_bin, "-d", "-c", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **subprocess_flags(),
        ) as proc:
            assert proc.stdout is not None
            total = 0
            buf = bytearray(4 * 1024 * 1024)
            while True:
                n = proc.stdout.readinto(buf)
                if not n:
                    break
                total += n
            rc = proc.wait()
        return total if (rc == 0 and total > 0) else None
    except (OSError, subprocess.SubprocessError):
        return None


def get_zstd_uncompressed_size(path: str, zstd_bin: Optional[str] = None, header_bytes: Optional[bytes] = None) -> int:
    # Prefer zstd -lv when CLI is available: accurately sums multi-frame (.zst / pzstd) archives in milliseconds.
    z_bin = zstd_bin or get_host_zstd()
    if z_bin:
        try:
            proc = subprocess.run(
                [z_bin, "-lv", path],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                **subprocess_flags(),
            )
            if proc.returncode == 0:
                m = _ZSTD_LV_BYTES_RE.search(proc.stdout)
                if m:
                    val = int(m.group(1))
                    if val > 0:
                        return val
                # Valid zstd stream without Frame_Content_Size: count exact bytes via CLI stream.
                counted = _count_zstd_bytes_via_cli(z_bin, path)
                if counted is not None and counted > 0:
                    return counted
            else:
                err = (proc.stderr or "").strip()
                raise RuntimeError(f"Corrupt or unreadable ZSTD archive {os.path.basename(path)}: {err}")
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    # Without zstd CLI: try Python zstandard (handles multi-frame + streaming) then RFC 8878 single-frame header.
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
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError(f"Corrupt or unreadable ZSTD archive {os.path.basename(path)}: {exc}") from exc

    if header_bytes:
        fast_sz = _parse_zstd_fcs_from_bytes(header_bytes)
        if fast_sz is not None and fast_sz > 0:
            return fast_sz

    hdr_size = read_zstd_frame_content_size(path)
    if hdr_size is not None and hdr_size > 0:
        return hdr_size

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


def classify_partition(name: str, path: str, fmt: str, header_bytes: Optional[bytes] = None) -> str:
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
    if fmt == "raw":
        if header_bytes is not None:
            if _is_raw_fs_header(header_bytes):
                return "dynamic"
        elif path and is_raw_filesystem(path):
            return "dynamic"
    if fmt == "sparse":
        return "dynamic"
    return "firmware"


def _parse_prop_file(fpath: str, result: Dict[str, str]) -> None:
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
        pass


def scan_folder(
    directory: str,
    zstd_bin: Optional[str] = None,
    max_depth: int = 3,
) -> Tuple[List[PartitionImage], Dict[str, str]]:
    """Single-pass os.scandir traversal collecting both partition images and build.prop metadata."""
    props = {"device": "", "codename": "", "version": ""}
    if not directory or not os.path.isdir(directory):
        return [], props

    img_candidates: List[Tuple[int, str, str, int]] = []
    prop_candidates: List[Tuple[int, str]] = []
    queue_dirs: List[Tuple[int, str]] = [(0, directory)]

    while queue_dirs:
        depth, cur_dir = queue_dirs.pop(0)
        try:
            with os.scandir(cur_dir) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and entry.name not in _SKIP_DIRS:
                        queue_dirs.append((depth + 1, entry.path))
                    continue
                if not entry.is_file(follow_symlinks=True):
                    continue
                name = entry.name
                if name == "build.prop" or name.endswith(".prop"):
                    prop_candidates.append((depth, entry.path))
                    continue
                base = strip_image_suffix(name)
                if not base:
                    continue
                sz = entry.stat(follow_symlinks=True).st_size
                if sz > 0:
                    img_candidates.append((depth, entry.path, name, sz))
            except OSError:
                continue

    for _, ppath in sorted(prop_candidates):
        if all(props.values()):
            break
        _parse_prop_file(ppath, props)

    resolved_zstd = zstd_bin or get_host_zstd()
    unique_items: List[Tuple[str, str, str, int, bytes]] = []
    seen_bases = set()

    for _, fpath, fname, file_size in sorted(img_candidates, key=lambda x: (x[0], x[2].lower())):
        base = strip_image_suffix(fname)
        if not base or base in seen_bases:
            continue
        try:
            with open(fpath, "rb") as fh:
                head = fh.read(1088)
        except OSError:
            continue
        seen_bases.add(base)
        unique_items.append((base, fpath, fname, file_size, head))

    zstd_items = [item for item in unique_items if _is_zstd_magic(item[4])]
    zstd_sizes: Dict[str, int] = {}
    if len(zstd_items) > 1:
        workers = min(len(zstd_items), max(2, os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(get_zstd_uncompressed_size, fpath, resolved_zstd, head): base
                for base, fpath, _fname, _fsz, head in zstd_items
            }
            for fut, base in futs.items():
                zstd_sizes[base] = fut.result()
    elif len(zstd_items) == 1:
        base, fpath, _fname, _fsz, head = zstd_items[0]
        zstd_sizes[base] = get_zstd_uncompressed_size(fpath, resolved_zstd, head)

    found: Dict[str, PartitionImage] = {}
    for base, fpath, fname, file_size, head in unique_items:
        if base in zstd_sizes:
            fmt = "zstd"
            raw_size = zstd_sizes[base]
        else:
            sp_hdr = parse_sparse_header_bytes(head)
            if sp_hdr is not None:
                fmt = "sparse"
                _, _, blk_sz, total_blks, _ = sp_hdr
                raw_size = blk_sz * total_blks or file_size
            else:
                fmt = "raw"
                raw_size = file_size

        kind = classify_partition(base, fpath, fmt, header_bytes=head)
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
    parts = sorted(found.values(), key=lambda p: (order.get(p.kind, 9), p.name))
    return parts, props


def scan_partitions(directory: str, zstd_bin: Optional[str] = None) -> List[PartitionImage]:
    parts, _ = scan_folder(directory, zstd_bin=zstd_bin)
    return parts


def detect_build_props(directory: str) -> Dict[str, str]:
    _, props = scan_folder(directory)
    return props
