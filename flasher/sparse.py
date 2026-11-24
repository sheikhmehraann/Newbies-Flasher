import struct
from pathlib import Path
from typing import Callable, Optional, Tuple

SPARSE_MAGIC = 0xED26FF3A
SPARSE_MAGIC_BYTES = b"\x3a\xff\x26\xed"

CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4

IO_CHUNK = 8 * 1024 * 1024
_ZERO_BUF = bytes(IO_CHUNK)
_ZERO_MV = memoryview(_ZERO_BUF)


def is_sparse_image(path: str) -> bool:
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 28:
        return False
    try:
        with open(p, "rb") as f:
            return f.read(4) == SPARSE_MAGIC_BYTES
    except OSError:
        return False


def parse_sparse_header_bytes(raw: bytes) -> Optional[Tuple[int, int, int, int, int]]:
    """Parse 28-byte Android sparse header -> (file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks)."""
    if len(raw) < 28:
        return None
    magic, major, _minor, file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks, _crc = (
        struct.unpack_from("<IHHHHIIII", raw, 0)
    )
    if magic != SPARSE_MAGIC or major != 1 or file_hdr_sz < 28 or chunk_hdr_sz < 12 or blk_sz == 0:
        return None
    return file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks


def read_sparse_header(path: str) -> Optional[Tuple[int, int, int, int, int]]:
    """Return (file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks) or None."""
    try:
        with open(path, "rb") as f:
            return parse_sparse_header_bytes(f.read(28))
    except OSError:
        return None


def get_sparse_uncompressed_size(path: str) -> Optional[int]:
    hdr = read_sparse_header(path)
    if hdr is None:
        return None
    _, _, blk_sz, total_blks, _ = hdr
    return blk_sz * total_blks


def _write_zeros(write_fn: Callable[[memoryview], object], count: int, cancel_check: Optional[Callable[[], bool]]) -> None:
    remaining = count
    while remaining > 0:
        if cancel_check and cancel_check():
            raise RuntimeError("Build cancelled")
        step = IO_CHUNK if remaining >= IO_CHUNK else remaining
        write_fn(_ZERO_MV[:step])
        remaining -= step


def _write_pattern(
    write_fn: Callable[[memoryview], object],
    pattern: bytes,
    count: int,
    cancel_check: Optional[Callable[[], bool]],
) -> None:
    if pattern == b"\x00\x00\x00\x00":
        _write_zeros(write_fn, count, cancel_check)
        return
    buf_bytes = min(IO_CHUNK, max(4, (count // 4) * 4))
    fill_mv = memoryview(pattern * (buf_bytes // 4))
    remaining = count
    while remaining > 0:
        if cancel_check and cancel_check():
            raise RuntimeError("Build cancelled")
        step = len(fill_mv) if remaining >= len(fill_mv) else remaining
        write_fn(fill_mv[:step])
        remaining -= step


def unsparse_to_stream(
    src_path: str,
    write_fn: Callable[[memoryview], object],
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    """Stream unsparsed raw image bytes directly into write_fn (zero intermediate disk file)."""
    hdr = read_sparse_header(src_path)
    if hdr is None:
        raise ValueError(f"Invalid Android sparse image: {src_path}")

    file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks = hdr
    expected_size = blk_sz * total_blks
    extra_chunk_hdr = chunk_hdr_sz - 12
    read_buf = bytearray(IO_CHUNK)
    read_mv = memoryview(read_buf)

    with open(src_path, "rb") as src:
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
                    want = IO_CHUNK if remaining >= IO_CHUNK else remaining
                    n = src.readinto(read_mv[:want])
                    if not n:
                        raise ValueError(f"Unexpected EOF in sparse RAW chunk: {src_path}")
                    write_fn(read_mv[:n])
                    remaining -= n
                written += out_bytes

            elif chunk_type == CHUNK_TYPE_FILL:
                if data_sz != 4:
                    raise ValueError(f"Sparse FILL chunk must carry 4 bytes in {src_path}")
                pattern = src.read(4)
                if len(pattern) < 4:
                    raise ValueError(f"Truncated sparse FILL payload in {src_path}")
                _write_pattern(write_fn, pattern, out_bytes, cancel_check)
                written += out_bytes

            elif chunk_type == CHUNK_TYPE_DONT_CARE:
                if data_sz != 0:
                    src.seek(data_sz, 1)
                _write_zeros(write_fn, out_bytes, cancel_check)
                written += out_bytes

            elif chunk_type == CHUNK_TYPE_CRC32:
                if data_sz > 0:
                    src.seek(data_sz, 1)

            else:
                raise ValueError(f"Unsupported sparse chunk type 0x{chunk_type:04X} in {src_path}")

        if written < expected_size:
            _write_zeros(write_fn, expected_size - written, cancel_check)
            written = expected_size

    return written


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
    read_buf = bytearray(IO_CHUNK)
    read_mv = memoryview(read_buf)

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
                    want = IO_CHUNK if remaining >= IO_CHUNK else remaining
                    n = src.readinto(read_mv[:want])
                    if not n:
                        raise ValueError(f"Unexpected EOF in sparse RAW chunk: {src_path}")
                    dst.write(read_mv[:n])
                    remaining -= n
                written += out_bytes

            elif chunk_type == CHUNK_TYPE_FILL:
                if data_sz != 4:
                    raise ValueError(f"Sparse FILL chunk must carry 4 bytes in {src_path}")
                pattern = src.read(4)
                if len(pattern) < 4:
                    raise ValueError(f"Truncated sparse FILL payload in {src_path}")
                # Write explicit fill bytes rather than seeking so zero-fills overwrite prior data.
                _write_pattern(dst.write, pattern, out_bytes, cancel_check)
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
