import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .avb import is_top_level_vbmeta, patch_vbmeta_flags
from .partitions import PartitionImage, scan_partitions
from .platform_utils import (
    ROOT_DIR,
    get_device_binaries,
    get_host_zstd,
    has_python_zstandard,
    subprocess_flags,
)
from .recovery_script import generate_update_binary
from .sparse import get_sparse_uncompressed_size, unsparse_image, unsparse_to_stream

ZIP64_THRESHOLD = 0xFFFFFFFF - (16 * 1024 * 1024)
COPY_BUF = 16 * 1024 * 1024
ZIP_WRITE_BUF = 4 * 1024 * 1024
PIPE_BUF = 1 * 1024 * 1024
LARGE_IMAGE_BYTES = 64 * 1024 * 1024
ALREADY_COMPRESSED_EXTS = (".zst", ".gz", ".xz", ".bz2", ".lz4", ".zip")


@dataclass
class BuildConfig:
    imgs_dir: str
    output_zip: str
    device: str = "Infinix GT 20 Pro"
    codename: str = "X6871"
    version: str = "1.0"
    maintainer: str = "User"
    vbmeta_mode: str = "skip"
    zstd_level: int = 1
    zip_level: int = 1
    partitions: Optional[List[PartitionImage]] = None


@dataclass
class BuildResult:
    output_zip: str
    size_bytes: int
    elapsed_sec: float
    device: str
    codename: str
    version: str
    maintainer: str
    vbmeta_mode: str
    zstd_level: int
    zip_level: int
    dynamic_partitions: List[str] = field(default_factory=list)
    bootchain_partitions: List[str] = field(default_factory=list)
    firmware_partitions: List[str] = field(default_factory=list)
    bootloader_partitions: List[str] = field(default_factory=list)
    super_raw: bool = False

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


def format_output_filename(device: str, version: str, codename: str = "") -> str:
    def clean(val: str) -> str:
        s = re.sub(r"[\s_]+", "-", str(val or "").strip())
        s = re.sub(r"[^a-zA-Z0-9.-]", "", s)
        return re.sub(r"-+", "-", s).strip("-")

    code = clean(codename)
    dev = clean(device) or "Device"
    ver = clean(version) or "1.0"
    primary = code or dev
    return f"{primary}-{ver}-Flashable.zip"


def sanitize_output_path(output_zip: str) -> str:
    resolved = output_zip if os.path.isabs(output_zip) else str(ROOT_DIR / output_zip)
    out_dir = os.path.dirname(resolved)
    base = os.path.basename(resolved)
    clean_base = re.sub(r"[\s_]+", "-", base)
    clean_base = re.sub(r"-+", "-", clean_base).strip("-") or "Flashable.zip"
    if not clean_base.lower().endswith(".zip"):
        clean_base += ".zip"
    return os.path.join(out_dir, clean_base) if out_dir else clean_base


def _write_all(fh, data: memoryview) -> None:
    """Loop until the entire buffer is written so pipe short-writes never truncate streams."""
    view = memoryview(data)
    total = len(view)
    sent = 0
    while sent < total:
        n = fh.write(view[sent:])
        if n is None:
            return
        if n <= 0:
            raise OSError("Short write to zstd pipe")
        sent += n


