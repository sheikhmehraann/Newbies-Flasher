#!/usr/bin/env python3
import ctypes
import os
import queue
import sys
import threading
import time
from dataclasses import replace
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from flasher import __version__
from flasher.builder import (
    BuildConfig,
    BuildResult,
    build_package,
    format_output_filename,
    preview_update_binary,
)
from flasher.partitions import PartitionImage, detect_build_props, scan_partitions
from flasher.platform_utils import (
    detect_host_os,
    get_host_zstd,
    has_python_zstandard,
    reveal_in_file_manager,
)

DEFAULT_DEVICE = "Infinix GT 20 Pro"
DEFAULT_CODENAME = "X6871"
DEFAULT_VERSION = "1.0"
DEFAULT_MAINTAINER = "User"


def _enable_windows_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _apply_windows_dark_chrome(window: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        c_int = ctypes.c_int
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        dwm(hwnd, 20, ctypes.byref(c_int(1)), ctypes.sizeof(c_int))
        dwm(hwnd, 33, ctypes.byref(c_int(2)), ctypes.sizeof(c_int))
        dwm(hwnd, 35, ctypes.byref(c_int(0x001A1614)), ctypes.sizeof(c_int))
    except Exception:
        pass


def _fmt_size(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / (1024 ** 3):.2f} GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


class NewbiesFlasherApp:
    BG = "#121418"
    SURFACE = "#1a1d24"
    SURFACE_ALT = "#212630"
    BORDER = "#2b313d"
    INPUT = "#101216"
    TEXT = "#edf1f7"
    MUTED = "#8c96a8"
    ACCENT = "#2575e6"
    ACCENT_HOVER = "#3988f5"
    DANGER = "#c44545"
    DANGER_HOVER = "#d85757"
    OK = "#38b377"
    WARN = "#e0a538"
    DYNAMIC_FG = "#79b8ff"
    SUPER_FG = "#b392f0"
    BOOT_FG = "#69d49d"
    CONSOLE = "#0d0f13"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Newbies Flasher v{__version__}")

        # Fit cleanly inside any display, including 1360x768 and 1280x720 laptops.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        win_w = min(1120, max(860, sw - 80))
        win_h = min(650, max(540, sh - 100))
        pos_x = max(0, (sw - win_w) // 2)
        pos_y = max(0, (sh - win_h) // 2 - 16)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(840, 520)
        self.root.configure(bg=self.BG)
        _apply_windows_dark_chrome(self.root)

        self.ui_font = "Segoe UI" if sys.platform == "win32" else ("SF Pro Text" if sys.platform == "darwin" else "TkDefaultFont")
        self.mono_font = "Consolas" if sys.platform == "win32" else ("Menlo" if sys.platform == "darwin" else "Monospace")

        self.partitions: List[PartitionImage] = []
        self.output_dir: str = os.path.join(ROOT_DIR, "output")
        self.last_output: Optional[str] = None
        self.cancel_event = threading.Event()
        self.build_thread: Optional[threading.Thread] = None
        self.build_start_ts: float = 0.0
        self.scan_after_id: Optional[str] = None
        self.poll_after_id: Optional[str] = None
        self.scan_seq = 0
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()

        self._setup_styles()
        self._build_layout()
        self._bind_shortcuts()
        self._update_output_preview()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_destroy)
        self._poll_queue()

    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "NF.Treeview",
            background=self.INPUT,
            foreground=self.TEXT,
            fieldbackground=self.INPUT,
            bordercolor=self.BORDER,
            rowheight=24,
            font=(self.ui_font, 9),
        )
        style.configure(
            "NF.Treeview.Heading",
            background=self.SURFACE_ALT,
            foreground=self.MUTED,
            bordercolor=self.BORDER,
            relief="flat",
            padding=(6, 4),
            font=(self.ui_font, 8, "bold"),
        )
        style.map(
            "NF.Treeview",
            background=[("selected", "#243550")],
            foreground=[("selected", self.TEXT)],
        )
        style.configure(
            "NF.Horizontal.TProgressbar",
            troughcolor=self.INPUT,
            background=self.ACCENT,
            bordercolor=self.BORDER,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=7,
        )
        style.configure(
            "NF.TCombobox",
            fieldbackground=self.INPUT,
            background=self.SURFACE_ALT,
            foreground=self.TEXT,
            arrowcolor=self.TEXT,
            bordercolor=self.BORDER,
            padding=4,
        )
        style.map(
            "NF.TCombobox",
            fieldbackground=[("readonly", self.INPUT)],
            selectbackground=[("readonly", self.SURFACE_ALT)],
            selectforeground=[("readonly", self.TEXT)],
        )
        style.configure(
            "NF.Horizontal.TScale",
            background=self.SURFACE,
            troughcolor=self.INPUT,
            bordercolor=self.BORDER,
        )

    def _build_layout(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # Top Bar
        top = tk.Frame(outer, bg=self.BG)
        top.pack(fill=tk.X, pady=(0, 8))

        tk.Label(
            top, text="Newbies Flasher", font=(self.ui_font, 15, "bold"),
            fg=self.TEXT, bg=self.BG,
        ).pack(side=tk.LEFT)

        tk.Label(
            top, text=f"v{__version__}", font=(self.ui_font, 9),
            fg=self.MUTED, bg=self.BG,
        ).pack(side=tk.LEFT, padx=(6, 12), pady=(4, 0))

        zstd_status = "zstd: ready" if get_host_zstd() else ("zstd: python" if has_python_zstandard() else "zstd: missing")
        tk.Label(
            top,
            text=f"{detect_host_os()} | {zstd_status}",
            font=(self.ui_font, 8, "bold"),
            fg=self.OK if "missing" not in zstd_status else self.WARN,
            bg=self.SURFACE,
            padx=8,
            pady=3,
        ).pack(side=tk.LEFT, pady=(2, 0))

        self._btn(top, "Open Output", self._open_output, bg=self.SURFACE_ALT, hover="#2e3543", pad=(10, 4), size=9).pack(side=tk.RIGHT)
        self._btn(top, "Preview Script", self._open_script_preview, bg=self.SURFACE_ALT, hover="#2e3543", pad=(10, 4), size=9).pack(side=tk.RIGHT, padx=(0, 6))

        # Main Two-Pane Workspace
        main_pane = tk.Frame(outer, bg=self.BG)
        main_pane.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        # Left Pane: Folder picker + Partition checklist
        left_card = tk.Frame(main_pane, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER, padx=10, pady=8)
        left_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        left_head = tk.Frame(left_card, bg=self.SURFACE)
        left_head.pack(fill=tk.X)
        tk.Label(left_head, text="Partition Images", font=(self.ui_font, 10, "bold"), fg=self.TEXT, bg=self.SURFACE).pack(side=tk.LEFT)
        self.scan_lbl = tk.Label(left_head, text="Select a folder with .img / .img.zst files", font=(self.ui_font, 8), fg=self.MUTED, bg=self.SURFACE)
        self.scan_lbl.pack(side=tk.RIGHT)

        path_row = tk.Frame(left_card, bg=self.SURFACE)
        path_row.pack(fill=tk.X, pady=(6, 6))

        self.path_var = tk.StringVar()
        self.path_entry = tk.Entry(
            path_row,
            textvariable=self.path_var,
            font=(self.ui_font, 9),
            bg=self.INPUT,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
        )
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 6))
        self.path_entry.bind("<KeyRelease>", self._schedule_scan)

        self._btn(path_row, "Browse...", self._browse_folder, bg=self.SURFACE_ALT, hover="#2e3543", pad=(10, 3), size=9).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(path_row, "Rescan", self._trigger_scan_now, bg=self.SURFACE_ALT, hover="#2e3543", pad=(8, 3), size=9).pack(side=tk.LEFT)

        tree_wrap = tk.Frame(left_card, bg=self.SURFACE)
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        cols = ("use", "name", "kind", "fmt", "raw", "disk")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=9, style="NF.Treeview", selectmode="browse")
        self.tree.heading("use", text="Use")
        self.tree.heading("name", text="Partition")
        self.tree.heading("kind", text="Type")
        self.tree.heading("fmt", text="Fmt")
        self.tree.heading("raw", text="Raw")
        self.tree.heading("disk", text="File")

        self.tree.column("use", width=44, anchor=tk.CENTER, stretch=False)
        self.tree.column("name", width=165, anchor=tk.W)
        self.tree.column("kind", width=82, anchor=tk.CENTER, stretch=False)
        self.tree.column("fmt", width=58, anchor=tk.CENTER, stretch=False)
        self.tree.column("raw", width=80, anchor=tk.E, stretch=False)
        self.tree.column("disk", width=80, anchor=tk.E, stretch=False)

        self.tree.tag_configure("dynamic", foreground=self.DYNAMIC_FG)
        self.tree.tag_configure("super_raw", foreground=self.SUPER_FG)
        self.tree.tag_configure("bootchain", foreground=self.BOOT_FG)
        self.tree.tag_configure("bootloader", foreground=self.WARN)
        self.tree.tag_configure("unchecked", foreground="#5d6678")

        tscroll = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)

        sel_bar = tk.Frame(left_card, bg=self.SURFACE)
        sel_bar.pack(fill=tk.X, pady=(6, 0))

        self._btn(sel_bar, "All", lambda: self._set_all_selected(True), bg=self.SURFACE_ALT, hover="#2e3543", pad=(8, 2), size=8).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(sel_bar, "None", lambda: self._set_all_selected(False), bg=self.SURFACE_ALT, hover="#2e3543", pad=(8, 2), size=8).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(sel_bar, "Safe (skip bootloader)", self._select_safe_only, bg=self.SURFACE_ALT, hover="#2e3543", pad=(8, 2), size=8).pack(side=tk.LEFT)
        self.sel_summary_lbl = tk.Label(sel_bar, text="Selected: 0/0", font=(self.ui_font, 8), fg=self.MUTED, bg=self.SURFACE)
        self.sel_summary_lbl.pack(side=tk.RIGHT)

        # Right Pane: ROM metadata, compression, output path, and Build CTA
        right_card = tk.Frame(main_pane, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER, padx=12, pady=8, width=370)
        right_card.pack(side=tk.RIGHT, fill=tk.Y)
        right_card.pack_propagate(False)

        tk.Label(right_card, text="ROM Info & Options", font=(self.ui_font, 10, "bold"), fg=self.TEXT, bg=self.SURFACE).pack(anchor=tk.W, pady=(0, 6))

        meta_grid = tk.Frame(right_card, bg=self.SURFACE)
        meta_grid.pack(fill=tk.X, pady=(0, 6))
        meta_grid.columnconfigure(0, weight=1)
        meta_grid.columnconfigure(1, weight=1)

        self.device_var = tk.StringVar(value=DEFAULT_DEVICE)
        self.codename_var = tk.StringVar(value=DEFAULT_CODENAME)
        self.version_var = tk.StringVar(value=DEFAULT_VERSION)
        self.maintainer_var = tk.StringVar(value=DEFAULT_MAINTAINER)

        for var in (self.device_var, self.codename_var, self.version_var):
            var.trace_add("write", lambda *_: self._update_output_preview())

        self._grid_entry(meta_grid, 0, 0, "Device Name", self.device_var, pad_right=6)
        self._grid_entry(meta_grid, 0, 1, "Codename", self.codename_var, pad_right=0)
        self._grid_entry(meta_grid, 2, 0, "Version", self.version_var, pad_right=6)
        self._grid_entry(meta_grid, 2, 1, "Maintainer", self.maintainer_var, pad_right=0)

        tk.Label(right_card, text="AVB 2.0 (top-level vbmeta)", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(anchor=tk.W, pady=(2, 1))
        self.vbmeta_var = tk.StringVar(value="skip")
        ttk.Combobox(
            right_card,
            textvariable=self.vbmeta_var,
            values=("skip", "disable", "enable"),
            state="readonly",
            style="NF.TCombobox",
        ).pack(fill=tk.X, pady=(0, 6))

        z_head = tk.Frame(right_card, bg=self.SURFACE)
        z_head.pack(fill=tk.X)
        tk.Label(z_head, text="ZSTD Compression", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(side=tk.LEFT)
        self.zstd_lbl = tk.Label(z_head, text="Level 1 (Fast)", font=(self.ui_font, 8, "bold"), fg=self.ACCENT_HOVER, bg=self.SURFACE)
        self.zstd_lbl.pack(side=tk.RIGHT)

        self.zstd_var = tk.IntVar(value=1)
        ttk.Scale(
            right_card, from_=0, to=22, orient=tk.HORIZONTAL,
            variable=self.zstd_var, command=self._on_zstd_change, style="NF.Horizontal.TScale",
        ).pack(fill=tk.X, pady=(1, 6))

        p_head = tk.Frame(right_card, bg=self.SURFACE)
        p_head.pack(fill=tk.X)
        tk.Label(p_head, text="ZIP Level (raw only)", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(side=tk.LEFT)
        self.zip_lbl = tk.Label(p_head, text="Level 1 (Fast)", font=(self.ui_font, 8, "bold"), fg=self.ACCENT_HOVER, bg=self.SURFACE)
        self.zip_lbl.pack(side=tk.RIGHT)

        self.zip_var = tk.IntVar(value=1)
        ttk.Scale(
            right_card, from_=0, to=9, orient=tk.HORIZONTAL,
            variable=self.zip_var, command=self._on_zip_change, style="NF.Horizontal.TScale",
        ).pack(fill=tk.X, pady=(1, 6))

        self.clean_super_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            right_card,
            text="Clean stale dynamic partitions in super",
            variable=self.clean_super_var,
            bg=self.SURFACE,
            fg=self.TEXT,
            selectcolor=self.INPUT,
            activebackground=self.SURFACE,
            activeforeground=self.TEXT,
            font=(self.ui_font, 8),
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(0, 6))

        out_head = tk.Frame(right_card, bg=self.SURFACE)
        out_head.pack(fill=tk.X)
        tk.Label(out_head, text="Output ZIP", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(side=tk.LEFT)
        self._btn(out_head, "Change Folder...", self._browse_output_dir, bg=self.SURFACE_ALT, hover="#2e3543", pad=(6, 1), size=8).pack(side=tk.RIGHT)

        self.out_name_lbl = tk.Label(
            right_card,
            text="",
            font=(self.mono_font, 8),
            fg=self.TEXT,
            bg=self.INPUT,
            anchor=tk.W,
            padx=6,
            pady=4,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        self.out_name_lbl.pack(fill=tk.X, pady=(2, 2))

        self.out_dir_lbl = tk.Label(right_card, text="", font=(self.ui_font, 7), fg=self.MUTED, bg=self.SURFACE, anchor=tk.W)
        self.out_dir_lbl.pack(fill=tk.X)

        cta_row = tk.Frame(right_card, bg=self.SURFACE)
        cta_row.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        self.build_btn = self._btn(cta_row, "Build Flashable ZIP", self._start_build, bg=self.ACCENT, hover=self.ACCENT_HOVER, pad=(12, 8), size=10, bold=True)
        self.build_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.cancel_btn = self._btn(cta_row, "Cancel", self._cancel_build, bg=self.DANGER, hover=self.DANGER_HOVER, pad=(10, 8), size=9)
        self.cancel_btn.config(state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.RIGHT)

        # Bottom Progress + Compact Log Console
        prog_row = tk.Frame(outer, bg=self.BG)
        prog_row.pack(fill=tk.X, pady=(0, 4))

        self.stage_lbl = tk.Label(prog_row, text="Ready", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.BG)
        self.stage_lbl.pack(side=tk.LEFT)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(outer, style="NF.Horizontal.TProgressbar", mode="determinate", maximum=100.0, variable=self.progress_var)
        self.progress.pack(fill=tk.X, pady=(0, 6))

        log_card = tk.Frame(outer, bg=self.CONSOLE, highlightthickness=1, highlightbackground=self.BORDER)
        log_card.pack(fill=tk.X)

        log_head = tk.Frame(log_card, bg=self.CONSOLE, padx=8, pady=3)
        log_head.pack(fill=tk.X)
        tk.Label(log_head, text="Build Log", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.CONSOLE).pack(side=tk.LEFT)
        self._btn(log_head, "Clear", self._clear_log, bg="#1b1e26", hover="#262b36", pad=(7, 1), size=8).pack(side=tk.RIGHT)
        self._btn(log_head, "Save", self._save_log, bg="#1b1e26", hover="#262b36", pad=(7, 1), size=8).pack(side=tk.RIGHT, padx=(0, 4))

        log_body = tk.Frame(log_card, bg=self.CONSOLE)
        log_body.pack(fill=tk.X)

        self.log_text = tk.Text(
            log_body,
            height=6,
            bg=self.CONSOLE,
            fg="#d2d8e2",
            insertbackground=self.TEXT,
            font=(self.mono_font, 9),
            relief=tk.FLAT,
            padx=8,
            pady=4,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        lscroll = ttk.Scrollbar(log_body, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lscroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lscroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self._browse_folder())
        self.root.bind("<Control-O>", lambda _e: self._browse_folder())
        self.root.bind("<F5>", lambda _e: self._trigger_scan_now())
        self.root.bind("<Control-b>", lambda _e: self._start_build() if str(self.build_btn["state"]) != "disabled" else None)

    def _btn(self, parent, text, cmd, bg, hover, pad=(10, 4), size=9, bold=False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=cmd,
            font=(self.ui_font, size, "bold" if bold else "normal"),
            bg=bg,
            fg=self.TEXT,
            activebackground=hover,
            activeforeground=self.TEXT,
            disabledforeground="#666d7a",
            relief=tk.FLAT,
            padx=pad[0],
            pady=pad[1],
            cursor="hand2",
        )

    def _grid_entry(self, parent, row: int, col: int, label: str, var: tk.StringVar, pad_right: int) -> None:
        tk.Label(parent, text=label, font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).grid(
            row=row, column=col, sticky="w", padx=(0, pad_right)
        )
        tk.Entry(
            parent,
            textvariable=var,
            font=(self.ui_font, 9),
            bg=self.INPUT,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
        ).grid(row=row + 1, column=col, sticky="ew", padx=(0, pad_right), pady=(1, 5), ipady=3)

    def _update_output_preview(self) -> None:
        dev = self.device_var.get().strip() or DEFAULT_DEVICE
        code = self.codename_var.get().strip()
        ver = self.version_var.get().strip() or DEFAULT_VERSION
        fname = format_output_filename(dev, ver, code)
        self.out_name_lbl.config(text=fname)
        short_dir = self.output_dir
        if len(short_dir) > 48:
            short_dir = "..." + short_dir[-45:]
        self.out_dir_lbl.config(text=f"Dir: {short_dir}")

    def _on_zstd_change(self, val: str) -> None:
        lvl = int(float(val))
        self.zstd_var.set(lvl)
        note = "Raw" if lvl == 0 else ("Fast" if lvl <= 3 else ("Ultra" if lvl >= 20 else "Balanced"))
        self.zstd_lbl.config(text=f"Level {lvl} ({note})")

    def _on_zip_change(self, val: str) -> None:
        lvl = int(float(val))
        self.zip_var.set(lvl)
        note = "Store" if lvl == 0 else ("Fast" if lvl == 1 else f"Deflate {lvl}")
        self.zip_lbl.config(text=f"Level {lvl} ({note})")

    def _browse_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select Partition Images Folder")
        if folder:
            self.path_var.set(os.path.abspath(folder))
            self._trigger_scan_now()

    def _browse_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="Select Output Folder", initialdir=self.output_dir if os.path.isdir(self.output_dir) else ROOT_DIR)
        if folder:
            self.output_dir = os.path.abspath(folder)
            self._update_output_preview()

    def _schedule_scan(self, _event=None) -> None:
        if self.scan_after_id is not None:
            self.root.after_cancel(self.scan_after_id)
        self.scan_after_id = self.root.after(300, self._trigger_scan_now)

    def _trigger_scan_now(self) -> None:
        self.scan_after_id = None
        path = self.path_var.get().strip().strip('"').strip("'")
        if not path or not os.path.isdir(path):
            self.partitions = []
            self._refresh_tree()
            self.scan_lbl.config(text="Choose a valid folder", fg=self.MUTED)
            return

        self.scan_seq += 1
        seq = self.scan_seq
        self.scan_lbl.config(text="Scanning...", fg=self.ACCENT_HOVER)

        def worker():
            try:
                parts = scan_partitions(path)
                props = detect_build_props(path)
                self.ui_queue.put(("scan_ok", seq, parts, props))
            except Exception as exc:
                self.ui_queue.put(("scan_err", seq, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        sel_count = 0
        sel_raw = 0
        for idx, p in enumerate(self.partitions):
            mark = "[x]" if p.selected else "[ ]"
            if p.selected:
                sel_count += 1
                sel_raw += p.raw_size
            tags = ()
            if not p.selected:
                tags = ("unchecked",)
            elif p.kind in ("dynamic", "super_raw", "bootchain", "bootloader"):
                tags = (p.kind,)
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(mark, p.name, p.kind, p.fmt, _fmt_size(p.raw_size), _fmt_size(p.file_size)),
                tags=tags,
            )
        self.sel_summary_lbl.config(
            text=f"Selected: {sel_count}/{len(self.partitions)} | Raw: {_fmt_size(sel_raw)}"
        )

    def _toggle_index(self, idx: int) -> None:
        if 0 <= idx < len(self.partitions):
            p = self.partitions[idx]
            self.partitions[idx] = replace(p, selected=not p.selected)
            self._refresh_tree()
            self.tree.selection_set(str(idx))

    def _on_tree_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self._toggle_index(int(row_id))

    def _on_tree_space(self, _event) -> None:
        sel = self.tree.selection()
        if sel:
            self._toggle_index(int(sel[0]))

    def _set_all_selected(self, flag: bool) -> None:
        self.partitions = [replace(p, selected=flag) for p in self.partitions]
        self._refresh_tree()

    def _select_safe_only(self) -> None:
        self.partitions = [replace(p, selected=(p.kind != "bootloader")) for p in self.partitions]
        self._refresh_tree()

    def _current_config(self) -> BuildConfig:
        imgs_dir = self.path_var.get().strip().strip('"').strip("'")
        dev = self.device_var.get().strip() or DEFAULT_DEVICE
        code = self.codename_var.get().strip()
        ver = self.version_var.get().strip() or DEFAULT_VERSION
        maint = self.maintainer_var.get().strip() or DEFAULT_MAINTAINER

        os.makedirs(self.output_dir, exist_ok=True)
        out_zip = os.path.join(self.output_dir, format_output_filename(dev, ver, code))

        return BuildConfig(
            imgs_dir=imgs_dir,
            output_zip=out_zip,
            device=dev,
            codename=code,
            version=ver,
            maintainer=maint,
            vbmeta_mode=self.vbmeta_var.get(),
            zstd_level=self.zstd_var.get(),
            zip_level=self.zip_var.get(),
            clean_super=self.clean_super_var.get(),
            partitions=list(self.partitions),
        )

    def _append_log(self, line: str) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Build Log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text.get("1.0", tk.END))

    def _open_script_preview(self) -> None:
        if not any(p.selected for p in self.partitions):
            messagebox.showinfo("Newbies Flasher", "Select a folder with at least one checked partition first.")
            return

        body = preview_update_binary(self._current_config())
        win = tk.Toplevel(self.root)
        win.title("Recovery Script Preview (update-binary)")
        win.geometry("820x540")
        win.configure(bg=self.BG)

        bar = tk.Frame(win, bg=self.BG, padx=10, pady=8)
        bar.pack(fill=tk.X)
        line_count = len(body.splitlines())
        tk.Label(
            bar,
            text=f"META-INF/com/google/android/update-binary ({line_count} lines)",
            font=(self.ui_font, 9, "bold"),
            fg=self.MUTED,
            bg=self.BG,
        ).pack(side=tk.LEFT)

        copy_btn = self._btn(bar, "Copy Script", lambda: None, bg=self.ACCENT, hover=self.ACCENT_HOVER, pad=(10, 3), size=8, bold=True)

        def _do_copy():
            win.clipboard_clear()
            win.clipboard_append(body)
            copy_btn.config(text="Copied")
            win.after(1500, lambda: copy_btn.winfo_exists() and copy_btn.config(text="Copy Script"))

        copy_btn.config(command=_do_copy)
        copy_btn.pack(side=tk.RIGHT)

        wrap = tk.Frame(win, bg=self.CONSOLE, highlightthickness=1, highlightbackground=self.BORDER)
        wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        txt = tk.Text(
            wrap, bg=self.CONSOLE, fg="#d5dae3", font=(self.mono_font, 9),
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.NONE,
        )
        yscroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=txt.yview)
        xscroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=txt.xview)
        txt.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        txt.insert("1.0", body)
        txt.config(state=tk.DISABLED)

        xscroll.pack(side=tk.BOTTOM, fill=tk.X)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _start_build(self) -> None:
        cfg = self._current_config()
        if not cfg.imgs_dir or not os.path.isdir(cfg.imgs_dir):
            self.stage_lbl.config(text="Select a valid images folder first", fg=self.WARN)
            return
        if not any(p.selected for p in cfg.partitions or []):
            self.stage_lbl.config(text="Check at least one partition to package", fg=self.WARN)
            return

        self.cancel_event.clear()
        self.build_start_ts = time.monotonic()
        self.progress_var.set(0.0)
        self.build_btn.config(state=tk.DISABLED, text="Building...")
        self.cancel_btn.config(state=tk.NORMAL)
        self.stage_lbl.config(text="Starting...", fg=self.ACCENT_HOVER)
        self._append_log(f"--- Build started: {os.path.basename(cfg.output_zip)} ---")

        def worker():
            try:
                res = build_package(
                    cfg,
                    on_log=lambda m: self.ui_queue.put(("log", m)),
                    on_progress=lambda pct, st: self.ui_queue.put(("prog", pct, st)),
                    cancel_event=self.cancel_event,
                )
                self.ui_queue.put(("build_ok", res))
            except Exception as exc:
                self.ui_queue.put(("build_err", str(exc)))

        self.build_thread = threading.Thread(target=worker, daemon=True)
        self.build_thread.start()

    def _cancel_build(self) -> None:
        self.cancel_event.set()
        self.cancel_btn.config(state=tk.DISABLED)
        self.stage_lbl.config(text="Cancelling...", fg=self.WARN)

    def _open_output(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        target = self.last_output if (self.last_output and os.path.isfile(self.last_output)) else self.output_dir
        reveal_in_file_manager(target)

    def _on_close(self) -> None:
        self.cancel_event.set()
        self._cancel_timers()
        self.root.destroy()

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self._cancel_timers()

    def _cancel_timers(self) -> None:
        for attr in ("poll_after_id", "scan_after_id"):
            tid = getattr(self, attr, None)
            if tid is not None:
                try:
                    self.root.after_cancel(tid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def _poll_queue(self) -> None:
        self.poll_after_id = None
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "scan_ok":
                _, seq, parts, props = item
                if seq != self.scan_seq:
                    continue
                self.partitions = parts
                self._refresh_tree()
                if parts:
                    self.scan_lbl.config(text=f"Found {len(parts)} image(s)", fg=self.OK)
                else:
                    self.scan_lbl.config(text="No .img or .img.zst files found", fg=self.WARN)
                if props["device"] and self.device_var.get() in ("", DEFAULT_DEVICE):
                    self.device_var.set(props["device"])
                if props["codename"] and self.codename_var.get() in ("", DEFAULT_CODENAME):
                    self.codename_var.set(props["codename"])
                if props["version"] and self.version_var.get() in ("", DEFAULT_VERSION):
                    self.version_var.set(props["version"])

            elif kind == "scan_err":
                _, seq, msg = item
                if seq == self.scan_seq:
                    self.scan_lbl.config(text=f"Scan error: {msg}", fg=self.WARN)

            elif kind == "log":
                self._append_log(item[1])

            elif kind == "prog":
                _, pct, stage = item
                elapsed = time.monotonic() - self.build_start_ts
                self.progress_var.set(pct)
                self.stage_lbl.config(text=f"{stage} ({pct:.0f}% | {elapsed:.1f}s)", fg=self.ACCENT_HOVER)

            elif kind == "build_ok":
                res: BuildResult = item[1]
                self.last_output = res.output_zip
                self.progress_var.set(100.0)
                self.build_btn.config(state=tk.NORMAL, text="Build Flashable ZIP")
                self.cancel_btn.config(state=tk.DISABLED)
                self.stage_lbl.config(text=f"Complete: {res.size_mb:.1f} MB in {res.elapsed_sec:.1f}s", fg=self.OK)

            elif kind == "build_err":
                err_msg = item[1]
                self.build_btn.config(state=tk.NORMAL, text="Build Flashable ZIP")
                self.cancel_btn.config(state=tk.DISABLED)
                self.stage_lbl.config(text=f"Failed: {err_msg}", fg=self.WARN)
                self._append_log(f"ERROR: {err_msg}")

        self.poll_after_id = self.root.after(40, self._poll_queue)


def launch_gui() -> None:
    _enable_windows_dpi()
    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"Cannot start GUI ({exc}). Run main.py for CLI mode.")
        if sys.platform.startswith("linux"):
            print("On Debian/Ubuntu, install Tkinter with: sudo apt install python3-tk")
        sys.exit(1)

    NewbiesFlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
