import struct
from pathlib import Path
from typing import Optional

AVB_MAGIC = b"AVB0"
AVB_HEADER_MIN_SIZE = 256
AVB_FLAGS_OFFSET = 120
AVB_FLAG_DISABLE_VERITY = 0x01
AVB_FLAG_DISABLE_VERIFICATION = 0x02
AVB_ALL_DISABLE_FLAGS = AVB_FLAG_DISABLE_VERITY | AVB_FLAG_DISABLE_VERIFICATION

VALID_MODES = ("skip", "disable", "enable")


def read_vbmeta_flags(vbmeta_path: str) -> Optional[int]:
    p = Path(vbmeta_path)
    if not p.is_file() or p.stat().st_size < AVB_HEADER_MIN_SIZE:
        return None
    try:
        with open(p, "rb") as f:
            header = f.read(AVB_HEADER_MIN_SIZE)
        if not header.startswith(AVB_MAGIC):
            return None
        return struct.unpack(">I", header[AVB_FLAGS_OFFSET : AVB_FLAGS_OFFSET + 4])[0]
    except OSError:
        return None


def is_top_level_vbmeta(partition_name: str) -> bool:
    """Only top-level vbmeta accepts non-zero AVB flags; libavb rejects flags on chained vbmeta_*."""
    return partition_name.lower() == "vbmeta"


def patch_vbmeta_flags(vbmeta_path: str, mode: str) -> bool:
    mode_norm = mode.lower().strip()
    if mode_norm not in VALID_MODES:
        raise ValueError(f"Invalid AVB mode '{mode}'. Expected one of: {', '.join(VALID_MODES)}")
    if mode_norm == "skip":
        return False

    p = Path(vbmeta_path)
    if not p.is_file() or p.stat().st_size < AVB_HEADER_MIN_SIZE:
        return False

    with open(p, "r+b") as f:
        header = f.read(AVB_HEADER_MIN_SIZE)
        if not header.startswith(AVB_MAGIC):
            return False

        current_flags = struct.unpack(">I", header[AVB_FLAGS_OFFSET : AVB_FLAGS_OFFSET + 4])[0]
        if mode_norm == "disable":
            new_flags = current_flags | AVB_ALL_DISABLE_FLAGS
        else:
            new_flags = current_flags & ~AVB_ALL_DISABLE_FLAGS

        f.seek(AVB_FLAGS_OFFSET)
        f.write(struct.pack(">I", new_flags))
        return True