def _wait_proc(proc: subprocess.Popen, cancel_event: Optional[threading.Event]) -> Tuple[int, bytes]:
    while True:
        if cancel_event and cancel_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            raise RuntimeError("Build cancelled")
        try:
            proc.wait(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            continue
    err_bytes = proc.stderr.read() if proc.stderr else b""
    return proc.returncode, err_bytes


def _zstd_cli_args(zstd_bin: str, level: int, threads: int, stream_size: int = 0) -> List[str]:
    eff_threads = min(threads, 4) if level >= 20 else threads
    args = [zstd_bin, f"-T{max(1, eff_threads)}", "-f", "-q"]
    if stream_size > 0:
        args.append(f"--stream-size={stream_size}")
    if level >= 20:
        args.extend([f"-{min(22, level)}", "--ultra"])
    else:
        args.append(f"-{max(1, level)}")
    return args


def _run_zstd_compress(
    src: str,
    dst: str,
    level: int,
    threads: int,
    zstd_bin: Optional[str],
    cancel_event: Optional[threading.Event] = None,
) -> None:
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Build cancelled")

    if zstd_bin:
        # Retry once with -T1 if ultra-level multi-threaded compression runs short on host RAM.
        attempts = [threads] + ([1] if (level >= 20 and threads > 1) else [])
        last_err = ""
        for t_cnt in attempts:
            args = _zstd_cli_args(zstd_bin, level, t_cnt) + [src, "-o", dst]
            with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **subprocess_flags()) as proc:
                rc, err_bytes = _wait_proc(proc, cancel_event)
            if rc == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return
            last_err = err_bytes.decode("utf-8", errors="replace").strip()
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
        if not has_python_zstandard():
            raise RuntimeError(f"zstd compression failed for {os.path.basename(src)}: {last_err}")

    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "No working zstd binary found and Python 'zstandard' package is not installed."
        ) from exc

    c_level = max(1, min(level, 22))
    py_threads = 1 if c_level >= 20 else threads
    try:
        cctx = zstandard.ZstdCompressor(level=c_level, threads=py_threads, write_checksum=True, write_content_size=True)
    except TypeError:
        cctx = zstandard.ZstdCompressor(level=c_level, write_checksum=True, write_content_size=True)
    src_size = os.path.getsize(src)
    buf = bytearray(COPY_BUF)
    mv = memoryview(buf)
    with open(src, "rb") as ifh, open(dst, "wb") as ofh:
        with cctx.stream_writer(ofh, size=src_size, closefd=False) as writer:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Build cancelled")
                n = ifh.readinto(buf)
                if not n:
                    break
                writer.write(mv[:n])


def _run_sparse_to_zstd(
    src: str,
    dst: str,
    level: int,
    threads: int,
    zstd_bin: Optional[str],
    stage_dir: str,
    cancel_event: Optional[threading.Event] = None,
) -> int:
    """Stream Android sparse unsparsing directly into zstd without writing a raw temp image."""
    cancel_check = (lambda: bool(cancel_event and cancel_event.is_set()))
    expected_sz = get_sparse_uncompressed_size(src) or 0

    if zstd_bin and expected_sz > 0:
        args = _zstd_cli_args(zstd_bin, level, threads, stream_size=expected_sz) + ["-o", dst]
        try:
            with subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=PIPE_BUF,
                **subprocess_flags(),
            ) as proc:
                assert proc.stdin is not None
                try:
                    raw_sz = unsparse_to_stream(src, lambda mv: _write_all(proc.stdin, mv), cancel_check=cancel_check)
                    proc.stdin.flush()
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                rc, _ = _wait_proc(proc, cancel_event)
            if rc == 0 and raw_sz == expected_sz and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return raw_sz
        except OSError:
            pass
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass

    if not zstd_bin and has_python_zstandard() and expected_sz > 0:
        import zstandard
        c_level = max(1, min(level, 22))
        py_threads = 1 if c_level >= 20 else threads
        try:
            cctx = zstandard.ZstdCompressor(level=c_level, threads=py_threads, write_checksum=True, write_content_size=True)
        except TypeError:
            cctx = zstandard.ZstdCompressor(level=c_level, write_checksum=True, write_content_size=True)
        with open(dst, "wb") as ofh:
            with cctx.stream_writer(ofh, size=expected_sz, closefd=False) as writer:
                return unsparse_to_stream(src, writer.write, cancel_check=cancel_check)

    # Fallback if an old system zstd does not support --stream-size or hits a pipe error.
    tmp_raw = os.path.join(stage_dir, f"{os.path.basename(dst)}.raw")
    try:
        raw_sz = unsparse_image(src, tmp_raw, cancel_check=cancel_check)
        _run_zstd_compress(tmp_raw, dst, level, threads, zstd_bin, cancel_event)
        return raw_sz
    finally:
        if os.path.exists(tmp_raw):
            try:
                os.remove(tmp_raw)
            except OSError:
                pass


