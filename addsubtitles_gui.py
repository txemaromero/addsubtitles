#!/usr/bin/env python3
# addsubtitles_gui.py

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ---------------------------
# Helpers: FFmpeg / subtitles
# ---------------------------

def which_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def which_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def _fps_from_ratio(ratio: str) -> str:
    """Converts 'num/den' (r_frame_rate) to a readable fps, e.g. '29.97'."""
    try:
        num, den = ratio.split("/")
        den_f = float(den)
        if den_f == 0:
            return "?"
        fps = float(num) / den_f
        return f"{fps:.0f}" if abs(fps - round(fps)) < 0.01 else f"{fps:.2f}"
    except (ValueError, ZeroDivisionError):
        return "?"


def probe_video_info(path: Path) -> str | None:
    """
    Returns a short video description (resolution · duration · fps) using
    ffprobe. None if ffprobe is unavailable or the file cannot be read.
    """
    ffprobe = which_ffprobe()
    if ffprobe is None:
        return None
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WINDOW_FLAGS,
        ).stdout
        data = json.loads(out)
    except (OSError, ValueError):
        return None

    stream = (data.get("streams") or [{}])[0]
    w, h = stream.get("width"), stream.get("height")
    parts = []
    if w and h:
        parts.append(f"{w}x{h}")
    try:
        dur = float(data.get("format", {}).get("duration", ""))
        parts.append(ms_to_srt_time(int(dur * 1000)).split(",")[0])
    except (TypeError, ValueError):
        pass
    ratio = stream.get("r_frame_rate")
    if ratio and ratio != "0/0":
        parts.append(f"{_fps_from_ratio(ratio)} fps")
    return "  ·  ".join(parts) or None


_TIME_LINE_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3}).*$"
)

