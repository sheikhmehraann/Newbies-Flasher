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
)
from .recovery_script import generate_update_binary
from .sparse import unsparse_image

ZIP64_THRESHOLD = 0xFFFFFFFF - (16 * 1024 * 1024)
COPY_BUF = 8 * 1024 * 1024
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
    clean_super: bool = True
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

    dev = clean(device) or "Device"
    ver = clean(version) or "1.0"
    code = clean(codename)

    parts = [dev]
    if code and code.lower() not in dev.lower():
        parts.append(code)
    parts.append(ver)
    parts.append("Flashable.zip")
    return "-".join(parts)


def sanitize_output_path(output_zip: str) -> str:
    resolved = output_zip if os.path.isabs(output_zip) else str(ROOT_DIR / output_zip)
    out_dir = os.path.dirname(resolved)
    base = os.path.basename(resolved)
    clean_base = re.sub(r"[\s_]+", "-", base)
    clean_base = re.sub(r"-+", "-", clean_base).strip("-") or "Flashable.zip"
    if not clean_base.lower().endswith(".zip"):
        clean_base += ".zip"
    return os.path.join(out_dir, clean_base) if out_dir else clean_base


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
        args = [zstd_bin, f"-T{threads}", "-f", "-q"]
        if level >= 20:
            args.extend([f"-{level}", "--ultra"])
        else:
            args.append(f"-{max(1, level)}")
        args.extend([src, "-o", dst])

        with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as proc:
            while proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    proc.terminate()
                    proc.wait(timeout=5)
                    raise RuntimeError("Build cancelled")
                time.sleep(0.05)
            err_bytes = proc.stderr.read() if proc.stderr else b""
            rc = proc.returncode
        if rc == 0 and os.path.isfile(dst):
            return
        err = err_bytes.decode("utf-8", errors="replace").strip()
        if not has_python_zstandard():
            raise RuntimeError(f"zstd compression failed for {os.path.basename(src)}: {err}")

    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "No working zstd binary found and Python 'zstandard' package is not installed."
        ) from exc

    c_level = max(1, min(level, 22))
    try:
        cctx = zstandard.ZstdCompressor(level=c_level, threads=threads, write_checksum=True, write_content_size=True)
    except TypeError:
        cctx = zstandard.ZstdCompressor(level=c_level, write_checksum=True, write_content_size=True)
    src_size = os.path.getsize(src)
    with open(src, "rb") as ifh, open(dst, "wb") as ofh:
        with cctx.stream_writer(ofh, size=src_size, closefd=False) as writer:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Build cancelled")
                buf = ifh.read(COPY_BUF)
                if not buf:
                    break
                writer.write(buf)


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
        ) as proc:
            while proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    proc.terminate()
                    proc.wait(timeout=5)
                    raise RuntimeError("Build cancelled")
                time.sleep(0.05)
            err_bytes = proc.stderr.read() if proc.stderr else b""
            rc = proc.returncode
        if rc == 0 and os.path.isfile(dst):
            return os.path.getsize(dst)
        err = err_bytes.decode("utf-8", errors="replace").strip()
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
    parts = cfg.partitions if cfg.partitions is not None else scan_partitions(cfg.imgs_dir, zstd_bin=zstd_bin)
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
        clean_super=cfg.clean_super,
        super_raw_size=super_raw_size,
    )


def _entry_pack_priority(rel_path: str, size: int) -> Tuple[int, int, str]:
    """Pack META-INF and small partitions first so they remain well below 4 GiB offset."""
    if rel_path.startswith("META-INF/"):
        return (0, size, rel_path)
    return (1, size, rel_path)