def _run_zstd_decompress(
    src: str,
    dst: str,
    zstd_bin: Optional[str],
    cancel_event: Optional[threading.Event] = None,
) -> int:
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Build cancelled")

    if zstd_bin:
        with subprocess.Popen(
            [zstd_bin, "-d", "-f", "-q", src, "-o", dst],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **subprocess_flags(),
        ) as proc:
            rc, err_bytes = _wait_proc(proc, cancel_event)
        if rc == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return os.path.getsize(dst)
        err = err_bytes.decode("utf-8", errors="replace").strip()
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        if not has_python_zstandard():
            raise RuntimeError(f"zstd decompression failed for {os.path.basename(src)}: {err}")

    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "No working zstd binary found and Python 'zstandard' package is not installed."
        ) from exc

    dctx = zstandard.ZstdDecompressor()
    with open(src, "rb") as ifh, open(dst, "wb") as ofh:
        with dctx.stream_reader(ifh) as reader:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Build cancelled")
                buf = reader.read(COPY_BUF)
                if not buf:
                    break
                ofh.write(buf)
    return os.path.getsize(dst)


def preview_update_binary(cfg: BuildConfig) -> str:
    zstd_bin = get_host_zstd()
    if cfg.partitions is not None:
        parts = cfg.partitions
    elif cfg.imgs_dir and os.path.isdir(cfg.imgs_dir):
        parts = scan_partitions(cfg.imgs_dir, zstd_bin=zstd_bin)
    else:
        parts = []
    selected = [p for p in parts if p.selected]

    use_zstd = cfg.zstd_level > 0
    dyn_specs = [(p.name, p.raw_size) for p in selected if p.kind == "dynamic"]
    boot_specs = [(p.name, p.raw_size) for p in selected if p.kind == "bootchain"]
    fw_specs = [(p.name, p.raw_size) for p in selected if p.kind in ("firmware", "bootloader")]
    super_parts = [p for p in selected if p.kind == "super_raw"]
    super_raw = bool(super_parts)
    super_raw_size = super_parts[0].raw_size if super_parts else 0

    return generate_update_binary(
        device=cfg.device,
        version=cfg.version,
        codename=cfg.codename,
        maintainer=cfg.maintainer,
        dynamic_specs=dyn_specs,
        super_raw=super_raw,
        bootchain_specs=boot_specs,
        firmware_specs=fw_specs,
        vbmeta_mode=cfg.vbmeta_mode,
        use_zstd=use_zstd,
        super_raw_size=super_raw_size,
    )


def _entry_pack_priority(rel_path: str, size: int) -> Tuple[int, int, str]:
    """Pack META-INF and small partitions first so they remain well below 4 GiB offset."""
    if rel_path.startswith("META-INF/"):
        return (0, size, rel_path)
    return (1, size, rel_path)


