"""Windows 当前音频输出状态查看器。

读取默认播放设备的 WASAPI mix format，直观展示采样率、位深、声道、
码率、输出源；并通过 loopback 采集显示实时电平与波形。
"""

from __future__ import annotations

import argparse
import ctypes
import math
import struct
import sys
import threading
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

warnings.filterwarnings("ignore")

import comtypes
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT
from ctypes import POINTER, c_uint32, c_uint64, c_ubyte, c_double, c_void_p, c_wchar_p
from pycaw.pycaw import AudioUtilities
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audiopolicy import IAudioSessionControl2

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


# ---------------------------------------------------------------------------
# WASAPI 常量 / 接口
# ---------------------------------------------------------------------------

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")


class IAudioCaptureClient(comtypes.IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(c_uint32), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "ReleaseBuffer",
            (["in"], c_uint32, "NumFramesRead"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetNextPacketSize",
            (["out"], POINTER(c_uint32), "pNumFramesInNextPacket"),
        ),
    )


# ---------------------------------------------------------------------------
# 格式解析
# ---------------------------------------------------------------------------

KSDATAFORMAT_SUBTYPE_PCM = "00000001-0000-0010-8000-00aa00389b71"
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = "00000003-0000-0010-8000-00aa00389b71"
KSDATAFORMAT_SUBTYPE_ALAW = "00000006-0000-0010-8000-00aa00389b71"
KSDATAFORMAT_SUBTYPE_MULAW = "00000007-0000-0010-8000-00aa00389b71"

CHANNEL_FLAGS = [
    (0x1, "FL"),
    (0x2, "FR"),
    (0x4, "FC"),
    (0x8, "LFE"),
    (0x10, "BL"),
    (0x20, "BR"),
    (0x40, "FLC"),
    (0x80, "FRC"),
    (0x100, "BC"),
    (0x200, "SL"),
    (0x400, "SR"),
    (0x800, "TC"),
    (0x1000, "TFL"),
    (0x2000, "TFC"),
    (0x4000, "TFR"),
    (0x8000, "TBL"),
    (0x10000, "TBC"),
    (0x20000, "TBR"),
]

WAVE_FORMAT_NAMES = {
    0x0001: "PCM",
    0x0002: "ADPCM",
    0x0003: "IEEE Float",
    0x0006: "A-law",
    0x0007: "µ-law",
    0xFFFE: "WAVE_FORMAT_EXTENSIBLE",
}


def _guid_str(g) -> str:
    return str(g).strip("{}").lower()


def format_name(format_tag: int, sub_format: str) -> str:
    if format_tag == 0xFFFE:
        sub = _guid_str(sub_format)
        mapping = {
            KSDATAFORMAT_SUBTYPE_PCM: "PCM",
            KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: "IEEE Float",
            KSDATAFORMAT_SUBTYPE_ALAW: "A-law",
            KSDATAFORMAT_SUBTYPE_MULAW: "µ-law",
        }
        return mapping.get(sub, f"Extensible / {sub[:8] or '?'}")
    return WAVE_FORMAT_NAMES.get(format_tag, f"0x{format_tag:04X}")


def channel_layout(mask: int, channels: int) -> str:
    if mask:
        names = [name for bit, name in CHANNEL_FLAGS if mask & bit]
        if names:
            return f"{channels}ch ({'+'.join(names)})"
    mono = {1: "Mono", 2: "Stereo"}
    label = mono.get(channels, "")
    return f"{channels}ch ({label})" if label else f"{channels}ch"


def parse_wave_format(ptr) -> dict:
    raw = ctypes.string_at(ptr, 40)
    (
        format_tag,
        channels,
        sample_rate,
        avg_bytes,
        block_align,
        bits,
        cb_size,
    ) = struct.unpack_from("<HHIIHHH", raw, 0)

    valid_bits = bits
    channel_mask = 0
    sub_format = ""

    if format_tag == 0xFFFE and cb_size >= 22 and len(raw) >= 40:
        valid_bits, channel_mask = struct.unpack_from("<HI", raw, 18)
        d1, d2, d3 = struct.unpack_from("<IHH", raw, 24)
        d4 = raw[32:40].hex()
        sub_format = f"{d1:08x}-{d2:04x}-{d3:04x}-{d4[:4]}-{d4[4:]}"

    return {
        "format_tag": format_tag,
        "format": format_name(format_tag, sub_format),
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": channel_layout(channel_mask, channels),
        "bits_per_sample": bits,
        "valid_bits_per_sample": valid_bits,
        "block_align": block_align,
        "avg_bytes_per_sec": avg_bytes,
        "bitrate_bps": avg_bytes * 8,
    }