def _pack_zip(
    file_entries: List[Tuple[str, str, bool]],
    output_zip: str,
    zip_level: int,
    on_progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> None:
    out_dir = os.path.dirname(output_zip)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(output_zip):
        os.remove(output_zip)

    entries: List[Tuple[str, str, int, bool]] = []
    dir_set: Set[str] = set()
    total_bytes = 0

    for fp, rp, is_exec in file_entries:
        arc = rp.replace("\\", "/").lstrip("/")
        sz = os.path.getsize(fp)
        entries.append((fp, arc, sz, is_exec))
        total_bytes += sz
        parts = arc.split("/")[:-1]
        cur = ""
        for part in parts:
            cur += part + "/"
            dir_set.add(cur)

    # Guard against out-of-space failure mid-pack when packing stored streams.
    try:
        free_bytes = shutil.disk_usage(out_dir or ".").free
        needed_est = total_bytes + (4 * 1024 * 1024)
        if zip_level <= 0 or all(rp.lower().endswith(ALREADY_COMPRESSED_EXTS) or rp.startswith("META-INF/") for _, rp, _, _ in entries):
            if free_bytes < needed_est:
                raise RuntimeError(
                    f"Not enough free disk space in {out_dir or '.'}: "
                    f"need {needed_est / 1048576:.1f} MB, have {free_bytes / 1048576:.1f} MB."
                )
    except OSError:
        pass

    entries.sort(key=lambda item: _entry_pack_priority(item[1], item[2]))
    now_dt = time.localtime()[:6]

    written_bytes = 0
    pack_start = time.monotonic()
    default_comp = zipfile.ZIP_STORED if zip_level <= 0 else zipfile.ZIP_DEFLATED
    zip_kwargs = {"compresslevel": min(9, zip_level)} if zip_level > 0 else {}

    with zipfile.ZipFile(output_zip, "w", compression=default_comp, allowZip64=True, **zip_kwargs) as zf:
        for dp in sorted(dir_set):
            zinfo = zipfile.ZipInfo(filename=dp, date_time=now_dt)
            zinfo.create_system = 3
            zinfo.external_attr = 0o040755 << 16
            zf.writestr(zinfo, b"")

        for fp, rp, sz, is_exec in entries:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Build cancelled")

            if zip_level <= 0 or rp.lower().endswith(ALREADY_COMPRESSED_EXTS):
                comp_type = zipfile.ZIP_STORED
            else:
                comp_type = zipfile.ZIP_DEFLATED

            st = os.stat(fp)
            zinfo = zipfile.ZipInfo(filename=rp)
            zinfo.date_time = time.localtime(st.st_mtime)[:6]
            zinfo.compress_type = comp_type
            zinfo.create_system = 3
            zinfo.file_size = sz
            zinfo.external_attr = (0o100755 if is_exec else 0o100644) << 16

            use_zip64 = sz >= ZIP64_THRESHOLD or (written_bytes + sz) >= ZIP64_THRESHOLD
            with open(fp, "rb") as src_f, zf.open(zinfo, mode="w", force_zip64=use_zip64) as dst_f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Build cancelled")
                    buf = src_f.read(COPY_BUF)
                    if not buf:
                        break
                    dst_f.write(buf)
                    written_bytes += len(buf)
                    if on_progress and total_bytes > 0:
                        dt = max(0.05, time.monotonic() - pack_start)
                        mbps = (written_bytes / 1048576.0) / dt
                        pct = 68.0 + (31.0 * written_bytes / total_bytes)
                        on_progress(min(99.0, pct), f"Packing {rp} ({mbps:.0f} MB/s)")


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
    all_parts = cfg.partitions if cfg.partitions is not None else scan_partitions(cfg.imgs_dir, zstd_bin=zstd_bin)
    selected = [p for p in all_parts if p.selected]
    if not selected:
        raise ValueError("No partition images selected for packaging.")

    output_zip = sanitize_output_path(cfg.output_zip)
    log(f"Target: {cfg.device} ({cfg.codename or 'any'}) | Version: {cfg.version}")
    log(f"Selected {len(selected)} image(s) | ZSTD: {cfg.zstd_level} | ZIP: {cfg.zip_level}")
    if zstd_bin:
        log(f"zstd binary: {zstd_bin}")
    elif use_zstd or any(p.fmt == "zstd" for p in selected):
        log("Using Python zstandard fallback")

    stage_dir = tempfile.mkdtemp(prefix="newbies_flasher_stage_")
    cancel_check = (lambda: bool(cancel_event and cancel_event.is_set()))

    try:
        progress(6.0, "Preparing images")
        prepared: Dict[str, Tuple[PartitionImage, str, str, int]] = {}

        for idx, p in enumerate(selected, 1):
            check_cancel()
            eff_path = p.path
            eff_fmt = p.fmt
            eff_raw = p.raw_size

            if eff_fmt == "sparse":
                log(f"  Unsparsing {p.filename}...")
                raw_out = os.path.join(stage_dir, f"{p.name}.raw.img")
                eff_raw = unsparse_image(eff_path, raw_out, cancel_check=cancel_check)
                eff_path = raw_out
                eff_fmt = "raw"

            if cfg.vbmeta_mode in ("disable", "enable") and is_top_level_vbmeta(p.name):
                vb_stage = os.path.join(stage_dir, f"{p.name}.vbmeta.img")
                if eff_fmt == "zstd":
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
            progress(6.0 + (10.0 * idx / len(selected)), f"Prepared {p.name}")

        compress_jobs = []
        decompress_jobs = []
        final_files: Dict[str, Tuple[PartitionImage, str, int]] = {}

        # Stream already-ready images directly from source path without redundant temp copies.
        for name, (p, eff_path, eff_fmt, eff_raw) in prepared.items():
            if use_zstd:
                if eff_fmt == "zstd":
                    final_files[name] = (p, eff_path, eff_raw)
                else:
                    dst = os.path.join(stage_dir, f"{name}.img.zst")
                    compress_jobs.append((p, eff_path, dst, eff_raw))
            else:
                if eff_fmt == "zstd":
                    dst = os.path.join(stage_dir, f"{name}.img")
                    decompress_jobs.append((p, eff_path, dst))
                else:
                    final_files[name] = (p, eff_path, eff_raw)

        total_cpus = os.cpu_count() or 4

        if compress_jobs:
            compress_jobs.sort(key=lambda item: item[3], reverse=True)
            workers = min(len(compress_jobs), max(1, total_cpus // 2 if total_cpus >= 4 else total_cpus))
            threads_each = max(1, total_cpus // workers)
            log(f"Compressing {len(compress_jobs)} image(s) ({workers} worker x {threads_each} threads)...")

            def _do_comp(job):
                p, src, dst, raw_sz = job
                _run_zstd_compress(src, dst, cfg.zstd_level, threads_each, zstd_bin, cancel_event)
                return p, dst, raw_sz

            done = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_do_comp, job) for job in compress_jobs]
                for fut in as_completed(futs):
                    p, dst, raw_sz = fut.result()
                    done += 1
                    comp_sz = os.path.getsize(dst)
                    ratio = (comp_sz / raw_sz * 100.0) if raw_sz else 0.0
                    log(f"  {p.name}: {raw_sz / 1048576:.1f} MB -> {comp_sz / 1048576:.1f} MB ({ratio:.0f}%)")
                    final_files[p.name] = (p, dst, raw_sz)
                    progress(16.0 + (48.0 * done / len(compress_jobs)), f"Compressed {p.name} ({done}/{len(compress_jobs)})")

        if decompress_jobs:
            workers = min(len(decompress_jobs), total_cpus)
            log(f"Decompressing {len(decompress_jobs)} image(s)...")

            def _do_decomp(job):
                p, src, dst = job
                raw_sz = _run_zstd_decompress(src, dst, zstd_bin, cancel_event)
                return p, dst, raw_sz

            done = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_do_decomp, job) for job in decompress_jobs]
                for fut in as_completed(futs):
                    p, dst, raw_sz = fut.result()
                    done += 1
                    log(f"  {p.name}: {raw_sz / 1048576:.1f} MB")
                    final_files[p.name] = (p, dst, raw_sz)
                    progress(16.0 + (48.0 * done / len(decompress_jobs)), f"Decompressed {p.name} ({done}/{len(decompress_jobs)})")

        check_cancel()

        progress(65.0, "Writing recovery installer")
        dyn_specs: List[Tuple[str, int]] = []
        dyn_names: List[str] = []
        boot_specs: List[Tuple[str, int]] = []
        boot_names: List[str] = []
        fw_specs: List[Tuple[str, int]] = []
        fw_names: List[str] = []
        bl_names: List[str] = []
        super_raw = False
        super_raw_size = 0

        zip_entries: List[Tuple[str, str, bool]] = []

        for p in selected:
            _, final_path, exact_raw = final_files[p.name]
            zip_entries.append((final_path, f"{p.name}{ext}", False))
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

        dev_bins = get_device_binaries()
        if use_zstd and "zstd-arm64" in dev_bins:
            zip_entries.append((str(dev_bins["zstd-arm64"]), "META-INF/zstd", True))
        if dyn_specs and "lptools" in dev_bins:
            zip_entries.append((str(dev_bins["lptools"]), "META-INF/bin/lptools", True))
        if cfg.vbmeta_mode in ("disable", "enable") and "avbctl" in dev_bins:
            zip_entries.append((str(dev_bins["avbctl"]), "META-INF/bin/avbctl", True))

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
            clean_super=cfg.clean_super,
            super_raw_size=super_raw_size,
        )
        ub_path = os.path.join(stage_dir, "update-binary")
        us_path = os.path.join(stage_dir, "updater-script")
        with open(ub_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(update_bin_text)
        with open(us_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Newbies Flasher\n")

        zip_entries.append((ub_path, "META-INF/com/google/android/update-binary", True))
        zip_entries.append((us_path, "META-INF/com/google/android/updater-script", False))

        progress(68.0, "Packing ZIP archive")
        log(f"Packing {os.path.basename(output_zip)}...")
        _pack_zip(zip_entries, output_zip, cfg.zip_level, on_progress=on_progress, cancel_event=cancel_event)

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