class _StreamingZipWriter:
    """Buffered ZIP writer that keeps META-INF/small files first and streams large files as they finish."""

    def __init__(
        self,
        output_zip: str,
        zip_level: int,
        cancel_event: Optional[threading.Event],
    ):
        self.output_zip = output_zip
        self.zip_level = max(0, min(9, int(zip_level)))
        self.cancel_event = cancel_event
        self.written_bytes = 0
        self.start_ts = time.monotonic()
        self.last_prog_ts = 0.0
        self._dir_set: Set[str] = set()
        self._buf = bytearray(COPY_BUF)
        self._mv = memoryview(self._buf)
        self._fh = None
        self._zf: Optional[zipfile.ZipFile] = None

    def __enter__(self) -> "_StreamingZipWriter":
        out_dir = os.path.dirname(self.output_zip)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        if os.path.exists(self.output_zip):
            os.remove(self.output_zip)
        default_comp = zipfile.ZIP_STORED if self.zip_level <= 0 else zipfile.ZIP_DEFLATED
        zip_kwargs = {"compresslevel": self.zip_level} if self.zip_level > 0 else {}
        self._fh = open(self.output_zip, "wb", buffering=ZIP_WRITE_BUF)
        self._zf = zipfile.ZipFile(self._fh, "w", compression=default_comp, allowZip64=True, **zip_kwargs)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._zf is not None:
                self._zf.close()
        finally:
            if self._fh is not None:
                self._fh.close()

    def _ensure_dirs(self, arc: str) -> None:
        assert self._zf is not None
        parts = arc.split("/")[:-1]
        cur = ""
        now_dt = time.localtime()[:6]
        for part in parts:
            cur += part + "/"
            if cur not in self._dir_set:
                self._dir_set.add(cur)
                zinfo = zipfile.ZipInfo(filename=cur, date_time=now_dt)
                zinfo.create_system = 3
                zinfo.external_attr = 0o040755 << 16
                self._zf.writestr(zinfo, b"")

    def add_file(
        self,
        fp: str,
        rp: str,
        is_exec: bool,
        on_progress: Optional[ProgressCallback] = None,
        progress_base: float = 68.0,
        progress_span: float = 31.0,
        total_est_bytes: int = 0,
    ) -> int:
        assert self._zf is not None
        assert self._fh is not None
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Build cancelled")

        arc = rp.replace("\\", "/").lstrip("/")
        self._ensure_dirs(arc)

        st = os.stat(fp)
        sz = st.st_size
        comp_type = zipfile.ZIP_STORED if (self.zip_level <= 0 or arc.lower().endswith(ALREADY_COMPRESSED_EXTS)) else zipfile.ZIP_DEFLATED

        zinfo = zipfile.ZipInfo(filename=arc)
        zinfo.date_time = time.localtime(st.st_mtime)[:6]
        zinfo.compress_type = comp_type
        # Python's ZipFile.open(ZipInfo) reads zinfo._compresslevel rather than ZipFile.compresslevel.
        zinfo._compresslevel = self.zip_level if comp_type == zipfile.ZIP_DEFLATED else None
        zinfo.create_system = 3
        zinfo.file_size = sz
        zinfo.external_attr = (0o100755 if is_exec else 0o100644) << 16

        cur_offset = self._fh.tell()
        use_zip64 = sz >= ZIP64_THRESHOLD or (cur_offset + sz) >= ZIP64_THRESHOLD
        with open(fp, "rb") as src_f, self._zf.open(zinfo, mode="w", force_zip64=use_zip64) as dst_f:
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    raise RuntimeError("Build cancelled")
                n = src_f.readinto(self._buf)
                if not n:
                    break
                dst_f.write(self._mv[:n])
                self.written_bytes += n
                if on_progress and total_est_bytes > 0:
                    now = time.monotonic()
                    if now - self.last_prog_ts >= 0.06:
                        self.last_prog_ts = now
                        dt = max(0.05, now - self.start_ts)
                        mbps = (self.written_bytes / 1048576.0) / dt
                        frac = min(1.0, self.written_bytes / total_est_bytes)
                        on_progress(min(99.0, progress_base + progress_span * frac), f"Packing {arc} ({mbps:.0f} MB/s)")
        return sz

    def add_sparse_raw(
        self,
        src_path: str,
        rp: str,
        raw_sz: int,
        on_progress: Optional[ProgressCallback] = None,
        progress_base: float = 68.0,
        progress_span: float = 31.0,
        total_est_bytes: int = 0,
    ) -> int:
        """Stream Android sparse image directly into ZIP as raw .img with zero temp file on disk."""
        assert self._zf is not None
        assert self._fh is not None
        if self.cancel_event and self.cancel_event.is_set():
            raise RuntimeError("Build cancelled")

        arc = rp.replace("\\", "/").lstrip("/")
        self._ensure_dirs(arc)

        st = os.stat(src_path)
        comp_type = zipfile.ZIP_STORED if self.zip_level <= 0 else zipfile.ZIP_DEFLATED

        zinfo = zipfile.ZipInfo(filename=arc)
        zinfo.date_time = time.localtime(st.st_mtime)[:6]
        zinfo.compress_type = comp_type
        zinfo._compresslevel = self.zip_level if comp_type == zipfile.ZIP_DEFLATED else None
        zinfo.create_system = 3
        zinfo.file_size = raw_sz
        zinfo.external_attr = 0o100644 << 16

        cur_offset = self._fh.tell()
        use_zip64 = raw_sz >= ZIP64_THRESHOLD or (cur_offset + raw_sz) >= ZIP64_THRESHOLD
        cancel_check = (lambda: bool(self.cancel_event and self.cancel_event.is_set()))

        with self._zf.open(zinfo, mode="w", force_zip64=use_zip64) as dst_f:
            def _write_chunk(mv: memoryview) -> None:
                dst_f.write(mv)
                self.written_bytes += len(mv)
                if on_progress and total_est_bytes > 0:
                    now = time.monotonic()
                    if now - self.last_prog_ts >= 0.06:
                        self.last_prog_ts = now
                        dt = max(0.05, now - self.start_ts)
                        mbps = (self.written_bytes / 1048576.0) / dt
                        frac = min(1.0, self.written_bytes / total_est_bytes)
                        on_progress(min(99.0, progress_base + progress_span * frac), f"Packing {arc} ({mbps:.0f} MB/s)")

            return unsparse_to_stream(src_path, _write_chunk, cancel_check=cancel_check)