# ---------------------------------------------------------------------------
# 采集（loopback）
# ---------------------------------------------------------------------------

class LoopbackCapture:
    """在后台线程里用 WASAPI loopback 抓取当前输出波形/电平。"""

    def __init__(self, history_seconds: float = 1.6):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._mono: deque[float] = deque(maxlen=8192)
        self._peak = 0.0
        self._error = ""
        self._history_seconds = history_seconds
        self.sample_rate = 48000
        self.channels = 2
        self.fmt: dict = {}
        self._win_cache: dict = {}

    def _hann(self, n: int):
        win = self._win_cache.get(n)
        if win is None and np is not None:
            win = np.hanning(n)
            self._win_cache[n] = win
        return win if win is not None else np.hanning(n)

    @property
    def peak(self) -> float:
        with self._lock:
            return self._peak

    @property
    def error(self) -> str:
        return self._error

    def waveform(self, points: int = 240) -> list[float]:
        with self._lock:
            data = list(self._mono)
        if not data:
            return [0.0] * points
        if len(data) >= points:
            # 均匀抽样
            step = len(data) / points
            return [data[int(i * step)] for i in range(points)]
        # 不足则左补零
        pad = points - len(data)
        return [0.0] * pad + data

    def spectrum_db(self, bands: int = 32, fft_size: int = 8192) -> list[float]:
        """返回各频带 dBFS（约 -72..0）。

        每个对数频带取带内 FFT bin 的峰值：
        - 纯音落在两中心点之间也不会被漏掉
        - 带内无 bin 时用包络插值补洞（不抬高真实有能量的频带）
        """
        if np is None:
            return [-72.0] * bands
        with self._lock:
            data = list(self._mono)[-fft_size:]
        if len(data) < 8:
            return [-72.0] * bands
        if len(data) < fft_size:
            data = [0.0] * (fft_size - len(data)) + data

        arr = np.asarray(data, dtype=np.float64)
        arr -= arr.mean()
        n = len(arr)
        win = self._hann(n)
        spec = np.fft.rfft(arr * win)
        mag = np.abs(spec) * (2.0 / max(float(win.sum()), 1e-12))
        freqs = np.fft.rfftfreq(n, d=1.0 / max(1, self.sample_rate))

        f_min = 20.0
        f_max = min(20000.0, self.sample_rate / 2.0)
        edges = np.logspace(np.log10(f_min), np.log10(f_max), bands + 1)
        centers = np.sqrt(edges[:-1] * edges[1:])

        f = freqs[1:]
        m = mag[1:]
        db_bins = 20.0 * np.log10(np.maximum(m, 1e-10))
        log_f = np.log10(np.maximum(f, 1e-3))

        out = np.full(bands, -72.0)
        for i in range(bands):
            lo, hi = edges[i], edges[i + 1]
            mask = (f >= lo) & (f < hi)
            if np.any(mask):
                # 带内峰值（纯音/共振峰不会被中心采样漏掉）
                out[i] = float(np.max(db_bins[mask]))
            else:
                # 该对数带窄于 bin 宽度：用包络插值补洞
                out[i] = float(np.interp(math.log10(centers[i]), log_f, db_bins))

        np.clip(out, -72.0, 0.0, out=out)
        return out.tolist()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        comtypes.CoInitialize()
        try:
            device = AudioUtilities.GetSpeakers()
            if device is None:
                self._error = "未找到默认播放设备"
                return
            client = device._dev.Activate(
                IAudioClient._iid_, CLSCTX_ALL, None
            ).QueryInterface(IAudioClient)
            fmt = client.GetMixFormat()
            wf = fmt.contents
            self.sample_rate = int(wf.nSamplesPerSec)
            self.channels = max(1, int(wf.nChannels))
            bits = int(wf.wBitsPerSample)
            block = int(wf.nBlockAlign)
            # 保存实时 mix format（人耳听到的引擎输出格式）
            self.fmt = parse_wave_format(fmt)

            # 约 50ms 缓冲
            hns = int(0.05 * 10_000_000)
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                hns,
                0,
                fmt,
                None,
            )
            capture = client.GetService(IID_IAudioCaptureClient)
            capture = capture.QueryInterface(IAudioCaptureClient)
            client.Start()

            while not self._stop.is_set():
                nxt = capture.GetNextPacketSize()
                while nxt:
                    data_ptr, frames, flags, _devpos, _qpc = capture.GetBuffer()
                    frames = int(frames)
                    if frames:
                        silent = bool(flags & AUDCLNT_BUFFERFLAGS_SILENT)
                        if silent:
                            samples = [0.0] * frames
                        else:
                            samples = self._decode(
                                data_ptr, frames, self.channels, bits, block
                            )
                        self._push(samples)
                    capture.ReleaseBuffer(frames)
                    nxt = capture.GetNextPacketSize()
                time.sleep(0.008)

            client.Stop()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            comtypes.CoUninitialize()

    @staticmethod
    def _decode(data_ptr, frames: int, channels: int, bits: int, block: int) -> list[float]:
        nbytes = frames * block
        raw = ctypes.string_at(data_ptr, nbytes)
        out: list[float] = []
        if bits == 32:
            # float 或 int32：按 float 解（mix format 常见）
            count = frames * channels
            vals = struct.unpack(f"<{count}f", raw[: count * 4])
            for i in range(frames):
                s = 0.0
                for c in range(channels):
                    s += vals[i * channels + c]
                out.append(s / channels)
        elif bits == 16:
            count = frames * channels
            vals = struct.unpack(f"<{count}h", raw[: count * 2])
            for i in range(frames):
                s = 0.0
                for c in range(channels):
                    s += vals[i * channels + c] / 32768.0
                out.append(s / channels)
        else:
            # 兜底：按字节幅度
            for i in range(frames):
                chunk = raw[i * block : (i + 1) * block]
                if not chunk:
                    out.append(0.0)
                    continue
                s = sum(b - 128 for b in chunk) / (128.0 * max(1, len(chunk)))
                out.append(s)
        return out

    def _push(self, samples: list[float]) -> None:
        peak = 0.0
        mono = []
        for v in samples:
            a = abs(v)
            if a > peak:
                peak = a
            mono.append(max(-1.0, min(1.0, v)))
        with self._lock:
            self._mono.extend(mono)
            # 轻微衰减，让电平条在静音后回落
            self._peak = peak if peak >= self._peak else max(peak, self._peak * 0.88)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