# Read FFmpeg progress from its output
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_PROGRESS_TIME_RE = re.compile(r"\btime=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _hms_to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


# Hide the FFmpeg console on Windows (avoids the black window flashing)
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Default subtitle style values (for "Restore style")
_STYLE_DEFAULTS = {
    "font": "", "fontsize": "21", "color": "#FFFFFF", "outline_color": "#000000",
    "outline": "2", "shadow": "1", "align": "2", "margin_v": "15",
    "margin_l": "", "margin_r": "",
}


def detect_srt_encoding(path: Path) -> str:
    """
    Detects a .srt file's encoding with no external dependencies.
    Returns a Python codec: 'utf-8-sig', 'utf-16', 'utf-8' or 'cp1252'.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return "utf-8"

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        # Typical fallback for Windows-originated subtitles
        return "cp1252"


def ffmpeg_charenc(enc: str) -> str:
    """Translates the Python codec to the name FFmpeg expects (charenc)."""
    return {"utf-8-sig": "UTF-8", "utf-16": "UTF-16"}.get(enc, enc)


# Video encoders we offer, in order of preference (software + GPU)
_VENC_CANDIDATES = [
    "libx264", "libx265",
    "h264_nvenc", "hevc_nvenc",   # NVIDIA
    "h264_qsv", "hevc_qsv",       # Intel Quick Sync
    "h264_amf", "hevc_amf",       # AMD
    "h264_videotoolbox", "hevc_videotoolbox",  # macOS
]


def available_video_encoders() -> list[str]:
    """
    Returns the relevant video encoders actually available in the FFmpeg
    installation (including GPU ones if the machine supports them).
    """
    ffmpeg = which_ffmpeg()
    if ffmpeg is None:
        return ["libx264"]
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, creationflags=_NO_WINDOW_FLAGS,
        ).stdout
    except Exception:
        return ["libx264"]
    found = [
        c for c in _VENC_CANDIDATES
        if re.search(rf"^\s*V\S*\s+{re.escape(c)}\b", out, re.MULTILINE)
    ]
    return found or ["libx264"]


def video_quality_args(vcodec: str, crf: int) -> list[str]:
    """
    Translates the 'constant quality' value to the correct parameter for the
    encoder family (GPU encoders do not use -crf).
    """
    v = vcodec.lower()
    if "nvenc" in v:
        return ["-cq", str(crf)]
    if "qsv" in v:
        return ["-global_quality", str(crf)]
    if "amf" in v:
        return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)]
    if "videotoolbox" in v:
        return ["-q:v", str(crf)]
    # libx264 / libx265 / libvpx / software in general
    return ["-crf", str(crf)]


def config_file() -> Path:
    """Path of the JSON file where the last settings are remembered."""
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    root = Path(base) if base else Path.home()
    return root / "addsubtitles_gui" / "settings.json"


def srt_time_to_ms(t: str) -> int:
    hh, mm, rest = t.split(":")
    ss, mmm = rest.split(",")
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(mmm)


def ms_to_srt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def shift_srt_file(in_srt: Path, offset_seconds: float, out_srt: Path, encoding: str = "utf-8") -> None:
    """
    Shifts the SRT timings and renumbers blocks.
    Blocks that fall entirely before t=0 are discarded.
    """
    raw = in_srt.read_text(encoding=encoding, errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in re.split(r"\n\s*\n", raw) if b.strip()]

    offset_ms = int(round(offset_seconds * 1000))
    out_blocks: list[str] = []
    new_idx = 1

    for block in blocks:
        lines = block.split("\n")
        if not lines:
            continue

        # Find the timing line
        time_i = None
        for i, ln in enumerate(lines[:6]):
            if _TIME_LINE_RE.match(ln):
                time_i = i
                break
        if time_i is None:
            continue

        m = _TIME_LINE_RE.match(lines[time_i])
        assert m is not None
        start_ms = srt_time_to_ms(m.group(1)) + offset_ms
        end_ms = srt_time_to_ms(m.group(2)) + offset_ms

        if end_ms <= 0:
            continue

        start_ms = max(0, start_ms)
        end_ms = max(0, end_ms)
        if end_ms <= start_ms:
            continue

        lines[time_i] = f"{ms_to_srt_time(start_ms)} --> {ms_to_srt_time(end_ms)}"

        # Ensure the correct index
        if lines[0].strip().isdigit():
            lines[0] = str(new_idx)
        else:
            lines.insert(0, str(new_idx))

        out_blocks.append("\n".join(lines).strip())
        new_idx += 1

    out_srt.write_text("\n\n".join(out_blocks) + "\n", encoding=encoding, errors="replace")


def first_cue_center_ms(srt_path: Path, encoding: str = "utf-8") -> int | None:
    """
    Returns the midpoint (ms) of the first subtitle in the .srt, useful for
    placing the preview on a frame that actually shows text.
    None if no timing line is found.
    """
    try:
        raw = srt_path.read_text(encoding=encoding, errors="replace")
    except OSError:
        return None
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    for ln in raw.split("\n"):
        m = _TIME_LINE_RE.match(ln)
        if m:
            return (srt_time_to_ms(m.group(1)) + srt_time_to_ms(m.group(2))) // 2
    return None


def escape_for_subtitles_filter(p: Path) -> str:
    """
    Escapes a path for use inside the subtitles=filename='...' filter.
    """
    s = str(p.resolve()).replace("\\", "/")
    s = s.replace("'", r"\'")
    s = s.replace(":", r"\:")  # useful on Windows C:\...
    return s


def ass_color_from_hex(rgb: str) -> str:
    """
    '#RRGGBB' -> '&H00BBGGRR' (ASS)
    """
    s = rgb.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        raise ValueError("Invalid colour. Use '#RRGGBB' (e.g. #FFFFFF).")
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def build_force_style(font, fontsize, color, outline_color, outline, shadow, align, margin_v, margin_l, margin_r) -> str:
    """
    Returns an ASS 'force_style' for the subtitles filter.
    """
    parts = []
    if font:
        parts.append(f"Fontname={font}")
    if fontsize is not None:
        parts.append(f"Fontsize={fontsize}")
    if color:
        parts.append(f"PrimaryColour={ass_color_from_hex(color)}")
    if outline_color:
        parts.append(f"OutlineColour={ass_color_from_hex(outline_color)}")

    parts.append("BorderStyle=1")  # classic outline/shadow

    if outline is not None:
        parts.append(f"Outline={outline}")
    if shadow is not None:
        parts.append(f"Shadow={shadow}")
    if align is not None:
        parts.append(f"Alignment={align}")

    if margin_v is not None:
        parts.append(f"MarginV={margin_v}")
    if margin_l is not None:
        parts.append(f"MarginL={margin_l}")
    if margin_r is not None:
        parts.append(f"MarginR={margin_r}")

    return ",".join(parts)


# ---------------------------
# GUI App
# ---------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Add subtitles (.srt) to video (FFmpeg)")
        self.geometry("980x720")
        self.minsize(920, 650)

        self.msg_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.stop_requested = False
        self._temp_dir_obj: tempfile.TemporaryDirectory | None = None
        self._last_out: Path | None = None
        self._venc_choices = available_video_encoders()
        self._preview_dir: tempfile.TemporaryDirectory | None = None
        self._preview_img = None  # keep a live reference to the PhotoImage
        self._last_dir = ""       # last folder used in the dialogs
        self.video_info = tk.StringVar(value="")

        # Paths
        self.video_path = tk.StringVar()
        self.srt_path = tk.StringVar()
        self.out_path = tk.StringVar()

        # Mode / basics
        self.mode = tk.StringVar(value="burn")  # burn | soft
        self.offset = tk.StringVar(value="0.0")
        self.encoding = tk.StringVar(value="auto")
        self.lang = tk.StringVar(value="eng")
        self.overwrite = tk.BooleanVar(value=False)

        # Burn style (Size=21, Margin V=15; see _STYLE_DEFAULTS)
        self.font = tk.StringVar(value=_STYLE_DEFAULTS["font"])
        self.fontsize = tk.StringVar(value=_STYLE_DEFAULTS["fontsize"])
        self.color = tk.StringVar(value=_STYLE_DEFAULTS["color"])
        self.outline_color = tk.StringVar(value=_STYLE_DEFAULTS["outline_color"])
        self.outline = tk.StringVar(value=_STYLE_DEFAULTS["outline"])
        self.shadow = tk.StringVar(value=_STYLE_DEFAULTS["shadow"])
        self.align = tk.StringVar(value=_STYLE_DEFAULTS["align"])  # 2 = bottom centre
        self.margin_v = tk.StringVar(value=_STYLE_DEFAULTS["margin_v"])
        self.margin_l = tk.StringVar(value=_STYLE_DEFAULTS["margin_l"])
        self.margin_r = tk.StringVar(value=_STYLE_DEFAULTS["margin_r"])

        # Burn encode params
        self.vcodec = tk.StringVar(value="libx264")
        self.preset = tk.StringVar(value="medium")
        self.crf = tk.StringVar(value="18")
        self.acodec = tk.StringVar(value="copy")  # copy / aac / etc.
        self.abitrate = tk.StringVar(value="192k")

        self._build_ui()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)

        if which_ffmpeg() is None:
            messagebox.showwarning(
                "FFmpeg not found",
                "'ffmpeg' was not found on the PATH.\n\n"
                "Install FFmpeg and make sure 'ffmpeg -version' works in a terminal."
            )

    # -------- Persistent settings --------

    def _settings_vars(self) -> dict:
        """Variables remembered across sessions (does not include file paths)."""
        return {
            "mode": self.mode, "offset": self.offset, "encoding": self.encoding,
            "lang": self.lang, "overwrite": self.overwrite,
            "font": self.font, "fontsize": self.fontsize, "color": self.color,
            "outline_color": self.outline_color, "outline": self.outline,
            "shadow": self.shadow, "align": self.align, "margin_v": self.margin_v,
            "margin_l": self.margin_l, "margin_r": self.margin_r,
            "vcodec": self.vcodec, "preset": self.preset, "crf": self.crf,
            "acodec": self.acodec, "abitrate": self.abitrate,
        }

    def _load_settings(self):
        try:
            data = json.loads(config_file().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        for key, var in self._settings_vars().items():
            if key in data:
                try:
                    var.set(data[key])
                except Exception:
                    pass
        last_dir = data.get("_last_dir")
        if isinstance(last_dir, str) and os.path.isdir(last_dir):
            self._last_dir = last_dir
        self._toggle_mode()

    def _save_settings(self):
        data = {key: var.get() for key, var in self._settings_vars().items()}
        data["_last_dir"] = self._last_dir
        try:
            path = config_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        self._save_settings()
        if self._preview_dir is not None:
            try:
                self._preview_dir.cleanup()
            except Exception:
                pass
        self.destroy()

    # -------- UI --------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        files = ttk.LabelFrame(top, text="Files")
        files.pack(fill="x", expand=True, **pad)

        self._row_file(files, "Video:", self.video_path, self._pick_video)
        ttk.Label(files, textvariable=self.video_info, foreground="gray").pack(anchor="w", padx=(150, 10))
        self._row_file(files, "Subtitles (.srt):", self.srt_path, self._pick_srt)
        self._row_file(files, "Output:", self.out_path, self._pick_out, save=True)

        opts = ttk.Frame(top)
        opts.pack(fill="x", **pad)

        mode_box = ttk.LabelFrame(opts, text="Mode")
        mode_box.pack(side="left", fill="y", **pad)
        ttk.Radiobutton(mode_box, text="Burn in (hard-coded)", variable=self.mode, value="burn", command=self._toggle_mode).pack(anchor="w", padx=10, pady=4)
        ttk.Radiobutton(mode_box, text="Embed track (soft)", variable=self.mode, value="soft", command=self._toggle_mode).pack(anchor="w", padx=10, pady=4)

        basic = ttk.LabelFrame(opts, text="Settings")
        basic.pack(side="left", fill="both", expand=True, **pad)

        r = ttk.Frame(basic)
        r.pack(fill="x", padx=10, pady=4)

        ttk.Label(r, text="Offset (sec):").pack(side="left")
        ttk.Entry(r, textvariable=self.offset, width=10).pack(side="left", padx=6)

        ttk.Label(r, text="SRT encoding:").pack(side="left", padx=(20, 0))
        ttk.Combobox(r, textvariable=self.encoding, values=["auto", "utf-8", "cp1252", "latin-1"], width=10, state="readonly").pack(side="left", padx=6)

        ttk.Label(r, text="Track language (soft):").pack(side="left", padx=(20, 0))
        ttk.Entry(r, textvariable=self.lang, width=8).pack(side="left", padx=6)

        ttk.Checkbutton(r, text="Overwrite output", variable=self.overwrite).pack(side="left", padx=(20, 0))

        # Burn-in frame (style + encode)
        self.burn_frame = ttk.LabelFrame(self, text="Style (burn-in only) and re-encode parameters")
        self.burn_frame.pack(fill="x", padx=16, pady=8)

        style = ttk.Frame(self.burn_frame)
        style.pack(fill="x", padx=10, pady=6)

        row1 = ttk.Frame(style); row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="Font:").pack(side="left")
        ttk.Entry(row1, textvariable=self.font, width=22).pack(side="left", padx=6)

        ttk.Label(row1, text="Size:").pack(side="left", padx=(16, 0))
        ttk.Entry(row1, textvariable=self.fontsize, width=6).pack(side="left", padx=6)

        ttk.Label(row1, text="Colour:").pack(side="left", padx=(16, 0))
        ttk.Entry(row1, textvariable=self.color, width=10).pack(side="left", padx=6)

        ttk.Label(row1, text="Outline colour:").pack(side="left", padx=(16, 0))
        ttk.Entry(row1, textvariable=self.outline_color, width=10).pack(side="left", padx=6)

        row2 = ttk.Frame(style); row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="Outline:").pack(side="left")
        ttk.Entry(row2, textvariable=self.outline, width=6).pack(side="left", padx=6)

        ttk.Label(row2, text="Shadow:").pack(side="left", padx=(16, 0))
        ttk.Entry(row2, textvariable=self.shadow, width=6).pack(side="left", padx=6)

        ttk.Label(row2, text="Alignment (1-9):").pack(side="left", padx=(16, 0))
        ttk.Entry(row2, textvariable=self.align, width=6).pack(side="left", padx=6)

        ttk.Label(row2, text="Margin V:").pack(side="left", padx=(16, 0))
        ttk.Entry(row2, textvariable=self.margin_v, width=6).pack(side="left", padx=6)

        ttk.Label(row2, text="Margin L:").pack(side="left", padx=(16, 0))
        ttk.Entry(row2, textvariable=self.margin_l, width=6).pack(side="left", padx=6)

        ttk.Label(row2, text="Margin R:").pack(side="left", padx=(16, 0))
        ttk.Entry(row2, textvariable=self.margin_r, width=6).pack(side="left", padx=6)

        enc = ttk.Frame(self.burn_frame)
        enc.pack(fill="x", padx=10, pady=6)
        ttk.Label(enc, text="VCodec:").pack(side="left")
        ttk.Combobox(enc, textvariable=self.vcodec, values=self._venc_choices, width=18).pack(side="left", padx=6)

        ttk.Label(enc, text="Preset:").pack(side="left", padx=(16, 0))
        ttk.Entry(enc, textvariable=self.preset, width=10).pack(side="left", padx=6)

        ttk.Label(enc, text="Quality (CRF/CQ):").pack(side="left", padx=(16, 0))
        ttk.Entry(enc, textvariable=self.crf, width=6).pack(side="left", padx=6)

        ttk.Label(enc, text="ACodec:").pack(side="left", padx=(16, 0))
        ttk.Entry(enc, textvariable=self.acodec, width=10).pack(side="left", padx=6)

        ttk.Label(enc, text="A bitrate:").pack(side="left", padx=(16, 0))
        ttk.Entry(enc, textvariable=self.abitrate, width=8).pack(side="left", padx=6)

        prev = ttk.Frame(self.burn_frame)
        prev.pack(fill="x", padx=10, pady=(0, 6))
        self.btn_preview = ttk.Button(prev, text="Style preview", command=self.preview)
        self.btn_preview.pack(side="left")
        ttk.Button(prev, text="Restore style", command=self._reset_style).pack(side="left", padx=8)
        ttk.Label(prev, text="Generates a single frame with the subtitles burnt in (without re-encoding the video).").pack(side="left", padx=10)

        # Actions
        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=16, pady=6)

        self.btn_start = ttk.Button(actions, text="Process", command=self.start)
        self.btn_start.pack(side="left")

        self.btn_cancel = ttk.Button(actions, text="Cancel", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)

        self.btn_open = ttk.Button(actions, text="Open folder", command=self._open_output_folder, state="disabled")
        self.btn_open.pack(side="left", padx=8)

        self.pct = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.pct, width=6, anchor="e").pack(side="right")

        self.progress = ttk.Progressbar(actions, mode="indeterminate")
        self.progress.pack(side="right", fill="x", expand=True, padx=8)

        # Log
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, padx=16, pady=10)

        self.log = tk.Text(log_frame, height=18, wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

        self._toggle_mode()

    def _row_file(self, parent, label, var, cmd, save=False):
        fr = ttk.Frame(parent)
        fr.pack(fill="x", padx=10, pady=5)
        ttk.Label(fr, text=label, width=18).pack(side="left")
        ttk.Entry(fr, textvariable=var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(fr, text="Browse…", command=cmd).pack(side="left")
        if not save:
            ttk.Button(fr, text="Auto output", command=self._auto_output).pack(side="left", padx=6)

    def _remember_dir(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            self._last_dir = parent

    def _pick_video(self):
        path = filedialog.askopenfilename(
            title="Select video",
            initialdir=self._last_dir or None,
            filetypes=[("Videos", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All", "*.*")]
        )
        if path:
            self.video_path.set(path)
            self._remember_dir(path)
            self._auto_output()
            self._probe_video_async(path)

    def _pick_srt(self):
        path = filedialog.askopenfilename(
            title="Select .srt",
            initialdir=self._last_dir or None,
            filetypes=[("SRT subtitles", "*.srt"), ("All", "*.*")]
        )
        if path:
            self.srt_path.set(path)
            self._remember_dir(path)
            self._auto_output()

    def _pick_out(self):
        default_ext = ".mp4"
        if self.video_path.get():
            default_ext = Path(self.video_path.get()).suffix or ".mp4"
        path = filedialog.asksaveasfilename(
            title="Save output",
            initialdir=self._last_dir or None,
            defaultextension=default_ext,
            filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("MOV", "*.mov"), ("All", "*.*")]
        )
        if path:
            self.out_path.set(path)
            self._remember_dir(path)

    def _probe_video_async(self, path: str):
        """Reads the video's resolution/duration/fps in the background (ffprobe)."""
        self.video_info.set("Reading video information…")

        def worker():
            info = probe_video_info(Path(path))
            self.msg_queue.put(("video_info", info or ""))

        threading.Thread(target=worker, daemon=True).start()

    def _reset_style(self):
        """Restores the subtitle style's default values."""
        for name, value in _STYLE_DEFAULTS.items():
            getattr(self, name).set(value)

    def _auto_output(self):
        v = self.video_path.get().strip()
        if not v:
            return
        vp = Path(v)
        suffix = vp.suffix or ".mp4"
        tag = "_soft" if self.mode.get() == "soft" else "_subs"
        self.out_path.set(str(vp.with_name(vp.stem + tag + suffix)))

    def _toggle_mode(self):
        if self.mode.get() == "burn":
            self.burn_frame.pack(fill="x", padx=16, pady=8)
        else:
            self.burn_frame.forget()
        self._auto_output()

    # -------- Processing --------

    def log_write(self, text: str):
        self.log.insert("end", text)
        self.log.see("end")

    def _validate_inputs(self) -> str | None:
        """
        Validates the numeric and colour settings BEFORE launching FFmpeg.
        Returns an error message (str), or None if everything is correct.
        Style fields are only validated in 'Burn-in' mode.
        """
        def check_int(value: str, label: str, lo: int | None = None, hi: int | None = None):
            s = (value or "").strip()
            if not s:
                return None
            try:
                n = int(s)
            except ValueError:
                return f"{label} must be a whole number."
            if lo is not None and n < lo:
                return f"{label} must be ≥ {lo}."
            if hi is not None and n > hi:
                return f"{label} must be ≤ {hi}."
            return None

        def check_float(value: str, label: str, lo: float | None = None):
            s = (value or "").strip()
            if not s:
                return None
            try:
                n = float(s)
            except ValueError:
                return f"{label} must be a number."
            if lo is not None and n < lo:
                return f"{label} must be ≥ {lo}."
            return None

        if self.mode.get() != "burn":
            return None

        checks = [
            check_int(self.fontsize.get(), "Size", lo=1),
            check_int(self.align.get(), "Alignment", lo=1, hi=9),
            check_int(self.margin_v.get(), "Margin V", lo=0),
            check_int(self.margin_l.get(), "Margin L", lo=0),
            check_int(self.margin_r.get(), "Margin R", lo=0),
            check_int(self.crf.get(), "CRF", lo=0, hi=63),
            check_float(self.outline.get(), "Outline", lo=0),
            check_float(self.shadow.get(), "Shadow", lo=0),
        ]
        for msg in checks:
            if msg:
                return msg

        for value, label in ((self.color.get(), "Colour"), (self.outline_color.get(), "Outline colour")):
            s = (value or "").strip()
            if s:
                try:
                    ass_color_from_hex(s)
                except ValueError:
                    return f"{label} is invalid. Use '#RRGGBB' (e.g. #FFFFFF)."

        return None

    def start(self):
        if which_ffmpeg() is None:
            messagebox.showerror("FFmpeg not found", "'ffmpeg' was not found on the PATH.")
            return

        video = Path(self.video_path.get().strip())
        srt = Path(self.srt_path.get().strip())
        out = Path(self.out_path.get().strip())

        if not video.exists():
            messagebox.showerror("Error", "Select a valid video.")
            return
        if not srt.exists():
            messagebox.showerror("Error", "Select a valid .srt.")
            return
        if not str(out):
            messagebox.showerror("Error", "Select an output path.")
            return

        try:
            offset = float(self.offset.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Error", "Offset must be a number (e.g. 0.5 or -1.2).")
            return

        err = self._validate_inputs()
        if err:
            messagebox.showerror("Invalid settings", err)
            return

        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot create the output folder:\n{e}")
            return

        self._save_settings()
        self._last_out = out
        self.log.delete("1.0", "end")
        self.stop_requested = False
        self.btn_start.config(state="disabled")
        self.btn_open.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.pct.set("")
        self.progress.config(mode="indeterminate", value=0)
        self.progress.start(10)

        self.worker_thread = threading.Thread(
            target=self._run_job, args=(video, srt, out, offset), daemon=True
        )
        self.worker_thread.start()

    def preview(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "Wait for the current process to finish.")
            return
        if which_ffmpeg() is None:
            messagebox.showerror("FFmpeg not found", "'ffmpeg' was not found on the PATH.")
            return

        video = Path(self.video_path.get().strip())
        srt = Path(self.srt_path.get().strip())
        if not video.exists():
            messagebox.showerror("Error", "Select a valid video.")
            return
        if not srt.exists():
            messagebox.showerror("Error", "Select a valid .srt.")
            return
        try:
            offset = float(self.offset.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Error", "Offset must be a number (e.g. 0.5 or -1.2).")
            return
        err = self._validate_inputs()
        if err:
            messagebox.showerror("Invalid settings", err)
            return

        self.btn_preview.config(state="disabled")
        threading.Thread(
            target=self._run_preview, args=(video, srt, offset), daemon=True
        ).start()

    def _run_preview(self, video: Path, srt: Path, offset: float):
        try:
            png = self._make_preview_png(video, srt, offset)
            self.msg_queue.put(("preview_ok", str(png)))
        except Exception as e:
            self.msg_queue.put(("preview_err", str(e)))

    def _make_preview_png(self, video: Path, srt: Path, offset: float) -> Path:
        ffmpeg = which_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("'ffmpeg' was not found on the PATH.")

        enc = (self.encoding.get().strip() or "auto")
        if enc == "auto":
            enc = detect_srt_encoding(srt)

        # Preview's own temporary directory (replaced each time)
        if self._preview_dir is not None:
            self._preview_dir.cleanup()
        self._preview_dir = tempfile.TemporaryDirectory(prefix="srt_preview_")
        d = Path(self._preview_dir.name)

        srt_for_filter = srt
        if offset != 0.0:
            shifted = d / "shifted.srt"
            shift_srt_file(srt, offset, shifted, encoding=enc)
            srt_for_filter = shifted

        # Place the frame on the first subtitle (already shifted if there is an offset)
        center = first_cue_center_ms(srt, enc)
        seek_ms = 0 if center is None else max(0, center + int(round(offset * 1000)))

        filt = self._subtitles_filter(srt_for_filter, enc)
        png = d / "preview.png"
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{seek_ms / 1000.0:.3f}", "-copyts",
            "-i", str(video),
            "-frames:v", "1", "-an", "-sn",
            "-vf", filt,
            "-update", "1", str(png),
        ]
        res = subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WINDOW_FLAGS,
        )
        if not png.exists():
            raise RuntimeError(res.stderr.strip()[-800:] or f"FFmpeg failed (code {res.returncode}).")
        return png

    def _show_preview(self, png_path: str):
        img = tk.PhotoImage(file=png_path)
        # Downscale by an integer factor if wider than ~1280 px
        factor = max(1, (img.width() + 1279) // 1280)
        if factor > 1:
            img = img.subsample(factor)
        self._preview_img = img  # keep a live reference

        win = tk.Toplevel(self)
        win.title("Style preview")
        win.transient(self)
        ttk.Label(win, image=img).pack(padx=8, pady=8)
        ttk.Label(
            win,
            text="Sample frame. Adjust the style and regenerate the preview if needed.",
        ).pack(padx=8, pady=(0, 8))

    def cancel(self):
        self.stop_requested = True
        self._kill_proc()
        self.msg_queue.put(("log", "\n[Cancelling…]\n"))

    def _kill_proc(self):
        """Stops FFmpeg reliably, including child processes on Windows."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            # taskkill kills the process tree (/T) by force (/F);
            # FFmpeg does not always respond to terminate() on Windows.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    creationflags=_NO_WINDOW_FLAGS,
                    capture_output=True,
                )
                return
            except Exception:
                pass
        for stop in (proc.terminate, proc.kill):
            try:
                stop()
            except Exception:
                pass

    def _open_output_folder(self):
        """Opens the file explorer at the output file's folder (selecting it if possible)."""
        out = self._last_out
        if not out or not out.exists():
            messagebox.showwarning("Not available", "There is no output file yet.")
            return
        try:
            if os.name == "nt":
                # /select highlights the file within its folder
                subprocess.run(["explorer", "/select,", str(out)])
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", str(out)])
            else:
                subprocess.run(["xdg-open", str(out.parent)])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open the folder:\n{e}")

    def _run_job(self, video: Path, srt: Path, out: Path, offset: float):
        try:
            cmd = self._build_ffmpeg_cmd(video, srt, out, offset)
            self.msg_queue.put(("log", "Running:\n  " + " ".join(cmd) + "\n\n"))
            self._run_subprocess(cmd)
            self.msg_queue.put(("done", "cancelled" if self.stop_requested else "ok"))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def _run_subprocess(self, cmd: list[str]):
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            universal_newlines=True,
            bufsize=1,
            creationflags=_NO_WINDOW_FLAGS,
        )
        assert self.proc.stdout is not None
        total_secs: float | None = None
        for line in self.proc.stdout:
            self.msg_queue.put(("log", line))

            if total_secs is None:
                md = _DURATION_RE.search(line)
                if md:
                    total_secs = _hms_to_seconds(*md.groups())
                    self.msg_queue.put(("progress_start", total_secs))

            if total_secs:
                mt = _PROGRESS_TIME_RE.search(line)
                if mt:
                    cur = _hms_to_seconds(*mt.groups())
                    pct = max(0.0, min(100.0, cur / total_secs * 100.0))
                    self.msg_queue.put(("progress", pct))

        ret = self.proc.wait()
        if self.stop_requested:
            return
        if ret != 0:
            raise RuntimeError(f"FFmpeg failed with code {ret}.")

    def _subtitles_filter(self, srt_for_filter: Path, enc: str) -> str:
        """Builds FFmpeg's 'subtitles' filter with the configured style."""
        force_style = build_force_style(
            self.font.get().strip() or None,
            self._int_or_none(self.fontsize.get()),
            self.color.get().strip() or None,
            self.outline_color.get().strip() or None,
            self._float_or_none(self.outline.get()),
            self._float_or_none(self.shadow.get()),
            self._int_or_none(self.align.get()),
            self._int_or_none(self.margin_v.get()),
            self._int_or_none(self.margin_l.get()),
            self._int_or_none(self.margin_r.get()),
        )
        escaped_srt = escape_for_subtitles_filter(srt_for_filter)
        filt = f"subtitles=filename='{escaped_srt}':charenc={ffmpeg_charenc(enc)}"
        if force_style:
            filt += f":force_style='{force_style}'"
        return filt

    def _build_ffmpeg_cmd(self, video: Path, srt: Path, out: Path, offset: float) -> list[str]:
        ffmpeg = which_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("'ffmpeg' was not found on the PATH.")

        overwrite_flag = "-y" if self.overwrite.get() else "-n"
        mode = self.mode.get().strip()

        if mode == "soft":
            suffix = out.suffix.lower()
            scodec = "mov_text" if suffix == ".mp4" else "srt"
            lang = (self.lang.get().strip() or "eng")

            cmd = [ffmpeg, overwrite_flag, "-i", str(video)]
            if offset != 0.0:
                cmd += ["-itsoffset", str(offset)]
            cmd += ["-i", str(srt)]
            cmd += ["-map", "0:v:0", "-map", "0:a?", "-map", "1:0"]
            cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", scodec]
            cmd += ["-metadata:s:s:0", f"language={lang}"]
            cmd += [str(out)]
            return cmd

        enc = (self.encoding.get().strip() or "auto")
        if enc == "auto":
            enc = detect_srt_encoding(srt)
            self.msg_queue.put(("log", f"Detected SRT encoding: {enc}\n"))

        srt_for_filter = srt
        temp_dir_obj = None
        if offset != 0.0:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="srt_shift_")
            tmp_dir = Path(temp_dir_obj.name)
            shifted = tmp_dir / "shifted.srt"
            shift_srt_file(srt, offset, shifted, encoding=enc)
            srt_for_filter = shifted

        filt = self._subtitles_filter(srt_for_filter, enc)

        vcodec = self.vcodec.get().strip() or "libx264"
        preset = self.preset.get().strip() or "medium"
        crf = self._int_or_default(self.crf.get(), 18)
        acodec = self.acodec.get().strip() or "copy"
        abitrate = self.abitrate.get().strip() or "192k"

        cmd = [ffmpeg, overwrite_flag, "-i", str(video)]
        cmd += ["-vf", filt]
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
        cmd += ["-c:v", vcodec]
        if preset:
            cmd += ["-preset", preset]
        cmd += video_quality_args(vcodec, crf)
        cmd += ["-c:a", acodec]
        if acodec != "copy":
            cmd += ["-b:a", abitrate]

        self._temp_dir_obj = temp_dir_obj  # type: ignore[attr-defined]

        cmd += [str(out)]
        return cmd

    def _int_or_none(self, s: str):
        s = (s or "").strip()
        if not s:
            return None
        return int(s)

    def _float_or_none(self, s: str):
        s = (s or "").strip()
        if not s:
            return None
        return float(s)

    def _int_or_default(self, s: str, default: int):
        s = (s or "").strip()
        if not s:
            return default
        return int(s)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self.log_write(payload)
                elif kind == "progress_start":
                    # Duration known: switch to a determinate bar with %
                    self.progress.stop()
                    self.progress.config(mode="determinate", maximum=100, value=0)
                    self.pct.set("0%")
                elif kind == "progress":
                    self.progress.config(value=payload)
                    self.pct.set(f"{payload:.0f}%")
                elif kind == "preview_ok":
                    self.btn_preview.config(state="normal")
                    self._show_preview(payload)
                elif kind == "preview_err":
                    self.btn_preview.config(state="normal")
                    messagebox.showerror("Preview", f"Could not generate:\n{payload}")
                elif kind == "video_info":
                    self.video_info.set(payload)
                elif kind == "error":
                    self._finish_ui()
                    messagebox.showerror("Error", payload)
                elif kind == "done":
                    self._finish_ui()
                    if payload == "ok":
                        if self._last_out and self._last_out.exists():
                            self.btn_open.config(state="normal")
                        messagebox.showinfo("Done", "Process completed successfully.")
                    else:
                        messagebox.showwarning("Cancelled", "Process cancelled.")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish_ui(self):
        self.progress.stop()
        self.pct.set("")
        self.progress.config(mode="indeterminate", value=0)
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.proc = None
        self.stop_requested = False
        try:
            t = getattr(self, "_temp_dir_obj", None)
            if t is not None:
                t.cleanup()
                self._temp_dir_obj = None
        except Exception:
            pass


if __name__ == "__main__":
    App().mainloop()