def build_package(
    cfg: BuildConfig,
    on_log: Optional[LogCallback] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> BuildResult:
    start_ts = time.monotonic()

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            print(msg, flush=True)

    def progress(pct: float, stage: str) -> None:
        if on_progress:
            on_progress(pct, stage)

    def check_cancel() -> None:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Build cancelled")

    if not (0 <= cfg.zstd_level <= 22):
        raise ValueError("zstd_level must be between 0 and 22")
    if not (0 <= cfg.zip_level <= 9):
        raise ValueError("zip_level must be between 0 and 9")

    zstd_bin = get_host_zstd()
    use_zstd = cfg.zstd_level > 0
    ext = ".img.zst" if use_zstd else ".img"

    progress(2.0, "Scanning partitions")
    if cfg.partitions is not None:
        all_parts = cfg.partitions
    elif cfg.imgs_dir and os.path.isdir(cfg.imgs_dir):
        all_parts = scan_partitions(cfg.imgs_dir, zstd_bin=zstd_bin)
    else:
        all_parts = []
    selected = [p for p in all_parts if p.selected]
    if not selected and cfg.vbmeta_mode not in ("disable", "enable"):
        raise ValueError("No partition images selected for packaging.")

    needs_zstd = (
        (use_zstd and any(p.fmt != "zstd" for p in selected))
        or (not use_zstd and any(p.fmt == "zstd" for p in selected))
        or (cfg.vbmeta_mode in ("disable", "enable") and any(p.fmt == "zstd" and is_top_level_vbmeta(p.name) for p in selected))
    )
    if needs_zstd and not zstd_bin and not has_python_zstandard():
        raise RuntimeError(
            "zstd is not available on this system. Install zstd (or set ZSTD level to 0 for raw images)."
        )

    output_zip = sanitize_output_path(cfg.output_zip)
    log(f"Target: {cfg.device} ({cfg.codename or 'any'}) | Version: {cfg.version}")
    log(f"Selected {len(selected)} image(s) | ZSTD: {cfg.zstd_level} | ZIP: {cfg.zip_level}")
    if zstd_bin:
        log(f"zstd binary: {zstd_bin}")
    elif needs_zstd:
        log("Using Python zstandard fallback")

    stage_dir = tempfile.mkdtemp(prefix="newbies_flasher_stage_")
    cancel_check = (lambda: bool(cancel_event and cancel_event.is_set()))

    try:
        progress(6.0, "Preparing images")
        prepared: Dict[str, Tuple[PartitionImage, str, str, int]] = {}

        for p in selected:
            check_cancel()
            eff_path = p.path
            eff_fmt = p.fmt
            eff_raw = p.raw_size

            # Patch top-level vbmeta if requested (tiny file, unsparse/decompress only if needed).
            if cfg.vbmeta_mode in ("disable", "enable") and is_top_level_vbmeta(p.name):
                vb_stage = os.path.join(stage_dir, f"{p.name}.vbmeta.img")
                if eff_fmt == "sparse":
                    eff_raw = unsparse_image(eff_path, vb_stage, cancel_check=cancel_check)
                    eff_fmt = "raw"
                elif eff_fmt == "zstd":
                    eff_raw = _run_zstd_decompress(eff_path, vb_stage, zstd_bin, cancel_event)
                    eff_fmt = "raw"
                else:
                    shutil.copy2(eff_path, vb_stage)
                if patch_vbmeta_flags(vb_stage, cfg.vbmeta_mode):
                    log(f"  Patched top-level vbmeta AVB flags ({cfg.vbmeta_mode})")
                    eff_path = vb_stage
            elif cfg.vbmeta_mode in ("disable", "enable") and p.name.startswith("vbmeta_"):
                log(f"  Keeping {p.name} flags at 0 (chained vbmeta)")

            prepared[p.name] = (p, eff_path, eff_fmt, eff_raw)

        small_comp_jobs = []
        large_comp_jobs = []
        decompress_jobs = []
        # ready_files[name] = (p, path, raw_sz, is_temp, stream_sparse_raw)
        ready_files: Dict[str, Tuple[PartitionImage, str, int, bool, bool]] = {}

        # Stream already-ready images (and raw-mode sparse images) directly without redundant temp copies.
        for name, (p, eff_path, eff_fmt, eff_raw) in prepared.items():
            if use_zstd:
                if eff_fmt == "zstd":
                    ready_files[name] = (p, eff_path, eff_raw, False, False)
                else:
                    dst = os.path.join(stage_dir, f"{name}.img.zst")
                    job = (p, eff_path, dst, eff_raw, eff_fmt == "sparse")
                    if eff_raw >= LARGE_IMAGE_BYTES:
                        large_comp_jobs.append(job)
                    else:
                        small_comp_jobs.append(job)
            else:
                if eff_fmt == "zstd":
                    dst = os.path.join(stage_dir, f"{name}.img")
                    decompress_jobs.append((p, eff_path, dst))
                elif eff_fmt == "sparse":
                    ready_files[name] = (p, eff_path, eff_raw, False, True)
                else:
                    ready_files[name] = (p, eff_path, eff_raw, False, False)

        total_cpus = os.cpu_count() or 4
        total_work_items = len(small_comp_jobs) + len(large_comp_jobs) + len(decompress_jobs)
        completed_work = 0

        if small_comp_jobs:
            small_comp_jobs.sort(key=lambda item: item[3], reverse=True)
            workers = min(len(small_comp_jobs), 2 if cfg.zstd_level >= 20 else total_cpus)
            log(f"Compressing {len(small_comp_jobs)} image(s) ({workers} worker x 1 thread)...")

            def _do_small(job):
                p, src, dst, raw_sz, is_sp = job
                if is_sp:
                    raw_sz = _run_sparse_to_zstd(src, dst, cfg.zstd_level, 1, zstd_bin, stage_dir, cancel_event)
                else:
                    _run_zstd_compress(src, dst, cfg.zstd_level, 1, zstd_bin, cancel_event)
                return p, dst, raw_sz, is_sp

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_do_small, job) for job in small_comp_jobs]
                for fut in as_completed(futs):
                    p, dst, raw_sz, is_sp = fut.result()
                    completed_work += 1
                    comp_sz = os.path.getsize(dst)
                    ratio = (comp_sz / raw_sz * 100.0) if raw_sz else 0.0
                    tag = " [sparse->zstd]" if is_sp else ""
                    log(f"  {p.name}{tag}: {raw_sz / 1048576:.1f} MB -> {comp_sz / 1048576:.1f} MB ({ratio:.0f}%)")
                    ready_files[p.name] = (p, dst, raw_sz, True, False)
                    progress(10.0 + (45.0 * completed_work / max(1, total_work_items)), f"Compressed {p.name} ({completed_work}/{total_work_items})")

        if decompress_jobs:
            decompress_jobs.sort(key=lambda item: item[0].raw_size, reverse=True)
            workers = min(len(decompress_jobs), total_cpus)
            log(f"Decompressing {len(decompress_jobs)} image(s)...")

            def _do_decomp(job):
                p, src, dst = job
                raw_sz = _run_zstd_decompress(src, dst, zstd_bin, cancel_event)
                return p, dst, raw_sz

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_do_decomp, job) for job in decompress_jobs]
                for fut in as_completed(futs):
                    p, dst, raw_sz = fut.result()
                    completed_work += 1
                    log(f"  {p.name}: {raw_sz / 1048576:.1f} MB")
                    ready_files[p.name] = (p, dst, raw_sz, True, False)
                    progress(10.0 + (45.0 * completed_work / max(1, total_work_items)), f"Decompressed {p.name} ({completed_work}/{total_work_items})")

        check_cancel()

        # Build exact partition tables (exact raw sizes known from headers/files) so META-INF packs first.
        dyn_specs: List[Tuple[str, int]] = []
        dyn_names: List[str] = []
        boot_specs: List[Tuple[str, int]] = []
        boot_names: List[str] = []
        fw_specs: List[Tuple[str, int]] = []
        fw_names: List[str] = []
        bl_names: List[str] = []
        super_raw = False
        super_raw_size = 0

        for p in selected:
            exact_raw = ready_files[p.name][2] if p.name in ready_files else prepared[p.name][3]
            if p.kind == "dynamic":
                dyn_specs.append((p.name, exact_raw))
                dyn_names.append(p.name)
            elif p.kind == "super_raw":
                super_raw = True
                super_raw_size = exact_raw
            elif p.kind == "bootchain":
                boot_specs.append((p.name, exact_raw))
                boot_names.append(p.name)
            elif p.kind == "bootloader":
                fw_specs.append((p.name, exact_raw))
                bl_names.append(p.name)
            else:
                fw_specs.append((p.name, exact_raw))
                fw_names.append(p.name)

        meta_entries: List[Tuple[str, str, bool]] = []
        dev_bins = get_device_binaries()
        if use_zstd and selected and "zstd-arm64" in dev_bins:
            meta_entries.append((str(dev_bins["zstd-arm64"]), "META-INF/zstd", True))
        if dyn_specs and "lptools" in dev_bins:
            meta_entries.append((str(dev_bins["lptools"]), "META-INF/bin/lptools", True))
        if cfg.vbmeta_mode in ("disable", "enable") and "avbctl" in dev_bins:
            meta_entries.append((str(dev_bins["avbctl"]), "META-INF/bin/avbctl", True))

        update_bin_text = generate_update_binary(
            device=cfg.device,
            version=cfg.version,
            codename=cfg.codename,
            maintainer=cfg.maintainer,
            dynamic_specs=dyn_specs,
            super_raw=super_raw,
            bootchain_specs=boot_specs,
            firmware_specs=fw_specs,
            vbmeta_mode=cfg.vbmeta_mode,
            use_zstd=use_zstd,
            super_raw_size=super_raw_size,
        )
        ub_path = os.path.join(stage_dir, "update-binary")
        us_path = os.path.join(stage_dir, "updater-script")
        with open(ub_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(update_bin_text)
        with open(us_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Newbies Flasher\n")

        meta_entries.append((ub_path, "META-INF/com/google/android/update-binary", True))
        meta_entries.append((us_path, "META-INF/com/google/android/updater-script", False))

        # Order already-ready files: META-INF first, then small images before large images.
        initial_entries: List[Tuple[str, str, int, bool, bool, bool]] = []
        for fp, rp, is_exec in meta_entries:
            initial_entries.append((fp, rp, os.path.getsize(fp), is_exec, False, False))
        for name, (p, fp, raw_sz, is_temp, is_sparse_stream) in ready_files.items():
            eff_sz = raw_sz if is_sparse_stream else os.path.getsize(fp)
            initial_entries.append((fp, f"{name}{ext}", eff_sz, False, is_temp, is_sparse_stream))
        initial_entries.sort(key=lambda item: _entry_pack_priority(item[1], item[2]))

        est_initial_bytes = sum(item[2] for item in initial_entries)
        est_large_bytes = sum(job[3] // 2 for job in large_comp_jobs)
        est_total_bytes = max(1, est_initial_bytes + est_large_bytes)

        # Guard against out-of-space failure mid-pack when packing stored streams.
        try:
            out_dir = os.path.dirname(output_zip) or "."
            os.makedirs(out_dir, exist_ok=True)
            free_bytes = shutil.disk_usage(out_dir).free
            if cfg.zip_level <= 0 or use_zstd:
                min_needed = est_initial_bytes + (4 * 1024 * 1024)
                if free_bytes < min_needed:
                    raise RuntimeError(
                        f"Not enough free disk space in {out_dir}: "
                        f"need at least {min_needed / 1048576:.1f} MB, have {free_bytes / 1048576:.1f} MB."
                    )
        except OSError:
            pass

        progress(60.0 if large_comp_jobs else 68.0, "Packing ZIP archive")
        log(f"Packing {os.path.basename(output_zip)}...")

        with _StreamingZipWriter(output_zip, cfg.zip_level, cancel_event) as zw:
            def _pack_initial_entry(entry, base_pct: float, span_pct: float) -> None:
                fp, rp, sz, is_exec, is_temp, is_sparse_stream = entry
                if is_sparse_stream:
                    log(f"  {rp} [sparse->zip]: {sz / 1048576:.1f} MB")
                    zw.add_sparse_raw(fp, rp, sz, on_progress=on_progress, progress_base=base_pct, progress_span=span_pct, total_est_bytes=est_total_bytes)
                else:
                    zw.add_file(fp, rp, is_exec, on_progress=on_progress, progress_base=base_pct, progress_span=span_pct, total_est_bytes=est_total_bytes)
                    if is_temp and os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except OSError:
                            pass

            # Start compressing large partitions in background WHILE packing META-INF + small images.
            if large_comp_jobs:
                large_comp_jobs.sort(key=lambda item: item[3], reverse=True)
                # Ultra levels (20-22) use large memory windows, so run 1 large worker to avoid host OOM.
                l_workers = 1 if cfg.zstd_level >= 20 else min(len(large_comp_jobs), 2 if total_cpus >= 4 else 1)
                l_threads = max(1, total_cpus // l_workers)
                if cfg.zstd_level >= 20:
                    l_threads = min(l_threads, 4)
                log(f"Streaming {len(large_comp_jobs)} large image(s) ({l_workers} worker x {l_threads} threads)...")

                def _do_large(job):
                    p, src, dst, raw_sz, is_sp = job
                    if is_sp:
                        actual_raw = _run_sparse_to_zstd(src, dst, cfg.zstd_level, l_threads, zstd_bin, stage_dir, cancel_event)
                    else:
                        _run_zstd_compress(src, dst, cfg.zstd_level, l_threads, zstd_bin, cancel_event)
                        actual_raw = raw_sz
                    return p, dst, actual_raw, is_sp

                with ThreadPoolExecutor(max_workers=l_workers) as ex:
                    futs = [ex.submit(_do_large, job) for job in large_comp_jobs]

                    # Pack META-INF and small partitions immediately while large partitions compress.
                    for entry in initial_entries:
                        _pack_initial_entry(entry, 60.0, 10.0)

                    # Stream each large compressed image straight into the ZIP as it finishes, freeing temp disk immediately.
                    done_large = 0
                    for fut in as_completed(futs):
                        p, dst, actual_raw, is_sp = fut.result()
                        done_large += 1
                        comp_sz = os.path.getsize(dst)
                        ratio = (comp_sz / actual_raw * 100.0) if actual_raw else 0.0
                        tag = " [sparse->zstd]" if is_sp else ""
                        log(f"  {p.name}{tag}: {actual_raw / 1048576:.1f} MB -> {comp_sz / 1048576:.1f} MB ({ratio:.0f}%)")
                        arc = f"{p.name}{ext}"
                        base_pct = 70.0 + (29.0 * (done_large - 1) / len(large_comp_jobs))
                        span_pct = 29.0 / len(large_comp_jobs)
                        zw.add_file(dst, arc, False, on_progress=on_progress, progress_base=base_pct, progress_span=span_pct, total_est_bytes=zw.written_bytes + comp_sz)
                        try:
                            os.remove(dst)
                        except OSError:
                            pass
            else:
                for entry in initial_entries:
                    _pack_initial_entry(entry, 68.0, 31.0)

        elapsed = time.monotonic() - start_ts
        final_size = os.path.getsize(output_zip)
        progress(100.0, "Done")
        log(f"Done in {elapsed:.1f}s -> {output_zip} ({final_size / 1048576:.2f} MB)")

        return BuildResult(
            output_zip=output_zip,
            size_bytes=final_size,
            elapsed_sec=elapsed,
            device=cfg.device,
            codename=cfg.codename,
            version=cfg.version,
            maintainer=cfg.maintainer,
            vbmeta_mode=cfg.vbmeta_mode,
            zstd_level=cfg.zstd_level,
            zip_level=cfg.zip_level,
            dynamic_partitions=dyn_names,
            bootchain_partitions=boot_names,
            firmware_partitions=fw_names,
            bootloader_partitions=bl_names,
            super_raw=super_raw,
        )
    except Exception:
        if os.path.exists(output_zip):
            try:
                os.remove(output_zip)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