@dataclass
class AudioSnapshot:
    ok: bool = True
    error: str = ""
    device_name: str = ""
    fmt: dict = field(default_factory=dict)
    sessions: list = field(default_factory=list)
    peak: float = 0.0
    timestamp: float = 0.0


_STATE_NAMES = {0: "活跃", 1: "非活跃", 2: "过期"}


def collect_snapshot() -> AudioSnapshot:
    snap = AudioSnapshot(timestamp=time.time())
    try:
        device = AudioUtilities.GetSpeakers()
        if device is None:
            snap.ok = False
            snap.error = "未找到默认播放设备"
            return snap

        snap.device_name = device.FriendlyName or "（未知设备）"

        client = device._dev.Activate(
            IAudioClient._iid_, CLSCTX_ALL, None
        ).QueryInterface(IAudioClient)
        snap.fmt = parse_wave_format(client.GetMixFormat())

        # 会话
        rows = []
        try:
            enum = device.AudioSessionManager.GetSessionEnumerator()
            for i in range(enum.GetCount()):
                try:
                    ctrl = enum.GetSession(i)
                    ctrl2 = ctrl.QueryInterface(IAudioSessionControl2)
                    pid = int(ctrl2.GetProcessId() or 0)
                    state = ctrl.GetState()
                    is_system = ctrl2.IsSystemSoundsSession() == 0
                    name = ""
                    if pid and psutil is not None:
                        try:
                            name = psutil.Process(pid).name()
                        except Exception:
                            name = f"PID {pid}"
                    elif is_system:
                        name = "系统声音"
                    elif pid:
                        name = f"PID {pid}"
                    else:
                        name = "系统/未知"
                    rows.append(
                        {
                            "name": name,
                            "pid": pid,
                            "state": state,
                            "system": is_system,
                        }
                    )
                except Exception:
                    continue
        except Exception:
            pass
        rows.sort(key=lambda r: (not r["system"], r["name"].lower()))
        snap.sessions = rows
    except Exception as exc:
        snap.ok = False
        snap.error = f"{type(exc).__name__}: {exc}"
    return snap


