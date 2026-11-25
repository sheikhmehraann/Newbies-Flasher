#!/usr/bin/env python3
import ctypes
import os
import queue
import sys
import threading
import time
from dataclasses import replace
from typing import Callable, List, Optional

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

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
from flasher.partitions import PartitionImage, scan_folder
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


def _rounded_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> List[float]:
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]


class PillButton(tk.Canvas):
    """Cross-platform rounded clickable pill button with exact DPI-safe sizing."""

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Optional[Callable[[], None]],
        font_spec: tuple,
        bg: str,
        hover_bg: str,
        fg: str,
        parent_bg: str,
        border: str = "",
        disabled_bg: str = "#1c2029",
        disabled_fg: str = "#636b7a",
        pad_x: int = 12,
        pad_y: int = 5,
        min_w: int = 0,
        min_h: int = 0,
        pill: bool = False,
        clickable: bool = True,
    ):
        self._font = tkfont.Font(family=font_spec[0], size=font_spec[1], weight=font_spec[2] if len(font_spec) > 2 else "normal")
        self._text = text
        self._command = command
        self._bg = bg
        self._hover_bg = hover_bg
        self._fg = fg
        self._border = border
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._pad_x = pad_x
        self._pad_y = pad_y
        self._min_w = min_w
        self._min_h = min_h
        self._pill = pill
        self._clickable = clickable
        self._enabled = True
        self._hovered = False
        self._pressed = False
        self._shape_id: Optional[int] = None
        self._text_id: Optional[int] = None

        w, h = self._measure()
        super().__init__(
            parent,
            width=w,
            height=h,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2" if clickable else "",
            takefocus=1 if clickable else 0,
        )

        self.bind("<Configure>", lambda e: self._redraw(e.width, e.height))
        if clickable:
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<ButtonPress-1>", self._on_press)
            self.bind("<ButtonRelease-1>", self._on_release)
            self.bind("<Return>", lambda _e: self._invoke())
            self.bind("<space>", lambda _e: self._invoke())
        self._redraw(w, h)

    def _measure(self) -> tuple:
        tw = self._font.measure(self._text)
        th = self._font.metrics("linespace")
        w = max(self._min_w, tw + 2 * self._pad_x)
        h = max(self._min_h, th + 2 * self._pad_y)
        return int(w), int(h)

    def _current_colors(self) -> tuple:
        if not self._enabled:
            return self._disabled_bg, self._disabled_fg
        if self._pressed or self._hovered:
            return self._hover_bg, self._fg
        return self._bg, self._fg

    def _redraw(self, ev_w: int = 0, ev_h: int = 0) -> None:
        if not self.winfo_exists():
            return
        w = ev_w if ev_w > 2 else max(self.winfo_width(), int(self.cget("width")))
        h = ev_h if ev_h > 2 else max(self.winfo_height(), int(self.cget("height")))
        fill, fg = self._current_colors()
        radius = (h / 2.0) if self._pill else min(h / 2.0, 7.0)
        pts = _rounded_rect_points(1, 1, w - 1, h - 1, radius)
        outline = self._border if self._border else fill

        if self._shape_id is None:
            self._shape_id = self.create_polygon(pts, smooth=True, splinesteps=12, fill=fill, outline=outline, width=1)
            self._text_id = self.create_text(w / 2.0, h / 2.0, text=self._text, fill=fg, font=self._font)
        else:
            self.coords(self._shape_id, *pts)
            self.itemconfig(self._shape_id, fill=fill, outline=outline)
            self.coords(self._text_id, w / 2.0, h / 2.0)
            self.itemconfig(self._text_id, text=self._text, fill=fg)

    def _refresh_colors(self) -> None:
        if self._shape_id is None or not self.winfo_exists():
            return
        fill, fg = self._current_colors()
        outline = self._border if self._border else fill
        self.itemconfig(self._shape_id, fill=fill, outline=outline)
        self.itemconfig(self._text_id, fill=fg)

    def _on_enter(self, _event) -> None:
        if self._enabled:
            self._hovered = True
            self._refresh_colors()

    def _on_leave(self, _event) -> None:
        self._hovered = False
        self._pressed = False
        self._refresh_colors()

    def _on_press(self, _event) -> None:
        if self._enabled:
            self._pressed = True
            self._refresh_colors()

    def _on_release(self, event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._refresh_colors()
        if was_pressed and self._enabled:
            if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
                self._invoke()

    def _invoke(self) -> None:
        if self._enabled and self._command is not None:
            self._command()

    def set_text(self, text: str) -> None:
        if self._text == text:
            return
        self._text = text
        w, h = self._measure()
        super().configure(width=w, height=h)
        self._redraw()

    def set_command(self, cmd: Optional[Callable[[], None]]) -> None:
        self._command = cmd

    def set_colors(self, bg: str, hover_bg: str, fg: str, border: str = "") -> None:
        self._bg = bg
        self._hover_bg = hover_bg
        self._fg = fg
        self._border = border
        self._refresh_colors()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._hovered = False
            self._pressed = False
        super().configure(cursor="hand2" if (self._clickable and self._enabled) else "")
        self._refresh_colors()

    @property
    def text(self) -> str:
        return self._text

    @property
    def enabled(self) -> bool:
        return self._enabled


class FlatSlider(tk.Canvas):
    """Dark-theme integer slider with click-to-jump, smooth drag, and mouse-wheel stepping."""

    def __init__(
        self,
        parent: tk.Widget,
        variable: tk.IntVar,
        from_: int,
        to: int,
        bg: str,
        track_color: str,
        fill_color: str,
        thumb_color: str,
        height: int = 24,
        on_change: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.lo = from_
        self.hi = to
        self.track_color = track_color
        self.fill_color = fill_color
        self.thumb_color = thumb_color
        self.on_change = on_change
        self._hover = False
        self._pad = 9
        self._track_id: Optional[int] = None
        self._fill_id: Optional[int] = None
        self._thumb_id: Optional[int] = None

        self.bind("<Configure>", lambda e: self._redraw(ev_w=e.width, ev_h=e.height))
        self.bind("<Button-1>", self._on_pointer)
        self.bind("<B1-Motion>", self._on_pointer)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda _e: self.step(1))
        self.bind("<Button-5>", lambda _e: self.step(-1))
        self._trace_id = self.var.trace_add("write", self._on_var_write)
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event) -> None:
        if event.widget is self and self._trace_id:
            try:
                self.var.trace_remove("write", self._trace_id)
            except tk.TclError:
                pass
            self._trace_id = None

    def _set_hover(self, state: bool) -> None:
        self._hover = state
        self._redraw()

    def set_fill_color(self, color: str) -> None:
        if self.fill_color != color:
            self.fill_color = color
            self._redraw()

    def step(self, delta: int) -> None:
        cur = self._safe_get()
        nxt = max(self.lo, min(self.hi, cur + delta))
        if nxt != cur:
            self.var.set(nxt)

    def _safe_get(self) -> int:
        try:
            v = int(self.var.get())
        except (tk.TclError, ValueError):
            v = self.lo
        return max(self.lo, min(self.hi, v))

    def _x_to_val(self, x: int) -> int:
        w = max(1, self.winfo_width() - (2 * self._pad))
        ratio = max(0.0, min(1.0, (x - self._pad) / w))
        return int(round(self.lo + ratio * (self.hi - self.lo)))

    def _on_pointer(self, event) -> None:
        val = self._x_to_val(event.x)
        if val != self._safe_get():
            self.var.set(val)

    def _on_wheel(self, event) -> None:
        self.step(1 if event.delta > 0 else -1)

    def _on_var_write(self, *_args) -> None:
        if not self.winfo_exists():
            return
        val = self._safe_get()
        self._redraw(val=val)
        if self.on_change:
            self.on_change(val)

    def _redraw(self, val: Optional[int] = None, ev_w: int = 0, ev_h: int = 0) -> None:
        if not self.winfo_exists():
            return
        w = ev_w if ev_w > 2 else self.winfo_width()
        h = ev_h if ev_h > 2 else self.winfo_height()
        if w < 24:
            return
        cur = self._safe_get() if val is None else val
        span = max(1, self.hi - self.lo)
        ratio = (cur - self.lo) / span
        x0 = self._pad
        x1 = w - self._pad
        tx = x0 + ratio * (x1 - x0)
        cy = h // 2
        r = 7 if self._hover else 6
        ring = self.fill_color if self._hover else "#101216"

        if self._track_id is None:
            self._track_id = self.create_line(x0, cy, x1, cy, fill=self.track_color, width=5, capstyle=tk.ROUND)
            self._fill_id = self.create_line(x0, cy, max(x0, tx), cy, fill=self.fill_color, width=5, capstyle=tk.ROUND)
            self._thumb_id = self.create_oval(tx - r, cy - r, tx + r, cy + r, fill=self.thumb_color, outline=ring, width=2)
        else:
            self.coords(self._track_id, x0, cy, x1, cy)
            self.coords(self._fill_id, x0, cy, max(x0, tx), cy)
            self.itemconfig(self._fill_id, fill=self.fill_color, state=tk.NORMAL if tx > x0 else tk.HIDDEN)
            self.coords(self._thumb_id, tx - r, cy - r, tx + r, cy + r)
            self.itemconfig(self._thumb_id, outline=ring)


