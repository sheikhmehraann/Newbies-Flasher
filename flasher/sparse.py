import struct
from pathlib import Path
from typing import Callable, Optional, Tuple

SPARSE_MAGIC = 0xED26FF3A
SPARSE_MAGIC_BYTES = b"\x3a\xff\x26\xed"

CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4

IO_CHUNK = 4 * 1024 * 1024


def is_sparse_image(path: str) -> bool:
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 28:
        return False
    try:
        with open(p, "rb") as f:
            return f.read(4) == SPARSE_MAGIC_BYTES
    except OSError:
        return False


def read_sparse_header(path: str) -> Optional[Tuple[int, int, int, int, int]]:
    """Return (file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks) or None."""
    try:
        with open(path, "rb") as f:
            raw = f.read(28)
            if len(raw) < 28:
                return None
            magic, major, _minor, file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks, _crc = (
                struct.unpack("<IHHHHIIII", raw)
            )
            if magic != SPARSE_MAGIC or major != 1 or file_hdr_sz < 28 or chunk_hdr_sz < 12 or blk_sz == 0:
                return None
            return file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks
    except OSError:
        return None


def get_sparse_uncompressed_size(path: str) -> Optional[int]:
    hdr = read_sparse_header(path)
    if hdr is None:
        return None
    _, _, blk_sz, total_blks, _ = hdr
    return blk_sz * total_blks


def unsparse_image(
    src_path: str,
    dst_path: str,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    """Convert an Android sparse image to a raw image on disk. Returns final raw size."""
    hdr = read_sparse_header(src_path)
    if hdr is None:
        raise ValueError(f"Invalid Android sparse image: {src_path}")

    file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks = hdr
    expected_size = blk_sz * total_blks
    extra_chunk_hdr = chunk_hdr_sz - 12

    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        src.seek(file_hdr_sz)
        written = 0

        for _ in range(total_chunks):
            if cancel_check and cancel_check():
                raise RuntimeError("Build cancelled")

            ch_raw = src.read(12)
            if len(ch_raw) < 12:
                raise ValueError(f"Truncated sparse chunk header in {src_path}")
            chunk_type, _reserved, chunk_sz, total_sz = struct.unpack("<HHII", ch_raw)
            if total_sz < chunk_hdr_sz:
                raise ValueError(f"Malformed sparse chunk size ({total_sz} < {chunk_hdr_sz}) in {src_path}")
            if extra_chunk_hdr > 0:
                src.seek(extra_chunk_hdr, 1)

            data_sz = total_sz - chunk_hdr_sz
            out_bytes = chunk_sz * blk_sz

            if chunk_type == CHUNK_TYPE_RAW:
                if data_sz != out_bytes:
                    raise ValueError(f"Sparse RAW chunk size mismatch in {src_path}")
                remaining = out_bytes
                while remaining > 0:
                    if cancel_check and cancel_check():
                        raise RuntimeError("Build cancelled")
                    buf = src.read(min(IO_CHUNK, remaining))
                    if not buf:
                        raise ValueError(f"Unexpected EOF in sparse RAW chunk: {src_path}")
                    dst.write(buf)
                    remaining -= len(buf)
                written += out_bytes

            elif chunk_type == CHUNK_TYPE_FILL:
                if data_sz != 4:
                    raise ValueError(f"Sparse FILL chunk must carry 4 bytes in {src_path}")
                pattern = src.read(4)
                if len(pattern) < 4:
                    raise ValueError(f"Truncated sparse FILL payload in {src_path}")
                # Always write explicit fill bytes rather than seeking so zero-fills are preserved.
                unit_count = max(1, IO_CHUNK // 4)
                fill_buf = pattern * unit_count
                remaining = out_bytes
                while remaining > 0:
                    if cancel_check and cancel_check():
                        raise RuntimeError("Build cancelled")
                    to_write = min(len(fill_buf), remaining)
                    dst.write(fill_buf[:to_write])
                    remaining -= to_write
                written += out_bytes

            elif chunk_type == CHUNK_TYPE_DONT_CARE:
                if data_sz != 0:
                    src.seek(data_sz, 1)
                written += out_bytes
                dst.seek(written)

            elif chunk_type == CHUNK_TYPE_CRC32:
                if data_sz > 0:
                    src.seek(data_sz, 1)

            else:
                raise ValueError(f"Unsupported sparse chunk type 0x{chunk_type:04X} in {src_path}")

        dst.truncate(max(written, expected_size))

    return max(written, expected_size)