def format_bitrate(bps: int) -> str:
    if bps <= 0:
        return "—"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    return f"{bps / 1000:.0f} kbps"


def render_text(snap: AudioSnapshot, peak: float = 0.0) -> str:
    if not snap.ok:
        return f"读取失败：{snap.error}"
    f = snap.fmt
    lines = [
        "Windows 当前音频输出",
        "-" * 44,
        f"输出设备  {snap.device_name}",
        "",
        "实际输出格式",
        f"  位深    {f.get('bits_per_sample', 0)} bit",
        f"  码率    {format_bitrate(int(f.get('bitrate_bps', 0)))}",
        f"  声道    {f.get('channel_layout', '—')}",
        f"  采样率  {f.get('sample_rate', 0):,} Hz",
        "",
        f"电平      {peak * 100:.0f}%",
        "",
        "活动会话",
    ]
    if not snap.sessions:
        lines.append("  （无）")
    else:
        for s in snap.sessions:
            mark = "●" if s["state"] == 0 else "○"
            tag = " [系统]" if s["system"] else ""
            lines.append(
                f"  {mark} {s['name']}{tag}  PID {s['pid']} · "
                f"{_STATE_NAMES.get(s['state'], s['state'])}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GUI — Windows 资源管理器风格
# ---------------------------------------------------------------------------

def _enable_dpi() -> None:
    """启用 Per-Monitor DPI，避免文字被位图拉伸发虚。"""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _enable_hires_timer() -> None:
    """把 Windows 定时器精度提到 1ms，否则 after() 约 15ms 才醒，上不了 120Hz。"""
    try:
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
    except Exception:
        pass


def run_gui(refresh_ms: int = 16) -> None:
    _enable_dpi()
    _enable_hires_timer()
    import tkinter as tk
    from tkinter import ttk
    from tkinter import font as tkfont

    # 资源管理器浅色
    BG = "#f0f0f0"
    CARD = "#ffffff"
    BORDER = "#e5e5e5"
    FG = "#1b1b1b"
    DIM = "#5f6368"
    ACCENT = "#0067c0"
    BAR_BG = "#e8e8e8"
    BAR_FG = "#0067c0"
    BAR_HIGH = "#c42b1c"
    GRID = "#d0d0d0"
    WAVE = "#0067c0"
    WAVE_FILL = "#cfe4f7"
    SEL = "#e8f1fb"

    root = tk.Tk()
    root.title("Windows 音频输出状态")
    root.configure(bg=BG)
    root.resizable(False, False)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        try:
            style.theme_use("clam")
        except Exception:
            pass

    base = tkfont.nametofont("TkDefaultFont")
    # 稍大字号 + Segoe UI，配合 DPI 感知提高清晰度
    ui = tkfont.Font(family="Segoe UI", size=10)
    ui_bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    ui_small = tkfont.Font(family="Segoe UI", size=9)
    ui_mono = tkfont.Font(family="Consolas", size=10)
    title_font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
    LAB_W = 8  # 标签列固定宽度（字符），保证两栏对齐

    main = tk.Frame(root, bg=BG)
    main.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

    # ── 标题栏 ──────────────────────────────────────────────
    tk.Label(
        main,
        text="Windows 当前音频输出",
        font=title_font,
        bg=BG,
        fg=FG,
        anchor="w",
    ).pack(fill=tk.X)

    status = tk.StringVar(value="正在读取…")
    tk.Label(
        main, textvariable=status, font=ui_small, bg=BG, fg=DIM, anchor="w"
    ).pack(fill=tk.X, pady=(0, 4))

    def section(title: str) -> tk.Frame:
        """资源管理器式分组：白底卡片 + 顶部细标题。"""
        wrap = tk.Frame(main, bg=BORDER)
        wrap.pack(fill=tk.X, pady=(0, 4))
        inner = tk.Frame(wrap, bg=CARD)
        inner.pack(fill=tk.BOTH, padx=1, pady=1)
        tk.Label(
            inner,
            text=title,
            font=ui_bold,
            bg=CARD,
            fg=ACCENT,
            anchor="w",
        ).pack(fill=tk.X, padx=8, pady=(3, 1))
        body = tk.Frame(inner, bg=CARD)
        body.pack(fill=tk.X, padx=8, pady=(0, 4))
        return body

    def _field_label(parent: tk.Frame, text: str, indent: int = 0) -> None:
        """标签定宽对齐；indent 为左缩进（英文字符数），用于微调个别标签位置。"""
        display = (" " * indent) + text if indent else text
        tk.Label(
            parent,
            text=display,
            font=ui,
            bg=CARD,
            fg=DIM,
            anchor="w",
            width=8,
        ).pack(side=tk.LEFT, padx=(0, 10))

    def add_field(parent: tk.Frame, label: str, value_font=None):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill=tk.X, pady=0)
        _field_label(row, label)
        var = tk.StringVar(value="—")
        tk.Label(
            row,
            textvariable=var,
            font=value_font or ui,
            bg=CARD,
            fg=FG,
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        return var

    def add_field_pair(parent: tk.Frame, left: tuple, right: tuple):
        """一行两列，标签列等宽对齐。tuple: (label, value_font, indent?)"""
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill=tk.X, pady=0)
        vars_out = []
        for item in (left, right):
            label, value_font = item[0], item[1]
            indent = item[2] if len(item) > 2 else 0
            cell = tk.Frame(row, bg=CARD)
            cell.pack(side=tk.LEFT, fill=tk.X, expand=True)
            _field_label(cell, label, indent=indent)
            var = tk.StringVar(value="—")
            tk.Label(
                cell,
                textvariable=var,
                font=value_font or ui,
                bg=CARD,
                fg=FG,
                anchor="w",
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)
            vars_out.append(var)
        return vars_out[0], vars_out[1]

    # ── 输出设备 ────────────────────────────────────────────
    card_dev = section("输出设备")
    v_device = add_field(card_dev, "设备", ui_bold)

    # ── 实际输出格式（双栏，标签定宽对齐）──────────────────
    card_fmt = section("实际输出格式")
    v_depth, v_channels = add_field_pair(
        card_fmt, ("位深", None), ("声道", None, 5)
    )
    v_bitrate, v_rate = add_field_pair(
        card_fmt, ("码率", None), ("采样率", None)
    )

    # ── 电平 / 频谱（声卡 LED + DAW 频谱）─────────────────
    card_wave = section("电平 / 频谱")

    level_row = tk.Frame(card_wave, bg=CARD)
    level_row.pack(fill=tk.X, pady=(0, 1))
    _field_label(level_row, "电平")
    led_canvas = tk.Canvas(
        level_row, height=18, bg="#111418", highlightthickness=1,
        highlightbackground=BORDER, bd=0,
    )
    led_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
    v_level = tk.StringVar(value="-∞ dB")
    tk.Label(
        level_row, textvariable=v_level, font=ui_mono, bg=CARD, fg=FG, width=8
    ).pack(side=tk.RIGHT)

    spec_canvas = tk.Canvas(
        card_wave,
        height=130,
        bg="#1a1d21",
        highlightthickness=1,
        highlightbackground=BORDER,
        bd=0,
    )
    spec_canvas.pack(fill=tk.X, pady=(1, 0))

    # ── 音频会话（可滚动列表，显示全部会话）────────────────
    card_sess = section("音频会话")
    sess_wrap = tk.Frame(card_sess, bg=CARD)
    sess_wrap.pack(fill=tk.X)
    sess_listbox = tk.Listbox(
        sess_wrap,
        height=5,
        font=ui,
        bg=CARD,
        fg=FG,
        selectbackground=SEL,
        selectforeground=FG,
        highlightthickness=1,
        highlightbackground=BORDER,
        relief=tk.FLAT,
        activestyle="none",
        exportselection=False,
    )
    sess_scroll = tk.Scrollbar(sess_wrap, orient=tk.VERTICAL, command=sess_listbox.yview)
    sess_listbox.configure(yscrollcommand=sess_scroll.set)
    sess_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sess_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ── 采集 ────────────────────────────────────────────────
    capture = LoopbackCapture()
    capture.start()

    # LED / 频谱绘制状态
    led_items = {"rects": [], "peak": None, "built": False, "key": None}
    spec_items = {"bars": [], "built": False, "n": 0, "key": None}
    peak_hold = {"val": 0.0, "t": 0.0}

    # 常显色段（熄灭也看得出绿/黄/红），点亮时更亮
    LED_OFF_G = "#14532d"
    LED_OFF_Y = "#713f12"
    LED_OFF_R = "#7f1d1d"
    LED_ON_G = "#22c55e"
    LED_ON_Y = "#facc15"
    LED_ON_R = "#f87171"
    SPEC_FILL = "#3b82f6"
    SPEC_TOP = "#67e8f9"
    SPEC_DIM = "#1e3a5f"

    smooth = {
        "led": 0.0,
        "peak": 0.0,
        "spec": [-72.0] * 32,
    }

    def _zone(ratio: float) -> str:
        if ratio >= 0.92:
            return "R"
        if ratio >= 0.72:
            return "Y"
        return "G"

    def draw_led(level: float) -> None:
        """声卡式绿/黄/红分段 LED 电平条（attack 快 / release 慢）。"""
        w = led_canvas.winfo_width() or 300
        h = led_canvas.winfo_height() or 18
        segs = 28
        gap = 1
        seg_w = max(3, (w - gap * (segs - 1)) // segs)

        # 平滑：快上慢下
        if level > smooth["led"]:
            smooth["led"] = smooth["led"] * 0.4 + level * 0.6
        else:
            smooth["led"] = smooth["led"] * 0.9 + level * 0.1
        lvl = max(level, smooth["led"])

        if lvl <= 0.00001:
            db = -60.0
        else:
            db = 20.0 * math.log10(max(lvl, 1e-6))
        db = max(-60.0, min(0.0, db))
        lit = int(round((db + 60.0) / 60.0 * segs))

        now = time.time()
        if lvl >= peak_hold["val"]:
            peak_hold["val"] = lvl
            peak_hold["t"] = now
        elif now - peak_hold["t"] > 1.0:
            peak_hold["val"] = max(lvl, peak_hold["val"] * 0.94)
        ph_db = max(-60.0, min(0.0, 20.0 * math.log10(max(peak_hold["val"], 1e-6))))
        ph_idx = int(round((ph_db + 60.0) / 60.0 * segs))

        # 宽度变化时重建
        key = (segs, w, h, seg_w)
        if led_items.get("key") != key:
            led_canvas.delete("all")
            led_items["rects"] = []
            led_items["colors"] = []
            for i in range(segs):
                x = i * (seg_w + gap)
                r = led_canvas.create_rectangle(
                    x, 2, x + seg_w, h - 2, width=0, fill=LED_OFF_G
                )
                led_items["rects"].append(r)
                led_items["colors"].append(LED_OFF_G)
            led_items["peak"] = led_canvas.create_rectangle(
                0, 1, 2, h - 1, width=0, fill="#f8fafc"
            )
            led_items["key"] = key
            led_items["built"] = True

        # 只在颜色变化时 itemconfig，降低每帧开销
        colors = led_items["colors"]
        rects = led_items["rects"]
        zone_t = segs
        y_off = (segs * 72) // 100  # 72%
        r_off = (segs * 92) // 100
        for i in range(segs):
            if i < lit:
                if i >= r_off:
                    color = LED_ON_R
                elif i >= y_off:
                    color = LED_ON_Y
                else:
                    color = LED_ON_G
            else:
                if i >= r_off:
                    color = LED_OFF_R
                elif i >= y_off:
                    color = LED_OFF_Y
                else:
                    color = LED_OFF_G
            if colors[i] != color:
                colors[i] = color
                led_canvas.itemconfig(rects[i], fill=color)

        px = max(0, min(segs - 1, ph_idx - 1)) * (seg_w + gap)
        led_canvas.coords(led_items["peak"], px, 1, px + 2, h - 1)

        if lvl <= 0.00001:
            v_level.set("-∞ dB")
        else:
            v_level.set(f"{db:.1f} dB")

    def draw_spectrum(bands_db: list[float]) -> None:
        """DAW 风格频谱：对数频率 × 响度柱。"""
        w = spec_canvas.winfo_width() or 460
        h = spec_canvas.winfo_height() or 130
        n = len(bands_db) or 32
        pad = 2
        axis_w = 34  # 左侧响度刻度留白，避免被第一根频带遮挡
        label_h = 14
        usable_h = max(20, h - pad * 2 - label_h)
        base_y = pad + usable_h
        plot_x0 = pad + axis_w
        plot_w = max(40, w - plot_x0 - pad)
        step = plot_w / max(1, n)
        bar_w = max(2.0, step - 1.0)

        sp = smooth["spec"]
        if len(sp) != n:
            sp = smooth["spec"] = [-72.0] * n
        for i in range(n):
            db = bands_db[i]
            prev = sp[i]
            if db > prev:
                sp[i] = prev * 0.4 + db * 0.6
            else:
                sp[i] = prev * 0.9 + db * 0.1

        key = (n, int(w), int(h), int(bar_w))
        if spec_items.get("key") != key:
            spec_canvas.delete("all")
            fmin, fmax = 20.0, 20000.0
            log_span = math.log10(fmax) - math.log10(fmin)

            def freq_to_x(f: float) -> float:
                t = (math.log10(f) - math.log10(fmin)) / log_span
                return plot_x0 + t * plot_w

            for frac, label in (
                (0.1667, "-10"), (0.3333, "-20"), (0.5, "-30"),
                (0.6667, "-40"), (0.8333, "-50"),
            ):
                y = int(pad + usable_h * frac)
                spec_canvas.create_line(plot_x0, y, w - pad, y, fill="#2f3540", tags="grid")
                spec_canvas.create_text(
                    axis_w - 4, y, text=label, anchor="e", fill="#666c75",
                    font=("Segoe UI", 6), tags="grid",
                )
            marks = (
                (20, "20"), (50, "50"), (100, "100"), (200, "200"),
                (500, "500"), (1000, "1k"), (2000, "2k"), (5000, "5k"),
                (10000, "10k"), (20000, "20k"),
            )
            for f, lab in marks:
                x = freq_to_x(f)
                spec_canvas.create_line(x, pad, x, base_y, fill="#2a3140", tags="grid")
                anchor = "w" if f == 20 else ("e" if f == 20000 else "center")
                spec_canvas.create_text(
                    x, h - label_h / 2 - 1, text=lab, anchor=anchor,
                    fill="#8b919a", font=("Segoe UI", 6), tags="grid",
                )
            # 每条频带一个矩形（可靠、可单独改高度）
            spec_items["bars"] = []
            spec_items["colors"] = []
            for i in range(n):
                x = plot_x0 + i * step
                r = spec_canvas.create_rectangle(
                    x, base_y - 1, x + bar_w, base_y,
                    width=0, fill=SPEC_FILL,
                )
                spec_items["bars"].append(r)
                spec_items["colors"].append(SPEC_FILL)
            spec_items["key"] = key
            spec_items["built"] = True
            spec_items["n"] = n

        bars = spec_items["bars"]
        colors = spec_items["colors"]
        for i in range(n):
            db = sp[i]
            t = 0.0 if db <= -60.0 else min(1.0, (db + 60.0) / 60.0)
            bh = 1.5 if t <= 0.0 else t * usable_h
            x = plot_x0 + i * step
            y0 = base_y - bh
            spec_canvas.coords(bars[i], x, y0, x + bar_w, base_y)
            if t > 0.75:
                color = SPEC_TOP
            elif t > 0.08:
                color = SPEC_FILL
            else:
                color = SPEC_DIM
            if colors[i] != color:
                colors[i] = color
                spec_canvas.itemconfig(bars[i], fill=color)

    def apply_snapshot(snap: AudioSnapshot) -> None:
        ts = time.strftime("%H:%M:%S", time.localtime(snap.timestamp))
        if not snap.ok:
            status.set(f"读取失败  {ts}  ·  {snap.error}")
            return
        # 以采集线程解析的实时 mix format 为准（= 你此刻听到的输出）
        live_fmt = getattr(capture, "fmt", None)
        if live_fmt:
            snap.fmt = live_fmt
        f = snap.fmt or {}
        status.set(f"实时 · {ts}")
        v_device.set(snap.device_name)
        bits = f.get("bits_per_sample", 0)
        valid = f.get("valid_bits_per_sample", 0)
        v_depth.set(
            f"{bits} bit" if not (valid and valid != bits) else f"{bits} bit（有效 {valid}）"
        )
        v_channels.set(f.get("channel_layout", "—"))
        v_bitrate.set(format_bitrate(int(f.get("bitrate_bps", 0))))
        v_rate.set(f"{f.get('sample_rate', 0):,} Hz")

        # 会话列表：显示全部，可滚动
        lines = []
        for s in snap.sessions:
            mark = "●" if s["state"] == 0 else "○"
            tag = "  [系统]" if s["system"] else ""
            state = _STATE_NAMES.get(s["state"], str(s["state"]))
            lines.append(f"{mark} {s['name']}{tag}  ·  PID {s['pid']}  ·  {state}")

        # 尽量保持滚动位置
        yview = sess_listbox.yview()
        sess_listbox.delete(0, tk.END)
        if lines:
            for line in lines:
                sess_listbox.insert(tk.END, line)
        else:
            sess_listbox.insert(tk.END, "（当前无音频会话）")
        try:
            sess_listbox.yview_moveto(yview[0])
        except Exception:
            pass

    # 缓存 meter，避免每帧 Activate COM
    meter_ref = {"obj": None}

    def meter_peak() -> float:
        try:
            from pycaw.api.endpointvolume import IAudioMeterInformation

            if meter_ref["obj"] is None:
                dev = AudioUtilities.GetSpeakers()
                meter_ref["obj"] = dev._dev.Activate(
                    IAudioMeterInformation._iid_, CLSCTX_ALL, None
                ).QueryInterface(IAudioMeterInformation)
            return float(meter_ref["obj"].GetPeakValue())
        except Exception:
            meter_ref["obj"] = None
            return 0.0

    # 快路径：电平 + 频谱（目标 ≥120 FPS）
    # 慢路径：完整设备/格式/会话快照
    wave_interval = 7
    snap_interval = 250  # 格式/会话较频繁刷新，贴近“实时”
    last_snap = 0.0

    def tick_fast() -> None:
        nonlocal last_snap
        try:
            peak = max(capture.peak, meter_peak())
            draw_led(peak)
            draw_spectrum(capture.spectrum_db(32))

            now = time.time()
            if now - last_snap >= snap_interval / 1000.0:
                last_snap = now
                snap = collect_snapshot()
                snap.peak = peak
                apply_snapshot(snap)
        except Exception as exc:
            status.set(f"刷新异常：{type(exc).__name__}: {exc}")
        root.after(wave_interval, tick_fast)

    def _abs_bottom(widget, base_y: int = 0) -> int:
        y = base_y + widget.winfo_y()
        bottom = y + widget.winfo_height()
        for child in widget.winfo_children():
            bottom = max(bottom, _abs_bottom(child, y))
        return bottom

    def fit_window() -> None:
        """宽度固定 560，高度按内容刚好容纳。"""
        root.geometry("620x900")
        root.update_idletasks()
        root.update()
        # 先填充一次真实内容（会话/格式），再量高度
        try:
            apply_snapshot(collect_snapshot())
        except Exception:
            pass
        root.update_idletasks()

        deco = max(31, root.winfo_rooty() - root.winfo_y())
        # 不计入 main 被 expand 撑大的自身高度，只看子控件底边
        content_bottom = 0
        my = main.winfo_y()
        for child in main.winfo_children():
            content_bottom = max(content_bottom, _abs_bottom(child, my))
        req_h = content_bottom + deco + 10
        root.geometry(f"620x{req_h}")
        root.minsize(620, req_h)
        root.maxsize(620, req_h)

    fit_window()

    root.after(30, tick_fast)
    try:
        root.mainloop()
    finally:
        capture.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="显示 Windows 当前输出音频的实际格式")
    parser.add_argument("--text", action="store_true", help="文本模式打印一次")
    parser.add_argument("--watch", action="store_true", help="文本模式持续刷新")
    parser.add_argument(
        "--interval", type=float, default=500, help="刷新间隔毫秒"
    )
    args = parser.parse_args()

    if args.text:
        cap = LoopbackCapture()
        cap.start()
        interval = max(0.2, args.interval / 1000.0)
        try:
            if args.watch:
                while True:
                    print("\033[2J\033[H", end="")
                    print(render_text(collect_snapshot(), cap.peak))
                    sys.stdout.flush()
                    time.sleep(interval)
            else:
                time.sleep(0.35)
                print(render_text(collect_snapshot(), cap.peak))
        except KeyboardInterrupt:
            pass
        finally:
            cap.stop()
        return 0

    run_gui(refresh_ms=16)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