class NewbiesFlasherApp:
    BG = "#111318"
    SURFACE = "#181c23"
    SURFACE_ALT = "#222733"
    SURFACE_HOVER = "#2e3545"
    BORDER = "#2a303d"
    INPUT = "#0e1015"
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
    CONSOLE = "#0c0e12"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Newbies Flasher v{__version__}")

        # Scale initial window cleanly with both display resolution and OS DPI scaling.
        dpi = max(1.0, min(2.25, self.root.winfo_fpixels("1i") / 96.0))
        self.dpi = dpi
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        win_w = min(int(1100 * dpi), max(640, sw - 60))
        win_h = min(int(650 * dpi), max(480, sh - 84))
        pos_x = max(0, (sw - win_w) // 2)
        pos_y = max(0, (sh - win_h) // 2 - 14)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(min(540, max(440, sw - 80)), min(440, max(380, sh - 90)))
        self.root.configure(bg=self.BG)
        _apply_windows_dark_chrome(self.root)

        self.ui_font = "Segoe UI" if sys.platform == "win32" else ("SF Pro Text" if sys.platform == "darwin" else "TkDefaultFont")
        self.mono_font = "Consolas" if sys.platform == "win32" else ("Menlo" if sys.platform == "darwin" else "Monospace")

        self.partitions: List[PartitionImage] = []
        self.sort_col: str = ""
        self.sort_desc: bool = False
        self.output_dir: str = os.path.join(ROOT_DIR, "output")
        self.last_output: Optional[str] = None
        self.cancel_event = threading.Event()
        self.build_thread: Optional[threading.Thread] = None
        self.is_building: bool = False
        self.build_start_ts: float = 0.0
        self.scan_after_id: Optional[str] = None
        self.poll_after_id: Optional[str] = None
        self.resize_after_id: Optional[str] = None
        self._last_layout_key: tuple = ()
        self._layout_mode: str = ""
        self._log_lines: int = 4
        self._chrome_applied: bool = False
        self.scan_seq = 0
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()

        self._setup_styles()
        self._build_layout()
        self._bind_shortcuts()
        self._update_zstd_ui(self.zstd_var.get())
        self._update_zip_ui(self.zip_var.get())
        self._update_output_preview()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_destroy)
        self.root.bind("<Configure>", self._on_root_configure)
        self.root.bind("<Map>", self._on_root_map)
        self.root.after_idle(self._apply_responsive_layout)
        self._poll_queue()

    def _s(self, px: int) -> int:
        return max(1, int(round(px * self.dpi)))

    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "NF.Treeview",
            background=self.INPUT,
            foreground=self.TEXT,
            fieldbackground=self.INPUT,
            bordercolor=self.BORDER,
            rowheight=max(22, self._s(24)),
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
            "NF.Treeview.Heading",
            background=[("active", self.SURFACE_HOVER)],
            foreground=[("active", self.TEXT)],
        )
        style.map(
            "NF.Treeview",
            background=[("selected", "#22344f")],
            foreground=[("selected", self.TEXT)],
        )
        style.configure(
            "NF.Horizontal.TProgressbar",
            troughcolor=self.INPUT,
            background=self.ACCENT,
            bordercolor=self.BORDER,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=max(6, self._s(7)),
        )

    def _build_layout(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        # Row 0: Top Bar (action pills packed RIGHT first so buttons never squish on any width)
        self.top_bar = tk.Frame(outer, bg=self.BG)
        self.top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.open_out_pill = self._pill(self.top_bar, "Open Output", self._open_output, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.BG, pad=(10, 4), size=8, bold=True, pill=True, border=self.BORDER)
        self.open_out_pill.pack(side=tk.RIGHT)
        self.preview_pill = self._pill(self.top_bar, "Preview Script", self._open_script_preview, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.BG, pad=(10, 4), size=8, bold=True, pill=True, border=self.BORDER)
        self.preview_pill.pack(side=tk.RIGHT, padx=(0, 6))

        self.title_lbl = tk.Label(
            self.top_bar, text="Newbies Flasher", font=(self.ui_font, 14, "bold"),
            fg=self.TEXT, bg=self.BG,
        )
        self.title_lbl.pack(side=tk.LEFT)

        self.ver_lbl = tk.Label(
            self.top_bar, text=f"v{__version__}", font=(self.ui_font, 8),
            fg=self.MUTED, bg=self.BG,
        )
        self.ver_lbl.pack(side=tk.LEFT, padx=(6, 8), pady=(3, 0))

        zstd_status = "zstd: ready" if get_host_zstd() else ("zstd: python" if has_python_zstandard() else "zstd: missing")
        self.status_pill = self._pill(
            self.top_bar,
            f"{detect_host_os()} | {zstd_status}",
            None,
            bg=self.SURFACE,
            hover=self.SURFACE,
            fg=self.OK if "missing" not in zstd_status else self.WARN,
            parent_bg=self.BG,
            pad=(9, 3),
            size=8,
            bold=True,
            pill=True,
            border=self.BORDER,
            clickable=False,
        )
        self.status_pill.pack(side=tk.LEFT, pady=(1, 0))

        # Row 1: Responsive Workspace (2-column when wide/maximized, 1-column stacked when narrow/split-screen)
        self.main_pane = tk.Frame(outer, bg=self.BG)
        self.main_pane.grid(row=1, column=0, sticky="nsew", pady=(0, 6))

        # Left Pane: Partition Images
        self.left_card = tk.Frame(self.main_pane, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER, padx=10, pady=7)
        self.left_card.columnconfigure(0, weight=1)
        self.left_card.rowconfigure(2, weight=1)

        left_head = tk.Frame(self.left_card, bg=self.SURFACE)
        left_head.grid(row=0, column=0, sticky="ew")
        tk.Label(left_head, text="Partition Images", font=(self.ui_font, 10, "bold"), fg=self.TEXT, bg=self.SURFACE).pack(side=tk.LEFT)
        self.scan_lbl = tk.Label(left_head, text="Select a folder with .img / .img.zst files", font=(self.ui_font, 8), fg=self.MUTED, bg=self.SURFACE)
        self.scan_lbl.pack(side=tk.RIGHT)

        path_row = tk.Frame(self.left_card, bg=self.SURFACE)
        path_row.grid(row=1, column=0, sticky="ew", pady=(5, 5))
        self._pill(path_row, "Rescan", self._trigger_scan_now, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(9, 4), size=8, bold=True, border=self.BORDER).pack(side=tk.RIGHT)
        self._pill(path_row, "Browse...", self._browse_folder, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(10, 4), size=8, bold=True, border=self.BORDER).pack(side=tk.RIGHT, padx=(0, 5))

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
        self.path_entry.bind("<Return>", lambda _e: self._trigger_scan_now())

        tree_wrap = tk.Frame(self.left_card, bg=self.SURFACE)
        tree_wrap.grid(row=2, column=0, sticky="nsew")

        cols = ("use", "name", "kind", "fmt", "raw", "disk")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=6, style="NF.Treeview", selectmode="browse")
        self.tree.heading("use", text="Use")
        self.tree.heading("name", text="Partition", command=lambda: self._sort_partitions("name"))
        self.tree.heading("kind", text="Type", command=lambda: self._sort_partitions("kind"))
        self.tree.heading("fmt", text="Fmt", command=lambda: self._sort_partitions("fmt"))
        self.tree.heading("raw", text="Raw", command=lambda: self._sort_partitions("raw"))
        self.tree.heading("disk", text="File", command=lambda: self._sort_partitions("disk"))

        self.tree.column("use", width=self._s(40), minwidth=34, anchor=tk.CENTER, stretch=False)
        self.tree.column("name", width=self._s(140), minwidth=80, anchor=tk.W, stretch=True)
        self.tree.column("kind", width=self._s(74), minwidth=58, anchor=tk.CENTER, stretch=False)
        self.tree.column("fmt", width=self._s(48), minwidth=40, anchor=tk.CENTER, stretch=False)
        self.tree.column("raw", width=self._s(70), minwidth=56, anchor=tk.E, stretch=False)
        self.tree.column("disk", width=self._s(70), minwidth=56, anchor=tk.E, stretch=False)

        self.tree.tag_configure("even", background=self.INPUT)
        self.tree.tag_configure("odd", background="#12161e")
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

        self.sel_bar = tk.Frame(self.left_card, bg=self.SURFACE)
        self.sel_bar.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        self.all_pill = self._pill(self.sel_bar, "All", lambda: self._set_all_selected(True), bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(10, 3), size=8, bold=True, pill=True, border=self.BORDER)
        self.all_pill.pack(side=tk.LEFT, padx=(0, 4))
        self.none_pill = self._pill(self.sel_bar, "None", lambda: self._set_all_selected(False), bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(10, 3), size=8, bold=True, pill=True, border=self.BORDER)
        self.none_pill.pack(side=tk.LEFT, padx=(0, 4))
        self.safe_pill = self._pill(self.sel_bar, "Skip Bootloader", self._select_safe_only, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(10, 3), size=8, bold=True, pill=True, border=self.BORDER)
        self.safe_pill.pack(side=tk.LEFT)
        self.sel_summary_lbl = tk.Label(self.sel_bar, text="Selected: 0/0", font=(self.ui_font, 8), fg=self.MUTED, bg=self.SURFACE)
        self.sel_summary_lbl.pack(side=tk.RIGHT)

        # Right Pane: Auto-scrolling Options Viewport + Pinned Build CTA
        self.right_card = tk.Frame(self.main_pane, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER, padx=10, pady=7)
        self.right_card.columnconfigure(0, weight=1)
        self.right_card.rowconfigure(1, weight=1)

        tk.Label(self.right_card, text="ROM Info & Options", font=(self.ui_font, 10, "bold"), fg=self.TEXT, bg=self.SURFACE).grid(row=0, column=0, sticky="w", pady=(0, 3))

        scroll_wrap = tk.Frame(self.right_card, bg=self.SURFACE)
        scroll_wrap.grid(row=1, column=0, sticky="nsew")
        scroll_wrap.columnconfigure(0, weight=1)
        scroll_wrap.rowconfigure(0, weight=1)

        self.opt_canvas = tk.Canvas(scroll_wrap, bg=self.SURFACE, width=320, highlightthickness=0, bd=0)
        self.opt_scroll = ttk.Scrollbar(scroll_wrap, orient=tk.VERTICAL, command=self.opt_canvas.yview)
        self.opt_canvas.configure(yscrollcommand=self.opt_scroll.set)
        self.opt_canvas.grid(row=0, column=0, sticky="nsew")

        self.opt_body = tk.Frame(self.opt_canvas, bg=self.SURFACE)
        self._opt_win_id = self.opt_canvas.create_window((0, 0), window=self.opt_body, anchor="nw")

        def _on_opt_wheel(event) -> None:
            if not self.opt_scroll.winfo_manager():
                return
            if isinstance(event.widget, FlatSlider):
                return
            delta = -1 if getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4 else 1
            self.opt_canvas.yview_scroll(delta, "units")

        def _bind_opt_wheel(_event=None) -> None:
            self.root.bind_all("<MouseWheel>", _on_opt_wheel)
            self.root.bind_all("<Button-4>", _on_opt_wheel)
            self.root.bind_all("<Button-5>", _on_opt_wheel)

        def _unbind_opt_wheel(_event=None) -> None:
            self.root.unbind_all("<MouseWheel>")
            self.root.unbind_all("<Button-4>")
            self.root.unbind_all("<Button-5>")

        self.opt_body.bind("<Configure>", lambda _e: self._sync_opt_scroll())
        self.opt_canvas.bind("<Configure>", lambda _e: self._sync_opt_scroll())
        scroll_wrap.bind("<Enter>", _bind_opt_wheel)
        scroll_wrap.bind("<Leave>", _unbind_opt_wheel)

        meta_grid = tk.Frame(self.opt_body, bg=self.SURFACE)
        meta_grid.pack(fill=tk.X, pady=(0, 3))
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

        tk.Label(self.opt_body, text="AVB 2.0 (vbmeta)", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(anchor=tk.W, pady=(1, 2))
        self.vbmeta_var = tk.StringVar(value="skip")
        vb_row = tk.Frame(self.opt_body, bg=self.SURFACE)
        vb_row.pack(fill=tk.X, pady=(0, 1))
        self.vbmeta_pills = {}
        for col_i, (mode, label) in enumerate((("skip", "Skip"), ("disable", "Disable"), ("enable", "Enable"))):
            vb_row.columnconfigure(col_i, weight=1)
            pbtn = self._pill(
                vb_row,
                label,
                lambda m=mode: self.vbmeta_var.set(m),
                bg=self.INPUT,
                hover=self.SURFACE_HOVER,
                fg=self.MUTED,
                parent_bg=self.SURFACE,
                pad=(8, 3),
                size=8,
                bold=True,
                pill=True,
                border=self.BORDER,
            )
            pbtn.grid(row=0, column=col_i, sticky="ew", padx=(0 if col_i == 0 else 4, 0))
            self.vbmeta_pills[mode] = pbtn

        self.vbmeta_hint_lbl = tk.Label(self.opt_body, text="", font=(self.ui_font, 7), fg=self.MUTED, bg=self.SURFACE, anchor=tk.W)
        self.vbmeta_hint_lbl.pack(fill=tk.X, pady=(1, 3))
        self.vbmeta_var.trace_add("write", lambda *_: self._update_vbmeta_ui())
        self._update_vbmeta_ui()

        tk.Frame(self.opt_body, height=1, bg=self.BORDER).pack(fill=tk.X, pady=(1, 4))

        # ZSTD Compression Slider (0..22)
        z_head = tk.Frame(self.opt_body, bg=self.SURFACE)
        z_head.pack(fill=tk.X)
        tk.Label(z_head, text="ZSTD Compression", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(side=tk.LEFT)
        self.zstd_lbl = tk.Label(z_head, text="", font=(self.ui_font, 8, "bold"), fg=self.ACCENT_HOVER, bg=self.SURFACE)
        self.zstd_lbl.pack(side=tk.RIGHT)

        self.zstd_var = tk.IntVar(value=1)
        self.zstd_slider = self._make_slider_row(self.opt_body, self.zstd_var, 0, 22, self._update_zstd_ui)

        # ZIP Compression Slider (0..9)
        p_head = tk.Frame(self.opt_body, bg=self.SURFACE)
        p_head.pack(fill=tk.X, pady=(1, 0))
        self.zip_title_lbl = tk.Label(p_head, text="ZIP Level", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE)
        self.zip_title_lbl.pack(side=tk.LEFT)
        self.zip_lbl = tk.Label(p_head, text="", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE)
        self.zip_lbl.pack(side=tk.RIGHT)

        self.zip_var = tk.IntVar(value=1)
        self.zip_slider = self._make_slider_row(self.opt_body, self.zip_var, 0, 9, self._update_zip_ui)

        # Output Folder Picker + Live Output ZIP Name
        out_head = tk.Frame(self.opt_body, bg=self.SURFACE)
        out_head.pack(fill=tk.X, pady=(4, 0))
        tk.Label(out_head, text="Output Folder", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.SURFACE).pack(side=tk.LEFT)
        self.out_name_lbl = tk.Label(out_head, text="", font=(self.mono_font, 8, "bold"), fg=self.ACCENT_HOVER, bg=self.SURFACE)
        self.out_name_lbl.pack(side=tk.RIGHT)

        out_row = tk.Frame(self.opt_body, bg=self.SURFACE)
        out_row.pack(fill=tk.X, pady=(2, 1))
        self._pill(out_row, "Browse...", self._browse_output_dir, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(9, 3), size=8, bold=True, border=self.BORDER).pack(side=tk.RIGHT)

        self.out_dir_var = tk.StringVar(value=self.output_dir)
        self.out_dir_entry = tk.Entry(
            out_row,
            textvariable=self.out_dir_var,
            font=(self.ui_font, 8),
            bg=self.INPUT,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
        )
        self.out_dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3, padx=(0, 5))
        self.out_dir_entry.bind("<FocusOut>", lambda _e: self._sync_output_dir_from_entry())
        self.out_dir_entry.bind("<Return>", lambda _e: self._sync_output_dir_from_entry())

        # Pinned CTA Footer (always full height, never squished)
        cta_row = tk.Frame(self.right_card, bg=self.SURFACE)
        cta_row.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        cta_row.columnconfigure(0, weight=1)

        self.build_btn = self._pill(cta_row, "Build Flashable ZIP", self._start_build, bg=self.ACCENT, hover=self.ACCENT_HOVER, parent_bg=self.SURFACE, pad=(12, 7), size=9, bold=True)
        self.build_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.cancel_btn = self._pill(cta_row, "Cancel", self._cancel_build, bg=self.DANGER, hover=self.DANGER_HOVER, parent_bg=self.SURFACE, pad=(11, 7), size=9, bold=True)
        self.cancel_btn.set_enabled(False)
        self.cancel_btn.grid(row=0, column=1, sticky="e")

        self._right_min_w = max(290, self.build_btn.winfo_reqwidth() + self.cancel_btn.winfo_reqwidth() + 34)

        # Row 2: Progress Row + Bar
        prog_block = tk.Frame(outer, bg=self.BG)
        prog_block.grid(row=2, column=0, sticky="ew", pady=(0, 5))

        prog_row = tk.Frame(prog_block, bg=self.BG)
        prog_row.pack(fill=tk.X, pady=(0, 2))
        self.stage_lbl = tk.Label(prog_row, text="Ready", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.BG)
        self.stage_lbl.pack(side=tk.LEFT)
        self.stat_lbl = tk.Label(prog_row, text="", font=(self.mono_font, 8), fg=self.MUTED, bg=self.BG)
        self.stat_lbl.pack(side=tk.RIGHT)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(prog_block, style="NF.Horizontal.TProgressbar", mode="determinate", maximum=100.0, variable=self.progress_var)
        self.progress.pack(fill=tk.X)

        # Row 3: Adaptive Color-Tagged Log Console
        log_card = tk.Frame(outer, bg=self.CONSOLE, highlightthickness=1, highlightbackground=self.BORDER)
        log_card.grid(row=3, column=0, sticky="ew")

        log_head = tk.Frame(log_card, bg=self.CONSOLE, padx=8, pady=2)
        log_head.pack(fill=tk.X)
        self._pill(log_head, "Clear", self._clear_log, bg="#1b1f29", hover="#272d3c", parent_bg=self.CONSOLE, pad=(8, 2), size=8, pill=True, border=self.BORDER).pack(side=tk.RIGHT)
        self._pill(log_head, "Save", self._save_log, bg="#1b1f29", hover="#272d3c", parent_bg=self.CONSOLE, pad=(8, 2), size=8, pill=True, border=self.BORDER).pack(side=tk.RIGHT, padx=(0, 4))
        tk.Label(log_head, text="Build Log", font=(self.ui_font, 8, "bold"), fg=self.MUTED, bg=self.CONSOLE).pack(side=tk.LEFT)

        log_body = tk.Frame(log_card, bg=self.CONSOLE)
        log_body.pack(fill=tk.X)

        self.log_text = tk.Text(
            log_body,
            height=self._log_lines,
            bg=self.CONSOLE,
            fg="#cfd6e3",
            insertbackground=self.TEXT,
            font=(self.mono_font, 9),
            relief=tk.FLAT,
            padx=8,
            pady=3,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        self.log_text.tag_configure("ok", foreground=self.OK)
        self.log_text.tag_configure("err", foreground=self.WARN)
        self.log_text.tag_configure("hdr", foreground=self.DYNAMIC_FG)

        lscroll = ttk.Scrollbar(log_body, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lscroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lscroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Place initial two-pane layout immediately before first map.
        self._place_workspace("wide", 1100)

    def _place_workspace(self, mode: str, win_w: int) -> None:
        if mode == "wide":
            # Cap right panel width so maximizing on 1080p/1440p/4K keeps form controls balanced.
            ideal_r = max(self._right_min_w, self._s(355))
            cap_r = min(max(self._right_min_w, int(win_w * 0.42)), max(self._right_min_w, self._s(415)))
            right_w = min(ideal_r, cap_r)
            if self._layout_mode != "wide":
                self.left_card.grid(row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 8), pady=0)
                self.right_card.grid(row=0, column=1, columnspan=1, sticky="nsew", padx=0, pady=0)
                self.main_pane.rowconfigure(0, weight=1)
                self.main_pane.rowconfigure(1, weight=0)
                self._layout_mode = "wide"
            self.main_pane.columnconfigure(0, weight=1, minsize=260)
            self.main_pane.columnconfigure(1, weight=0, minsize=right_w)
            self.opt_canvas.configure(width=max(240, right_w - 22))
        else:
            if self._layout_mode != "stacked":
                self.left_card.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 6))
                self.right_card.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=0)
                self.main_pane.rowconfigure(0, weight=1)
                self.main_pane.rowconfigure(1, weight=1)
                self.main_pane.columnconfigure(0, weight=1, minsize=200)
                self.main_pane.columnconfigure(1, weight=0, minsize=0)
                self._layout_mode = "stacked"
            self.opt_canvas.configure(width=max(220, win_w - 58))

    def _sync_opt_scroll(self) -> None:
        if not self.opt_canvas.winfo_exists():
            return
        cw = max(220, self.opt_canvas.winfo_width())
        self.opt_canvas.itemconfigure(self._opt_win_id, width=cw)
        req_h = self.opt_body.winfo_reqheight()
        view_h = self.opt_canvas.winfo_height()
        self.opt_canvas.configure(scrollregion=(0, 0, cw, max(req_h, view_h)))
        if req_h > view_h + 2 and view_h > 20:
            if not self.opt_scroll.winfo_manager():
                self.opt_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        else:
            if self.opt_scroll.winfo_manager():
                self.opt_scroll.grid_remove()
            self.opt_canvas.yview_moveto(0.0)

    def _on_root_map(self, _event=None) -> None:
        if not self._chrome_applied:
            self._chrome_applied = True
            _apply_windows_dark_chrome(self.root)
        self._schedule_responsive_layout()

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        if event.width < 100 or event.height < 100:
            return
        key = (event.width, event.height)
        if key == self._last_layout_key:
            return
        self._last_layout_key = key
        self._schedule_responsive_layout()

    def _schedule_responsive_layout(self) -> None:
        if self.resize_after_id is not None:
            try:
                self.root.after_cancel(self.resize_after_id)
            except tk.TclError:
                pass
        self.resize_after_id = self.root.after(16, self._apply_responsive_layout)

    def _apply_responsive_layout(self) -> None:
        self.resize_after_id = None
        if not self.root.winfo_exists():
            return
        try:
            if self.root.state() == "iconic":
                return
        except tk.TclError:
            return

        win_w = self.root.winfo_width()
        win_h = self.root.winfo_height()
        if win_w < 100 or win_h < 100:
            return

        # Switch between side-by-side and stacked cleanly for half-screen / narrow windows.
        wide_thresh = max(self._right_min_w + 330, int(740 * min(self.dpi, 1.35)))
        self._place_workspace("wide" if win_w >= wide_thresh else "stacked", win_w)

        # Scale log height gracefully from tiny windows up to maximized 1080p/1440p screens.
        target_log = 3 if win_h < 540 else (7 if win_h >= 820 else 4)
        if target_log != self._log_lines:
            self._log_lines = target_log
            self.log_text.configure(height=target_log)

        # Keep top bar action pills 100% unsquished at any width/DPI.
        top_w = max(win_w - 24, self.top_bar.winfo_width())
        actions_w = self.preview_pill.winfo_reqwidth() + self.open_out_pill.winfo_reqwidth() + 20
        title_w = self.title_lbl.winfo_reqwidth()
        ver_w = self.ver_lbl.winfo_reqwidth() + 14
        status_w = self.status_pill.winfo_reqwidth() + 18

        need_status = title_w + ver_w + status_w + actions_w + 24
        if top_w < need_status:
            if self.status_pill.winfo_manager():
                self.status_pill.pack_forget()
        else:
            if not self.status_pill.winfo_manager():
                self.status_pill.pack(side=tk.LEFT, pady=(1, 0))

        need_ver = title_w + ver_w + actions_w + 20
        if top_w < need_ver:
            if self.ver_lbl.winfo_manager():
                self.ver_lbl.pack_forget()
        else:
            if not self.ver_lbl.winfo_manager():
                self.ver_lbl.pack(side=tk.LEFT, padx=(6, 8), pady=(3, 0))
                if self.status_pill.winfo_manager():
                    self.status_pill.pack_forget()
                    self.status_pill.pack(side=tk.LEFT, pady=(1, 0))

        # Keep selection pills unsquished inside left pane.
        sel_w = self.sel_bar.winfo_width()
        if sel_w > 60:
            pills_w = self.all_pill.winfo_reqwidth() + self.none_pill.winfo_reqwidth() + self.safe_pill.winfo_reqwidth() + 16
            if sel_w < pills_w + self.sel_summary_lbl.winfo_reqwidth() + 12:
                if self.sel_summary_lbl.winfo_manager():
                    self.sel_summary_lbl.pack_forget()
            else:
                if not self.sel_summary_lbl.winfo_manager():
                    self.sel_summary_lbl.pack(side=tk.RIGHT)

        self._sync_opt_scroll()

    def _pill(
        self,
        parent: tk.Widget,
        text: str,
        cmd: Optional[Callable[[], None]],
        bg: str,
        hover: str,
        parent_bg: str,
        fg: Optional[str] = None,
        pad: tuple = (10, 4),
        size: int = 8,
        bold: bool = False,
        min_w: int = 0,
        min_h: int = 0,
        pill: bool = False,
        border: str = "",
        clickable: bool = True,
    ) -> PillButton:
        return PillButton(
            parent,
            text=text,
            command=cmd,
            font_spec=(self.ui_font, size, "bold" if bold else "normal"),
            bg=bg,
            hover_bg=hover,
            fg=fg or self.TEXT,
            parent_bg=parent_bg,
            border=border,
            pad_x=pad[0],
            pad_y=pad[1],
            min_w=min_w,
            min_h=min_h,
            pill=pill,
            clickable=clickable,
        )

    def _make_slider_row(
        self,
        parent: tk.Widget,
        var: tk.IntVar,
        lo: int,
        hi: int,
        on_change: Callable[[int], None],
    ) -> FlatSlider:
        row = tk.Frame(parent, bg=self.SURFACE)
        row.pack(fill=tk.X, pady=(1, 3))

        step_size = max(22, self._s(22))
        minus_btn = self._pill(row, "-", lambda: slider.step(-1), bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(6, 1), size=9, bold=True, min_w=step_size, min_h=step_size, pill=True, border=self.BORDER)
        plus_btn = self._pill(row, "+", lambda: slider.step(1), bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.SURFACE, pad=(6, 1), size=9, bold=True, min_w=step_size, min_h=step_size, pill=True, border=self.BORDER)

        slider = FlatSlider(
            row,
            variable=var,
            from_=lo,
            to=hi,
            bg=self.SURFACE,
            track_color=self.INPUT,
            fill_color=self.ACCENT,
            thumb_color=self.TEXT,
            height=max(step_size, int(minus_btn.cget("height"))),
            on_change=on_change,
        )
        minus_btn.pack(side=tk.LEFT, padx=(0, 5))
        plus_btn.pack(side=tk.RIGHT, padx=(5, 0))
        slider.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return slider

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self._browse_folder())
        self.root.bind("<Control-O>", lambda _e: self._browse_folder())
        self.root.bind("<Control-p>", lambda _e: self._open_script_preview())
        self.root.bind("<Control-P>", lambda _e: self._open_script_preview())
        self.root.bind("<F5>", lambda _e: self._trigger_scan_now())
        self.root.bind("<Control-b>", lambda _e: self._start_build() if not self.is_building else None)
        self.root.bind("<Control-B>", lambda _e: self._start_build() if not self.is_building else None)
        self.root.bind("<Escape>", lambda _e: self._cancel_build() if self.is_building else None)

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
        ).grid(row=row + 1, column=col, sticky="ew", padx=(0, pad_right), pady=(1, 3), ipady=2)

    def _update_zstd_ui(self, lvl: int) -> None:
        if lvl == 0:
            note = "Off (Raw .img)"
            color = self.WARN
        elif lvl <= 3:
            note = "Fast"
            color = self.ACCENT_HOVER
        elif lvl <= 12:
            note = "Balanced"
            color = self.ACCENT_HOVER
        elif lvl < 20:
            note = "High"
            color = self.OK
        else:
            note = "Ultra"
            color = self.SUPER_FG
        self.zstd_lbl.config(text=f"Level {lvl} ({note})", fg=color)
        if hasattr(self, "zip_var"):
            self._update_zip_ui(self.zip_var.get())

    def _update_zip_ui(self, lvl: int) -> None:
        zstd_on = self.zstd_var.get() > 0
        note = "Store" if lvl == 0 else ("Fast" if lvl == 1 else ("Max" if lvl == 9 else f"Deflate {lvl}"))
        if zstd_on:
            self.zip_title_lbl.config(text="ZIP Level (.zst stored)")
            self.zip_lbl.config(text=f"Level {lvl} ({note})", fg=self.MUTED)
            self.zip_slider.set_fill_color("#3e485c")
        else:
            self.zip_title_lbl.config(text="ZIP Level (raw images)")
            self.zip_lbl.config(text=f"Level {lvl} ({note})", fg=self.ACCENT_HOVER)
            self.zip_slider.set_fill_color(self.ACCENT)

    def _update_vbmeta_ui(self) -> None:
        mode = self.vbmeta_var.get()
        active_palette = {
            "skip": (self.ACCENT, self.ACCENT_HOVER, self.TEXT, self.ACCENT),
            "disable": ("#b86a19", "#cf7b24", self.TEXT, "#cf7b24"),
            "enable": ("#278255", "#329c68", self.TEXT, "#329c68"),
        }
        hints = {
            "skip": "Leave vbmeta untouched (requires partition images)",
            "disable": "Disable verity & verification via avbctl (works standalone too)",
            "enable": "Enable verity & verification via avbctl (works standalone too)",
        }
        for key, pill in self.vbmeta_pills.items():
            if key == mode:
                pill.set_colors(*active_palette[key])
            else:
                pill.set_colors(self.INPUT, self.SURFACE_HOVER, self.MUTED, self.BORDER)
        self.vbmeta_hint_lbl.config(text=hints.get(mode, ""))
        if hasattr(self, "out_name_lbl"):
            self._update_output_preview()

    def _resolve_output_dir(self) -> str:
        raw = self.out_dir_var.get().strip().strip('"').strip("'") if hasattr(self, "out_dir_var") else self.output_dir
        target = os.path.abspath(os.path.expanduser(raw)) if raw else os.path.join(ROOT_DIR, "output")
        self.output_dir = target
        return target

    def _sync_output_dir_from_entry(self) -> None:
        resolved = self._resolve_output_dir()
        if self.out_dir_var.get() != resolved:
            self.out_dir_var.set(resolved)

    def _update_output_preview(self) -> None:
        dev = self.device_var.get().strip() or DEFAULT_DEVICE
        code = self.codename_var.get().strip()
        ver = self.version_var.get().strip() or DEFAULT_VERSION
        fname = format_output_filename(dev, ver, code)
        self.out_name_lbl.config(text=fname)

    def _browse_folder(self) -> None:
        cur = self.path_var.get().strip().strip('"').strip("'")
        init = cur if os.path.isdir(cur) else ROOT_DIR
        folder = filedialog.askdirectory(title="Select Partition Images Folder", initialdir=init)
        if folder:
            self.path_var.set(os.path.abspath(folder))
            self._trigger_scan_now()

    def _browse_output_dir(self) -> None:
        cur = self._resolve_output_dir()
        folder = filedialog.askdirectory(title="Select Output Folder", initialdir=cur if os.path.isdir(cur) else ROOT_DIR)
        if folder:
            self.output_dir = os.path.abspath(folder)
            self.out_dir_var.set(self.output_dir)

    def _schedule_scan(self, _event=None) -> None:
        if self.scan_after_id is not None:
            self.root.after_cancel(self.scan_after_id)
        self.scan_after_id = self.root.after(240, self._trigger_scan_now)

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
                parts, props = scan_folder(path)
                self.ui_queue.put(("scan_ok", seq, parts, props))
            except Exception as exc:
                self.ui_queue.put(("scan_err", seq, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _sort_partitions(self, col: str) -> None:
        if not self.partitions:
            return
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = col in ("raw", "disk")

        key_map = {
            "name": lambda p: p.name.lower(),
            "kind": lambda p: (p.kind, p.name.lower()),
            "fmt": lambda p: (p.fmt, p.name.lower()),
            "raw": lambda p: p.raw_size,
            "disk": lambda p: p.file_size,
        }
        self.partitions.sort(key=key_map[col], reverse=self.sort_desc)
        self._update_sort_headings()
        self._refresh_tree()

    def _update_sort_headings(self) -> None:
        base_titles = {"name": "Partition", "kind": "Type", "fmt": "Fmt", "raw": "Raw", "disk": "File"}
        for key, label in base_titles.items():
            if key == self.sort_col:
                suffix = " v" if self.sort_desc else " ^"
                self.tree.heading(key, text=label + suffix)
            else:
                self.tree.heading(key, text=label)

    def _row_values_and_tags(self, idx: int, p: PartitionImage) -> tuple:
        mark = "[x]" if p.selected else "[ ]"
        stripe = "odd" if (idx % 2) else "even"
        if not p.selected:
            tags = (stripe, "unchecked")
        elif p.kind in ("dynamic", "super_raw", "bootchain", "bootloader"):
            tags = (stripe, p.kind)
        else:
            tags = (stripe,)
        vals = (mark, p.name, p.kind, p.fmt, _fmt_size(p.raw_size), _fmt_size(p.file_size))
        return vals, tags

    def _update_sel_summary(self) -> None:
        sel_count = sum(1 for p in self.partitions if p.selected)
        sel_raw = sum(p.raw_size for p in self.partitions if p.selected)
        self.sel_summary_lbl.config(
            text=f"Selected: {sel_count}/{len(self.partitions)} | Raw: {_fmt_size(sel_raw)}"
        )

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for idx, p in enumerate(self.partitions):
            vals, tags = self._row_values_and_tags(idx, p)
            self.tree.insert("", tk.END, iid=str(idx), values=vals, tags=tags)
        self._update_sel_summary()

    def _update_all_rows_in_place(self) -> None:
        existing = set(self.tree.get_children())
        if len(existing) != len(self.partitions):
            self._refresh_tree()
            return
        for idx, p in enumerate(self.partitions):
            vals, tags = self._row_values_and_tags(idx, p)
            self.tree.item(str(idx), values=vals, tags=tags)
        self._update_sel_summary()

    def _toggle_index(self, idx: int) -> None:
        if 0 <= idx < len(self.partitions):
            p = replace(self.partitions[idx], selected=not self.partitions[idx].selected)
            self.partitions[idx] = p
            iid = str(idx)
            if self.tree.exists(iid):
                vals, tags = self._row_values_and_tags(idx, p)
                self.tree.item(iid, values=vals, tags=tags)
                self._update_sel_summary()
            else:
                self._refresh_tree()
            self.tree.selection_set(iid)

    def _on_tree_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self._toggle_index(int(row_id))

    def _on_tree_space(self, _event) -> None:
        sel = self.tree.selection()
        if sel:
            self._toggle_index(int(sel[0]))

    def _set_all_selected(self, flag: bool) -> None:
        self.partitions = [replace(p, selected=flag) for p in self.partitions]
        self._update_all_rows_in_place()

    def _select_safe_only(self) -> None:
        self.partitions = [replace(p, selected=(p.kind != "bootloader")) for p in self.partitions]
        self._update_all_rows_in_place()

    def _current_config(self) -> BuildConfig:
        imgs_dir = self.path_var.get().strip().strip('"').strip("'")
        dev = self.device_var.get().strip() or DEFAULT_DEVICE
        code = self.codename_var.get().strip()
        ver = self.version_var.get().strip() or DEFAULT_VERSION
        maint = self.maintainer_var.get().strip() or DEFAULT_MAINTAINER

        out_dir = self._resolve_output_dir()
        os.makedirs(out_dir, exist_ok=True)
        mode = self.vbmeta_var.get()
        out_zip = os.path.join(out_dir, format_output_filename(dev, ver, code))

        return BuildConfig(
            imgs_dir=imgs_dir,
            output_zip=out_zip,
            device=dev,
            codename=code,
            version=ver,
            maintainer=maint,
            vbmeta_mode=mode,
            zstd_level=self.zstd_var.get(),
            zip_level=self.zip_var.get(),
            partitions=list(self.partitions),
        )

    def _append_logs(self, lines: List[str]) -> None:
        if not lines:
            return
        self.log_text.config(state=tk.NORMAL)
        for line in lines:
            tag = None
            if line.startswith("ERROR:"):
                tag = "err"
            elif line.startswith("Done in "):
                tag = "ok"
            elif line.startswith("--- "):
                tag = "hdr"
            if tag:
                self.log_text.insert(tk.END, line + "\n", tag)
            else:
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
        has_sel = any(p.selected for p in self.partitions)
        if not has_sel and self.vbmeta_var.get() == "skip":
            messagebox.showinfo("Newbies Flasher", "Select a folder with at least one checked partition, or pick Disable/Enable AVB 2.0.")
            return

        body = preview_update_binary(self._current_config())
        win = tk.Toplevel(self.root)
        win.title("Recovery Script Preview (update-binary)")
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        pw = min(self._s(820), max(500, sw - 80))
        ph = min(self._s(540), max(380, sh - 100))
        win.geometry(f"{pw}x{ph}")
        win.minsize(420, 300)
        win.configure(bg=self.BG)
        _apply_windows_dark_chrome(win)
        win.bind("<Escape>", lambda _e: win.destroy())

        bar = tk.Frame(win, bg=self.BG, padx=10, pady=8)
        bar.pack(fill=tk.X)
        line_count = len(body.splitlines())

        copy_btn = self._pill(bar, "Copy Script", None, bg=self.ACCENT, hover=self.ACCENT_HOVER, parent_bg=self.BG, pad=(12, 4), size=8, bold=True, pill=True)

        def _do_copy():
            win.clipboard_clear()
            win.clipboard_append(body)
            copy_btn.set_text("Copied")
            win.after(1500, lambda: copy_btn.winfo_exists() and copy_btn.set_text("Copy Script"))

        def _do_save():
            path = filedialog.asksaveasfilename(
                parent=win,
                title="Save Recovery Script",
                initialfile="update-binary",
                filetypes=[("Shell script", "*.sh"), ("All files", "*.*")],
            )
            if path:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(body)

        copy_btn.set_command(_do_copy)
        copy_btn.pack(side=tk.RIGHT)
        save_btn = self._pill(bar, "Save As...", _do_save, bg=self.SURFACE_ALT, hover=self.SURFACE_HOVER, parent_bg=self.BG, pad=(10, 4), size=8, bold=True, pill=True, border=self.BORDER)
        save_btn.pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(
            bar,
            text=f"META-INF/com/google/android/update-binary ({line_count} lines)",
            font=(self.ui_font, 9, "bold"),
            fg=self.MUTED,
            bg=self.BG,
        ).pack(side=tk.LEFT)

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
        has_sel = any(p.selected for p in cfg.partitions or [])
        if not has_sel and cfg.vbmeta_mode == "skip":
            if not cfg.imgs_dir or not os.path.isdir(cfg.imgs_dir):
                self.stage_lbl.config(text="Select an images folder, or set AVB 2.0 to Disable/Enable", fg=self.WARN)
            else:
                self.stage_lbl.config(text="Check at least one partition to package", fg=self.WARN)
            return

        self.is_building = True
        self.cancel_event.clear()
        self.build_start_ts = time.monotonic()
        self.progress_var.set(0.0)
        self.build_btn.set_enabled(False)
        self.build_btn.set_text("Building...")
        self.cancel_btn.set_enabled(True)
        self.stage_lbl.config(text="Starting...", fg=self.ACCENT_HOVER)
        self.stat_lbl.config(text="0% | 0.0s")
        self._append_logs([f"--- Build started: {os.path.basename(cfg.output_zip)} ---"])

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
        self.cancel_btn.set_enabled(False)
        self.stage_lbl.config(text="Cancelling...", fg=self.WARN)

    def _open_output(self) -> None:
        out_dir = self._resolve_output_dir()
        os.makedirs(out_dir, exist_ok=True)
        target = self.last_output if (self.last_output and os.path.isfile(self.last_output)) else out_dir
        reveal_in_file_manager(target)

    def _on_close(self) -> None:
        self.cancel_event.set()
        self._cancel_timers()
        self.root.destroy()

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self._cancel_timers()

    def _cancel_timers(self) -> None:
        for attr in ("poll_after_id", "scan_after_id", "resize_after_id"):
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
            is_iconic = self.root.state() == "iconic"
        except tk.TclError:
            return

        pending_logs: List[str] = []
        last_prog = None

        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "log":
                pending_logs.append(item[1])
            elif kind == "prog":
                last_prog = (item[1], item[2])
            elif kind == "scan_ok":
                _, seq, parts, props = item
                if seq != self.scan_seq:
                    continue
                self.partitions = parts
                self.sort_col = ""
                self._update_sort_headings()
                self._refresh_tree()
                if parts:
                    dyn_n = sum(1 for p in parts if p.kind in ("dynamic", "super_raw"))
                    boot_n = sum(1 for p in parts if p.kind == "bootchain")
                    bl_n = sum(1 for p in parts if p.kind == "bootloader")
                    fw_n = len(parts) - dyn_n - boot_n - bl_n
                    summary = f"{len(parts)} images ({dyn_n} dyn, {boot_n} boot, {fw_n} fw"
                    if bl_n:
                        summary += f", {bl_n} bl"
                    summary += ")"
                    self.scan_lbl.config(text=summary, fg=self.OK)
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
            elif kind == "build_ok":
                res: BuildResult = item[1]
                self.is_building = False
                self.last_output = res.output_zip
                self.progress_var.set(100.0)
                self.build_btn.set_enabled(True)
                self.build_btn.set_text("Build Flashable ZIP")
                self.cancel_btn.set_enabled(False)
                self.stage_lbl.config(text=f"Complete: {os.path.basename(res.output_zip)}", fg=self.OK)
                self.stat_lbl.config(text=f"{res.size_mb:.1f} MB | {res.elapsed_sec:.1f}s")
            elif kind == "build_err":
                err_msg = item[1]
                self.is_building = False
                self.build_btn.set_enabled(True)
                self.build_btn.set_text("Build Flashable ZIP")
                self.cancel_btn.set_enabled(False)
                self.stage_lbl.config(text=f"Failed: {err_msg}", fg=self.WARN)
                self.stat_lbl.config(text="")
                pending_logs.append(f"ERROR: {err_msg}")

        if pending_logs:
            self._append_logs(pending_logs)
        if last_prog and self.is_building:
            pct, stage = last_prog
            elapsed = time.monotonic() - self.build_start_ts
            self.progress_var.set(pct)
            self.stage_lbl.config(text=stage, fg=self.ACCENT_HOVER)
            self.stat_lbl.config(text=f"{pct:.0f}% | {elapsed:.1f}s")

        if self.is_building:
            interval = 40
        elif is_iconic:
            interval = 300
        else:
            interval = 120
        self.poll_after_id = self.root.after(interval, self._poll_queue)


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
