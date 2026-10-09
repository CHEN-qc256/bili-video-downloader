#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站视频下载器 —— 粘贴地址就能下，全图形界面。

为什么是 tkinter：本机 Python 3.13 自带 tkinter 8.6，yt-dlp / mutagen / Pillow 都已在
site-packages 里，所以这个工具**零额外依赖**，不用 pip install，复制走就能跑。

内建了在这台机器上实测出来的全部坑（详见 download-anything 技能的 SKILL.md §6）：

* B 站无 cookie 必回 HTTP 412 → 自动准备游客 cookie（cookies.txt），不需要浏览器配合。
  之所以不用 --cookies-from-browser：Edge 常驻时会独占锁住 cookie 数据库，必失败。
* 画质上限有两个来源，界面上分开说明：
    - UP 主本身只传到某档（`-F` 里没有任何会员提示），这是硬上限；
    - "1080P 高码率"是大会员画质（yt-dlp 会提示 premium member），游客拿不到。
* 编码可选 H.264 / AV1 / HEVC：实测同一个 1080P，AV1 62.7 MB vs H.264 94.4 MB。

用法：  python bili_dl_gui.py      （或双击同目录的 启动.bat）
"""

from __future__ import annotations

import datetime
import http.cookiejar
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
except ImportError:  # 交给 main() 弹窗说明
    yt_dlp = None

try:
    from yt_dlp.utils import DownloadCancelled
except Exception:  # pragma: no cover - 版本差异兜底
    class DownloadCancelled(Exception):
        pass

try:
    from PIL import Image, ImageTk
except ImportError:  # 封面预览降级，不影响下载
    Image = ImageTk = None


# --------------------------------------------------------------------- 常量

APP_TITLE = "B站视频下载器"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)

SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = SCRIPT_DIR / "settings.json"
LOG_FILE = SCRIPT_DIR / "last_run.log"
DEFAULT_COOKIE = SCRIPT_DIR / "cookies.txt"

# 封面预览的固定占位尺寸。这个框**必须**是固定大小的：封面图一显示出来，
# ① 区就会长高 90 像素（占位文字两行 → 图片 169 像素），而窗口高度是启动时定死的，
# grid 只好把没权重的行挤掉——「② 开始下载」那一整行直接 mapped=False 消失。
THUMB_W, THUMB_H = 300, 170

# 勾了「分开下载」时进度条的分配：第一遍（只要画面）占前 62%，第二遍（只要声音）占剩下 38%。
# 因为两遍是各下各的，没有一个总的字节数可以拿来算百分比，只能按阶段切。
SPLIT_VIDEO_END = 62.0

# 画质预设。kind=video 走画面，kind=audio 只取音轨。
QUALITY_PRESETS = [
    {"label": "最高画质（不设上限）",        "kind": "video", "height": None},
    {"label": "2160P / 4K",                  "kind": "video", "height": 2160},
    {"label": "1080P",                       "kind": "video", "height": 1080},
    {"label": "720P",                        "kind": "video", "height": 720},
    {"label": "480P",                        "kind": "video", "height": 480},
    {"label": "360P",                        "kind": "video", "height": 360},
    {"label": "仅音频 · 原生 m4a（不转码）",  "kind": "audio", "acodec": "best"},
    {"label": "仅音频 · MP3 320k",           "kind": "audio", "acodec": "mp3"},
]

CODEC_PRESETS = [
    {"label": "H.264（兼容性最好）",  "key": "avc",  "re": "^avc"},
    {"label": "AV1（同画质体积最小）", "key": "av01", "re": "^av01"},
    {"label": "HEVC / H.265（体积小）", "key": "hev", "re": "^(hev|hvc1)"},
]

def _default_outdir() -> Path:
    """默认保存位置：优先「下载」文件夹，其次主目录（不写死任何盘符）。"""
    for cand in (Path.home() / "Downloads", Path.home() / "下载"):
        if cand.is_dir():
            return cand / "B站视频"
    return Path.home() / "B站视频"


DEFAULT_SETTINGS = {
    "outdir": str(_default_outdir()),
    "quality": "最高画质（不设上限）",
    "codec": "H.264（兼容性最好）",
    "embed": True,
    "playlist": False,
    "bv_in_name": False,
    "replace_same_name": True,
    "split_av": False,
    "cookiefile": str(DEFAULT_COOKIE),
    "audio_only_dir": "",
}

# 分享文案里的 URL、以及裸 BV 号
RE_URL = re.compile(r"https?://[^\s\u4e00-\u9fff，。；、）)】\]]+", re.I)
RE_BV = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
RE_SHORT = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.I)


# --------------------------------------------------------------------- 小工具

def human_duration(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def human_size(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_speed(n) -> str:
    return f"{human_size(n)}/s" if n else ""


def scripts_dirs() -> list[Path]:
    """pip 装的命令行工具在这台机器上不在 PATH 里，得自己找。"""
    dirs: list[Path] = []
    try:
        import sysconfig
        p = sysconfig.get_path("scripts")
        if p:
            dirs.append(Path(p))
    except Exception:
        pass
    dirs.append(Path(sys.executable).parent)
    la = os.environ.get("LOCALAPPDATA")
    if la:
        dirs += sorted(
            (Path(la) / "Packages").glob(
                "PythonSoftwareFoundation.Python.*/LocalCache/local-packages/Python*/Scripts"
            )
        )
    dirs.append(Path.home() / "AppData" / "Roaming" / "Python" / "Python313" / "Scripts")
    seen, out = set(), []
    for d in dirs:
        if d and str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def find_ffmpeg() -> Path | None:
    """合并视频/音频、嵌封面都要 ffmpeg，而它不在 PATH 上。"""
    which = shutil.which("ffmpeg")
    if which:
        return Path(which)
    for d in scripts_dirs():
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = d / name
            if p.exists():
                return p
    return None


def build_video_format(height: int | None, codec_re: str) -> str:
    """优先目标编码 + mp4/AAC，退而求其次逐级放宽（保证一定能选到流）。"""
    h = f"[height<={height}]" if height else ""
    return (
        f"bv{h}[vcodec~='{codec_re}'][ext=mp4]+ba[acodec~='^(mp4a)'][ext=m4a]"
        f"/bv{h}[vcodec~='{codec_re}']+ba"
        f"/bv{h}[ext=mp4]+ba[ext=m4a]"
        f"/bv{h}+ba/b{h}"
    )


def build_video_only_format(height: int | None, codec_re: str) -> str:
    """只要画面、不要声音（分开下载时用）。

    刻意**不**留「+ba」和「/b」兜底：那两条会把音轨一起拉下来，就不是「分开」了。
    B 站所有清晰度都是 DASH 分离流，`bv` 一定选得到；万一真选不到，报错也比
    悄悄给你一个带声音的文件好。
    """
    h = f"[height<={height}]" if height else ""
    return (
        f"bv{h}[vcodec~='{codec_re}'][ext=mp4]"
        f"/bv{h}[vcodec~='{codec_re}']"
        f"/bv{h}[ext=mp4]"
        f"/bv{h}"
    )


def preset_by_label(label: str) -> dict:
    for p in QUALITY_PRESETS:
        if p["label"] == label:
            return p
    return QUALITY_PRESETS[0]


def codec_by_label(label: str) -> dict:
    for c in CODEC_PRESETS:
        if c["label"] == label:
            return c
    return CODEC_PRESETS[0]


def find_produced_file(infos: list, outdir: Path, ext: str) -> Path | None:
    """把后处理器改过扩展名的成品找回来（.mkv→.mp4、.m4a→.mp3 都发生过）。

    优先「同名 + 目标扩展名」，其次「同名 + 目标扩展名里最新的」，最后才退到
    「目录里最新的目标扩展名文件」—— 前面的路正常都能命中，最后一条只是兜底。
    """
    stems: list[str] = []
    for info in infos:
        if not info:
            continue
        for d in info.get("requested_downloads") or []:
            p = d.get("filepath")
            if p:
                stems.append(Path(p).stem)
        if info.get("filepath"):
            stems.append(Path(info["filepath"]).stem)

    want = "." + ext.lower().lstrip(".")
    for stem in stems:
        cand = outdir / f"{stem}{want}"
        if cand.exists():
            return cand
    for stem in stems:
        same = [p for p in outdir.glob(f"{stem}.*") if p.suffix.lower() == want]
        if same:
            return max(same, key=lambda p: p.stat().st_mtime)
    any_ext = [p for p in outdir.glob("*") if p.suffix.lower() == want]
    return max(any_ext, key=lambda p: p.stat().st_mtime) if any_ext else None


def extract_targets(text: str) -> list[str]:
    """从任意粘贴内容里挑出视频地址。

    支持：完整 URL（含 ?spm_id_from=... 这类尾巴）、b23.tv 短链、裸 BV 号，
    以及 B 站 App 的分享文案（【标题】 https://b23.tv/xxx）。
    """
    text = text or ""
    found: list[str] = []

    def add(u: str) -> None:
        u = u.rstrip(".,;，。；、)]）】\"'")
        if u and u not in found:
            found.append(u)

    for m in RE_URL.finditer(text):
        add(m.group(0))
    for m in RE_SHORT.finditer(text):
        add(m.group(0))
    for m in RE_BV.finditer(text):
        bv = m.group(0)
        if not any(bv in u for u in found):
            add(f"https://www.bilibili.com/video/{bv}")
    return found


def resolve_short_url(url: str, cookie_file: Path | None) -> str:
    """b23.tv 短链换成真实地址（yt-dlp 也能跟，但换掉后日志更好读）。"""
    if not RE_SHORT.match(url):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
        return final or url
    except Exception:
        return url


def describe_formats(info: dict) -> tuple[int, str]:
    """返回 (最高可用高度, 人类可读的画质说明)。"""
    heights = sorted(
        {
            f.get("height")
            for f in (info.get("formats") or [])
            if f.get("height") and f.get("vcodec") not in (None, "none")
        },
        reverse=True,
    )
    if not heights:
        return 0, "无视频流"
    top = heights[0]
    txt = f"最高 {top}P"
    if len(heights) > 1:
        txt += "（" + " / ".join(f"{h}P" for h in heights[:4]) + "）"
    return top, txt


# --------------------------------------------------------------------- Cookie

COOKIE_LOGIN_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


def parse_cookie_text(text: str) -> dict[str, str]:
    """把用户从浏览器复制出来的东西变成 {名字: 值}。

    两种粘法都得认：整条 Cookie 请求头（`a=1; b=2; …`），以及只复制了 SESSDATA 的裸值
    ——后者是 DevTools 里最常见的复制方式。
    """
    text = (text or "").strip()
    if not text:
        return {}
    pairs: dict[str, str] = {}
    if "=" in text:
        for chunk in re.split(r"[;\r\n]+", text):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            k, _, v = chunk.partition("=")
            k, v = k.strip(), v.strip().strip('"')
            if k:
                pairs[k] = v
    if not pairs:  # 一个 `=` 都没有 → 整段就是一个裸值，当 SESSDATA
        pairs["SESSDATA"] = text
    return pairs


def write_netscape_cookie_file(path: Path, pairs: dict[str, str],
                               domain: str = ".bilibili.com") -> Path:
    """写成 yt-dlp 能吃的 Netscape cookie 文件。

    字段顺序是「域 / 是否含子域 / 路径 / 是否仅 https / 过期时间 / 名字 / 值」，制表符分隔。
    过期时间给到 2038 年——yt-dlp 加载时本来就 ignore_expires=True，这里只是别让它看着像过期的。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 注释行只用 ASCII：stdlib 的 MozillaCookieJar.load() 是按系统默认编码（中文
    # Windows 上是 GBK）读文件的，注释里放中文会直接 UnicodeDecodeError，把整个
    # cookie 文件读废掉。yt-dlp 自己按 utf-8 读所以没事，但没理由给别人埋雷。
    lines = ["# Netscape HTTP Cookie File", "# Generated by bili_dl_gui. Do not edit.", ""]
    for name, value in pairs.items():
        if not name or "\t" in name or "\t" in value:
            continue
        lines.append("\t".join([domain, "TRUE", "/", "TRUE", "2147483647", name, value]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def probe_login(cookie_file: Path | None) -> tuple[bool, str]:
    """问 B 站「我是谁」，用来判断这份 cookie 到底有没有登录态。

    `nav` 接口不需要 WBI 签名，未登录时直接回 `code=-101 账号未登录`，
    所以这是最省事、最确定的登录校验——比翻 cookie 里有没有 SESSDATA 靠谱：
    SESSDATA 过期了照样在文件里躺着。
    """
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
    )
    opener = urllib.request.build_opener()
    if cookie_file and Path(cookie_file).exists():
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(str(cookie_file), ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(req, timeout=25) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    if d.get("code") != 0:
        return False, f"未登录（B 站回：{d.get('message') or d.get('code')}）"
    dd = d.get("data") or {}
    if not dd.get("isLogin"):
        return False, "未登录"
    vip = dd.get("vipStatus")
    tail = ""
    due = dd.get("vipDueDate") or 0
    if vip and due:
        try:
            tail = "，到期 " + datetime.datetime.fromtimestamp(due / 1000).strftime("%Y-%m-%d")
        except Exception:
            tail = ""
    return True, f"已登录：{dd.get('uname') or '?'} · {dd.get('vip_label') or ('大会员' if vip else '非大会员')}{tail}"


class GuestCookies:
    """自取访客 cookie。

    这台机器上 `--cookies-from-browser edge` 必失败（Edge 常驻独占锁住 cookie 数据库，
    报 `Could not copy Chrome cookie database` + `[WinError 5] 拒绝访问`），
    所以走这条路：直接问 B 站要 buvid3 / b_nut，存成 Netscape 格式给 yt-dlp 用。
    游客 cookie 足够清掉 HTTP 412，也够下所有非会员流。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.jar = http.cookiejar.MozillaCookieJar(str(self.path))
        if self.path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass  # 坏掉的 jar 不是致命问题，重取即可
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Accept": "*/*"},
        )
        with self.opener.open(req, timeout=40) as r:
            return r.read().decode("utf-8", "replace")

    def refresh(self) -> list[str]:
        for url in (
            "https://www.bilibili.com/",
            "https://api.bilibili.com/x/frontend/finger/spi",
        ):
            try:
                self._fetch(url)
            except Exception:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)
        return self.names()

    def names(self) -> list[str]:
        return sorted({c.name for c in self.jar})

    def ensure(self) -> list[str]:
        if not self.jar and self.path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        if not self.jar:
            return self.refresh()
        return self.names()


# --------------------------------------------------------------------- 数据模型

@dataclass
class Item:
    url: str
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    top_height: int = 0
    quality_note: str = ""
    note: str = ""
    thumb_url: str = ""
    thumb_bytes: bytes | None = None
    status: str = "待解析"
    progress: float = 0.0
    outfile: str = ""
    outfile2: str = ""     # 分开下载时的第二个成品（视频走 outfile，音频走这里）
    ok: bool | None = None
    n_entries: int = 0


class YtdlpLogger:
    """把 yt-dlp 的输出收进日志窗，同时抓会员画质提示。"""

    def __init__(self, sink):
        self.sink = sink

    def debug(self, msg):
        if not str(msg).startswith("[debug] "):
            self.sink(str(msg))

    def info(self, msg):
        self.sink(str(msg))

    def warning(self, msg):
        self.sink("⚠ " + str(msg))

    def error(self, msg):
        self.sink("✖ " + str(msg))


# --------------------------------------------------------------------- 主界面

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        # 真正的窗口尺寸在 _build_ui() 末尾由 _fit_window() 按内容需求决定，
        # 这里只是个初始值。
        self.geometry("1160x800")

        self.settings = self._load_settings()
        self.items: list[Item] = []
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.ffmpeg = find_ffmpeg()
        self._thumb_img = None  # 防 PhotoImage 被 GC
        self._alive = True
        self._pump_id: str | None = None

        self._build_style()
        self._build_ui()
        self.job = self._snapshot_job()
        self._set_cookie_status("准备 Cookie…")
        self._pump_id = self.after(150, self._pump)
        self._fade_log(self._banner())
        self._fade_log("提示：粘贴地址后按「① 解析信息」可以先看清标题、时长与可用画质，\n"
                       "确认没下错再下载。分享文案、短链 b23.tv、裸 BV 号都能识别。\n")
        threading.Thread(target=self._cookie_boot, args=(self.var_cookie.get(),), daemon=True).start()

    @staticmethod
    def _banner() -> str:
        """日志第一行写清「现在跑的是哪个文件、哪个时间的版本」。

        程序很容易在本机存在多份副本（开发用的、双击启动的那份），曾经因为启动的是
        没同步的旧副本，出现「新加的按钮不见了」这种查半天的假 bug。
        有了这一行，一眼就能对上号。
        """
        f = Path(__file__).resolve()
        try:
            stamp = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            stamp = "?"
        return f"B站视频下载器 · 程序文件 {f}（{stamp}）\n"

    # ------------------------------------------------------------ 设置持久化

    def _load_settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        try:
            if SETTINGS_FILE.exists():
                s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
        return s

    def _save_settings(self) -> None:
        try:
            self.settings.update(
                outdir=self.var_outdir.get(),
                quality=self.var_quality.get(),
                codec=self.var_codec.get(),
                embed=bool(self.var_embed.get()),
                playlist=bool(self.var_playlist.get()),
                bv_in_name=bool(self.var_bvname.get()),
                replace_same_name=bool(self.var_replace.get()),
                split_av=bool(self.var_split.get()),
                cookiefile=self.var_cookie.get(),
            )
            SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ------------------------------------------------------------ 外观

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        try:
            self.option_add("*Font", ("Microsoft YaHei UI", 9))
        except Exception:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Ok.TLabel", foreground="#137333")
        style.configure("Err.TLabel", foreground="#c5221f")

    # ------------------------------------------------------------ 控件搭建

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        # 按钮行【不能】有权重。Tk 的 grid 在「窗口比内容矮」时，是按各行权重去扣空间的：
        # 一旦按钮行有权重，被扣光了它就整行 mapped=False —— 「② 开始下载」凭空消失。
        # 让队列和日志去扛这个赤字，按钮行必须钉死。
        outer.rowconfigure(3, weight=0)
        outer.rowconfigure(4, weight=1)  # 屏幕装不下时，队列和日志先让位给按钮行
        outer.rowconfigure(6, weight=2)

        # ---- ① 地址输入
        box = ttk.LabelFrame(outer, text=" ① 粘贴 B 站视频地址（每行一个） ", padding=8)
        box.grid(row=0, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        self.txt = tk.Text(box, height=5, wrap="none", undo=True,
                           font=("Consolas", 10), relief="solid", borderwidth=1)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=sb.set)

        pv = ttk.LabelFrame(box, text=" 封面预览 ", padding=4)
        pv.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        # 固定占位框：图片来之前和来之后，这一块的请求尺寸完全一样，布局不会抖。
        holder = ttk.Frame(pv, width=THUMB_W, height=THUMB_H)
        holder.pack(fill="both", expand=True)
        holder.pack_propagate(False)
        self.lbl_thumb = ttk.Label(holder, text="解析后\n显示封面", style="Hint.TLabel",
                                   anchor="center", justify="center")
        self.lbl_thumb.pack(fill="both", expand=True)

        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="从剪贴板粘贴", command=self._paste).pack(side="left")
        ttk.Button(bar, text="清空", command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=6)
        ttk.Label(bar, text="识别：完整链接 / b23.tv 短链 / 裸 BV 号 / App 分享文案",
                  style="Hint.TLabel").pack(side="left", padx=12)

        # ---- ② 保存位置
        box2 = ttk.LabelFrame(outer, text=" ② 保存到 ", padding=8)
        box2.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        box2.columnconfigure(0, weight=1)
        self.var_outdir = tk.StringVar(value=self.settings["outdir"])
        ttk.Entry(box2, textvariable=self.var_outdir, font=("Consolas", 9)).grid(row=0, column=0, sticky="ew")
        ttk.Button(box2, text="浏览…", command=self._pick_dir).grid(row=0, column=1, padx=4)
        ttk.Button(box2, text="打开目录", command=self._open_outdir).grid(row=0, column=2)

        # ---- ③ 选项
        box3 = ttk.LabelFrame(outer, text=" ③ 下载选项 ", padding=8)
        box3.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        for c in (3, 7):
            box3.columnconfigure(c, weight=1)

        ttk.Label(box3, text="画质").grid(row=0, column=0, sticky="w")
        self.var_quality = tk.StringVar(value=self.settings["quality"])
        ttk.Combobox(box3, textvariable=self.var_quality, state="readonly", width=26,
                     values=[p["label"] for p in QUALITY_PRESETS]).grid(row=0, column=1, sticky="w", padx=(6, 18))

        ttk.Label(box3, text="编码").grid(row=0, column=2, sticky="w")
        self.var_codec = tk.StringVar(value=self.settings["codec"])
        ttk.Combobox(box3, textvariable=self.var_codec, state="readonly", width=22,
                     values=[c["label"] for c in CODEC_PRESETS]).grid(row=0, column=3, sticky="w", padx=(6, 18))

        self.var_embed = tk.BooleanVar(value=self.settings["embed"])
        ttk.Checkbutton(box3, text="嵌入封面与元数据", variable=self.var_embed).grid(row=0, column=4, sticky="w")
        self.var_playlist = tk.BooleanVar(value=self.settings["playlist"])
        ttk.Checkbutton(box3, text="下载整个合集 / 多P", variable=self.var_playlist).grid(row=0, column=5, sticky="w", padx=(14, 0))
        self.var_bvname = tk.BooleanVar(value=self.settings["bv_in_name"])
        ttk.Checkbutton(box3, text="文件名带 BV 号", variable=self.var_bvname).grid(row=0, column=6, sticky="w", padx=(14, 0))
        self.var_split = tk.BooleanVar(value=self.settings["split_av"])
        self.chk_split = ttk.Checkbutton(
            box3, text="分开下载：视频 mp4 + 音频 mp3（不合并成一个文件）", variable=self.var_split)
        # 单独占一行：塞进上面那一行会把整个选项区的请求宽度顶到 1760px，
        # 在 1366 宽的小屏上这一列会被直接切掉。独占一行就只是多 30px 高度，
        # 而高度是 _fit_window() 会自己让出来的。
        self.chk_split.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        # 画质选「仅音频」时没有画面可拆，把勾选框灰掉——不给做不到的承诺。
        # trace 而不是 <<ComboboxSelected>>，这样程序里 set() 也同步得到。
        self.var_quality.trace_add("write", lambda *_: self._sync_split_state())

        ttk.Label(box3, text="Cookie").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_cookie = tk.StringVar(value=self.settings["cookiefile"])
        ttk.Entry(box3, textvariable=self.var_cookie, font=("Consolas", 9)).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(6, 18), pady=(8, 0))
        ttk.Button(box3, text="换文件…", command=self._pick_cookie).grid(row=1, column=4, sticky="w", pady=(8, 0))
        self.btn_login_cookie = ttk.Button(box3, text="粘贴登录 Cookie…", command=self._paste_cookie)
        self.btn_login_cookie.grid(row=1, column=5, sticky="w", padx=(14, 0), pady=(8, 0))
        ttk.Button(box3, text="刷新游客 Cookie", command=self._refresh_cookie).grid(
            row=1, column=6, sticky="w", padx=(10, 0), pady=(8, 0))
        self.lbl_cookie = ttk.Label(box3, text="", style="Hint.TLabel")
        # 单独占一行：挤在「文件名带 BV 号」下面那一列里时，列宽只有 checkbox 那么宽，
        # 文本会被截成「游客 Cookie 就绪: b_nut, bu…」。
        self.lbl_cookie.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))
        # 教程入口就钉在 Cookie 这块里：画质不够时，人第一眼看的就是这行状态。
        self.btn_hq = ttk.Button(box3, text="想要更高画质？看教程", command=self._show_quality_guide)
        self.btn_hq.grid(row=2, column=6, columnspan=2, sticky="e", pady=(4, 0))

        hint = ("说明：UP 主只传到某档时，这就是硬上限。「1080P 高码率 / 4K / HDR / 杜比」属大会员画质，"
                "游客取不到——用「粘贴登录 Cookie…」喂一份登录态就能下（下面那行状态会显示你是谁）。"
                "用「解析信息」会明确告诉你这次拿到了什么。")
        ttk.Label(box3, text=hint, style="Hint.TLabel", wraplength=1060, justify="left").grid(
            row=4, column=0, columnspan=8, sticky="w", pady=(8, 0))

        # ---- ④ 操作按钮
        act = ttk.Frame(outer)
        act.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.btn_probe = ttk.Button(act, text="① 解析信息", command=lambda: self._start("probe"))
        self.btn_probe.pack(side="left")
        self.btn_dl = ttk.Button(act, text="② 开始下载", style="Accent.TButton",
                                 command=lambda: self._start("download"))
        self.btn_dl.pack(side="left", padx=8)
        self.btn_stop = ttk.Button(act, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left")
        self.clear_btn = ttk.Button(act, text="清空队列", command=self._clear_queue)
        self.clear_btn.pack(side="left", padx=8)
        self.var_replace = tk.BooleanVar(value=self.settings["replace_same_name"])
        ttk.Checkbutton(act, text="同名文件直接替换（已完成的也重下）",
                        variable=self.var_replace).pack(side="left", padx=(16, 0))
        self.lbl_sum = ttk.Label(act, text="", style="Hint.TLabel")
        self.lbl_sum.pack(side="right")

        # ---- ⑤ 队列
        qf = ttk.LabelFrame(outer, text=" ④ 队列 ", padding=6)
        qf.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        qf.columnconfigure(0, weight=1)
        qf.rowconfigure(0, weight=1)
        cols = ("title", "dur", "quality", "prog", "status")
        self.tree = ttk.Treeview(qf, columns=cols, show="headings", selectmode="browse", height=7)
        for cid, txt, w, anchor in (
            ("title", "标题", 330, "w"),
            ("dur", "时长", 72, "center"),
            ("quality", "可用画质", 190, "w"),
            ("prog", "进度", 66, "center"),
            ("status", "状态", 260, "w"),
        ):
            self.tree.heading(cid, text=txt)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid in ("title", "status", "quality")))
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(qf, orient="vertical", command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.tag_configure("odd", background="#fafafa")
        self.tree.tag_configure("done", foreground="#137333")
        self.tree.tag_configure("fail", foreground="#c5221f")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)

        # ---- ⑥ 进度 + 日志
        pf = ttk.Frame(outer)
        pf.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        pf.columnconfigure(1, weight=1)
        ttk.Label(pf, text="当前进度").grid(row=0, column=0, sticky="w")
        self.pb = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb.grid(row=0, column=1, sticky="ew", padx=8)
        self.lbl_prog = ttk.Label(pf, text="就绪", width=44, anchor="w", style="Hint.TLabel")
        self.lbl_prog.grid(row=0, column=2, sticky="w")

        lf = ttk.LabelFrame(outer, text=" ⑤ 日志 ", padding=6)
        lf.grid(row=6, column=0, sticky="nsew", pady=(8, 0))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log = tk.Text(lf, height=6, wrap="word", state="disabled",
                           font=("Consolas", 9), relief="flat", background="#fbfbfb")
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 有些控件的请求尺寸要等窗口真正映射出来才算得准（隐藏时算出来的偏小 20px 左右），
        # 所以「显示出来」这个时机要再校一次，否则窗口会比内容矮一点点。
        self.bind("<Map>", lambda e: self._ensure_fits() if e.widget is self else None)
        self._sync_split_state()
        self._fit_window()

    def _sync_split_state(self) -> None:
        """画质选了「仅音频」时没有画面可拆 —— 灰掉勾选框，别让人以为会出 mp4。"""
        audio = preset_by_label(self.var_quality.get())["kind"] == "audio"
        self.chk_split.configure(state="disabled" if audio else "normal")

    def _geom_inside_screen(self, w: int, h: int) -> str:
        """把窗口摆进屏幕里，返回 "宽x高+X+Y"。

        只设尺寸不设位置时，窗口会按左上角不动地「长下去」：1080 高的屏幕、1000 高的
        窗口，左上角一旦落在 y=239，底部的日志就长到屏幕外面去了（看着像凭空消失）。
        所以每次改尺寸都顺手把位置夹回来。
        """
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(self.winfo_x(), max(0, sw - w)))
        y = max(0, min(self.winfo_y(), max(0, sh - 40 - h)))
        return f"{w}x{h}+{x}+{y}"

    def _fit_window(self) -> None:
        """按内容需求决定窗口大小。

        原先写死 1160x780，而所有控件加起来要 832 像素，grid 只好从没设权重的行里
        抠高度，把「① 解析信息 / ② 开始下载」那一整行挤成 1 像素（mapped=False）——
        按钮明明建出来了却根本点不到。自检直接调 _start() 绕过了按钮，所以没发现。
        """
        self.update_idletasks()
        need_w = max(1160, self.winfo_reqwidth() + 4)
        need_h = self.winfo_reqheight() + 6
        screen_w = self.winfo_screenwidth()
        screen_h = max(600, self.winfo_screenheight() - 80)  # 给任务栏留位置
        w, h = min(need_w, screen_w), min(need_h, screen_h)
        self.geometry(self._geom_inside_screen(w, h))
        self.minsize(min(940, w), min(h, screen_h))

    def _ensure_fits(self) -> None:
        """内容长高了就把窗口跟着长高。

        窗口尺寸只在启动时算一次，但内容是会变的：封面一显示，① 区就高 90 像素。
        超出之后 grid 会把没有权重的行挤成 0 高度，那一行的控件立刻 mapped=False——
        表现就是「下载完按钮不见了」。这里只增不减，用户手动拉大的尺寸不会被抢回去。
        """
        try:
            self.update_idletasks()
            screen_w = self.winfo_screenwidth()
            screen_h = max(600, self.winfo_screenheight() - 80)
            w, h = self.winfo_width(), self.winfo_height()
            new_w = min(max(w, self.winfo_reqwidth() + 4), screen_w)
            new_h = min(max(h, self.winfo_reqheight() + 6), screen_h)
            if new_w > w or new_h > h:
                self.geometry(self._geom_inside_screen(new_w, new_h))
            # 下限就是内容高度：再往下拖就又会出现点不到的按钮。
            # 放在 try 里，免得 update_idletasks() 抛异常时 w/h 还没赋值就往下走。
            self.minsize(min(940, new_w), new_h)
        except Exception:  # 布局救急，绝不能反过来把下载搞崩
            pass

    # ------------------------------------------------------------ 日志

    def _fade_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._fade_log(f"[{stamp}] {text}\n")

    # ------------------------------------------------------------ 队列渲染

    def _iid(self, idx: int) -> str:
        return f"i{idx}"

    def _row_values(self, it: Item) -> tuple:
        return (
            it.title or it.url,
            human_duration(it.duration),
            it.quality_note or "—",
            f"{it.progress:.0f}%" if it.progress else "",
            it.status,
        )

    def _refresh_row(self, idx: int) -> None:
        iid = self._iid(idx)
        if not self.tree.exists(iid):
            return
        it = self.items[idx]
        tags = ["odd"] if idx % 2 else []
        if it.ok is True:
            tags.append("done")
        elif it.ok is False:
            tags.append("fail")
        self.tree.item(iid, values=self._row_values(it), tags=tuple(tags))

    def _add_items(self, urls: list[str]) -> None:
        added = 0
        for u in urls:
            if any(it.url == u for it in self.items):
                continue
            idx = len(self.items)
            self.items.append(Item(url=u))
            self.tree.insert("", "end", iid=self._iid(idx), values=self._row_values(self.items[idx]))
            added += 1
        self._update_summary()
        if added:
            self._log(f"加入队列 {added} 个地址")

    def _update_summary(self) -> None:
        done = sum(1 for it in self.items if it.ok is True)
        fail = sum(1 for it in self.items if it.ok is False)
        self.lbl_sum.configure(
            text=f"共 {len(self.items)} 项 · 成功 {done} · 失败 {fail}" if self.items else ""
        )
        self._ensure_fits()

    def _clear_queue(self) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "正在下载，请先停止。")
            return
        self.items.clear()
        self.tree.delete(*self.tree.get_children())
        self._update_summary()
        self._show_thumb(None)

    # ------------------------------------------------------------ 交互

    def _paste(self) -> None:
        try:
            data = self.clipboard_get()
        except Exception:
            data = ""
        if not data.strip():
            messagebox.showinfo(APP_TITLE, "剪贴板是空的。")
            return
        cur = self.txt.get("1.0", "end").strip()
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", (cur + "\n" + data).strip() if cur else data)
        n = len(extract_targets(data))
        self._log(f"从剪贴板粘贴，识别到 {n} 个地址" if n else "从剪贴板粘贴，但没识别出地址")

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or str(SCRIPT_DIR))
        if d:
            self.var_outdir.set(d)
            self._save_settings()

    def _open_outdir(self) -> None:
        d = Path(self.var_outdir.get())
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # noqa: S606 - Windows 专用
        except Exception:
            subprocess.Popen(["explorer", str(d)])

    def _pick_cookie(self) -> None:
        p = filedialog.askopenfilename(
            title="选择 cookies.txt（Netscape 格式）",
            initialdir=str(Path(self.var_cookie.get()).parent),
            filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")],
        )
        if p:
            self.var_cookie.set(p)
            self._save_settings()
            self._set_cookie_status(f"已指定：{Path(p).name}", ok=True)

    def _refresh_cookie(self) -> None:
        threading.Thread(target=self._cookie_boot, args=(self.var_cookie.get(), True), daemon=True).start()

    def _show_quality_guide(self) -> None:
        """「怎么下到更高画质」的完整教程，直接写在程序里。

        光靠状态行那句「会员画质取不到」说不清事：用户分不出是 UP 主没传到、
        还是被大会员门槛挡了、还是自己压根没登录。所以把判断依据和操作步骤都摊开，
        并且把「打开 B 站」「去粘贴」做成按钮——省得看完教程还要自己找入口。
        """
        win = tk.Toplevel(self)
        win.title("怎么下到更高画质（1080P 高码率 / 4K / HDR / 杜比）")
        win.transient(self)
        win.geometry("820x700")
        win.minsize(600, 440)

        body = ttk.Frame(win, padding=(12, 10))
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, wrap="word", relief="flat", background="#fbfbfb",
                      font=("Microsoft YaHei UI", 10), padx=12, pady=8, cursor="arrow")
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h", font=("Microsoft YaHei UI", 12, "bold"), spacing1=14, spacing3=6)
        txt.tag_configure("s", font=("Microsoft YaHei UI", 11, "bold"), spacing1=10, spacing3=4)
        txt.tag_configure("warn", foreground="#b03030")
        txt.tag_configure("ok", foreground="#1a7f37")

        def put(text: str, tag: str = "") -> None:
            txt.insert("end", text, tag)

        put("为什么现在下不到最高画质\n", "h")
        put("B 站能给你的画质上限有两个来源，要分开看：\n")
        put("① UP 主自己只传到了某一档\n", "s")
        put("这是硬上限，谁都下不到更高——大会员也不行。\n")
        put("② 「1080P 高码率 / 4K / HDR / 杜比」属大会员画质\n", "s")
        put("B 站对没登录的游客根本不下发这些流（yt-dlp 会主动去要「试看」流，"
            "所以拿到的是被截断的那一档）。\n")
        put("    注意：标准的 1080P（H.264 约 2.6 Mbps）游客是能下的，"
            "被挡的只是它上面那几档。\n")
        put("第 ② 种，喂一份「登录后的 Cookie」就能解决。\n")

        put("操作步骤\n", "h")
        put("1. 用浏览器登录 bilibili.com，确认这个账号是大会员。\n")
        put("2. 按 F12 打开开发者工具。\n")
        put("3. 上方切到「Application」标签（中文版：应用程序）。\n")
        put("4. 左侧点开 Cookies → https://www.bilibili.com。\n")
        put("5. 在列表里找到 SESSDATA，双击它的 Value 列，Ctrl+C 复制。\n")
        put("    也可以切到 Network 标签、刷新页面、点任意一条请求，"
            "在 Request Headers 里\n    复制 Cookie: 那一整行——两种格式都认。\n")
        put("6. 回到主窗口点「粘贴登录 Cookie…」，把内容贴进去，点「保存并检查登录」。\n")

        put("成功是什么样\n", "h")
        put("状态行会变成：")
        put("已登录：你的昵称 · 年度大会员，到期 2026-01-01 · 会员画质可用\n", "ok")
        put("然后点「解析信息」，最高画质会显示 2160P / 4K 之类；"
            "画质选「最高画质（不设上限）」就会选到最上面那一档。\n")

        put("要注意的几点\n", "h")
        put("· 只复制 SESSDATA 的值就够。缺的 buvid3 / b_nut（防 HTTP 412 用的）"
            "工具会自动补进同一个文件。\n")
        put("· SESSDATA 有有效期。过期后状态行会退回「未登录」，重新粘一次就行。\n")
        put("· SESSDATA 等同于你的登录凭证，别发给别人；"
            "工具只把它存在本机的 cookies_login.txt 里。\n")
        put("· 只有大会员账号才有上面那几档。普通账号登录后能拿到什么，"
            "以「解析信息」的结果为准——它不会骗你。\n")
        put("· 如果登录明明成功了、画质却没变，那多半是第 ① 种：UP 主就传到那儿。\n", "warn")

        txt.configure(state="disabled")  # 只读，但仍然可以选中复制

        bar = ttk.Frame(win, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="① 打开 bilibili.com",
                   command=lambda: webbrowser.open("https://www.bilibili.com")).pack(side="left")
        ttk.Button(bar, text="② 我复制好了，去粘贴 →", style="Accent.TButton",
                   command=lambda: (win.destroy(), self._paste_cookie())).pack(side="left", padx=10)
        ttk.Button(bar, text="从浏览器直接读（先退出浏览器）",
                   command=lambda: (win.destroy(), self._import_browser_cookie())).pack(side="left")
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
        try:
            win.grab_set()  # 还没映射出来时 Tk 会拒绝 grab，弹窗只是变成非模态，不影响用
        except tk.TclError:
            pass

    def _paste_cookie(self) -> None:
        """粘一份登录 Cookie → 生成 cookies.txt，然后当场问 B 站「我是谁」。

        为什么不做「从浏览器读」当主路：浏览器开着的时候锁着自己的 cookie 数据库，
        `--cookies-from-browser` 在这台机器上必失败（Could not copy Chrome cookie
        database + WinError 5）。手动粘一次最省事，而大会员画质必须要登录态。
        """
        win = tk.Toplevel(self)
        win.title("粘贴登录 Cookie（大会员画质用）")
        win.transient(self)
        win.resizable(False, False)
        tip = ttk.Frame(win)
        tip.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(tip, justify="left", text=(
            "1) 浏览器登录 bilibili.com（大会员账号）\n"
            "2) F12 → Application → Cookies → https://www.bilibili.com\n"
            "3) 找到 SESSDATA，复制它的 Value；或直接复制任意一条请求的整条 Cookie 头（都认）"
        )).pack(side="left", anchor="w")
        ttk.Button(tip, text="看详细教程",
                   command=lambda: (win.destroy(), self._show_quality_guide())
                   ).pack(side="right", anchor="n", padx=(12, 0))
        txt = tk.Text(win, width=78, height=7, wrap="word", font=("Consolas", 9))
        txt.pack(padx=14, fill="x")
        txt.focus_set()

        def fill_from_clipboard() -> None:
            """刚复制完就切过来的话，剪贴板里就是 SESSDATA——省得再粘一次。"""
            try:
                data = self.clipboard_get()
            except Exception:
                data = ""
            if not data.strip():
                messagebox.showinfo("剪贴板是空的",
                                    "先在浏览器里复制 SESSDATA 的值（或整条 Cookie 头）。", parent=win)
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", data.strip())

        def save() -> None:
            pairs = parse_cookie_text(txt.get("1.0", "end"))
            if not pairs:
                messagebox.showwarning("没读到东西", "把 Cookie 粘进来再点保存。", parent=win)
                return
            # 单独一个文件，别把游客 cookies.txt 覆盖掉——那份还要留着兜底
            out = SCRIPT_DIR / "cookies_login.txt"
            try:
                write_netscape_cookie_file(out, pairs)
            except Exception as e:
                messagebox.showerror("写文件失败", f"{type(e).__name__}: {e}", parent=win)
                return
            win.destroy()
            self.var_cookie.set(str(out))
            self._save_settings()
            self._set_cookie_status(f"正在检查登录态…（{', '.join(sorted(pairs)[:3])}）")
            # 缺 buvid3 会在 _cookie_boot 里自动补上，不需要用户操心
            threading.Thread(target=self._cookie_boot, args=(str(out), False), daemon=True).start()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=14, pady=(10, 14))
        ttk.Button(bar, text="从剪贴板填入", command=fill_from_clipboard).pack(side="left")
        ttk.Button(bar, text="保存并检查登录", style="Accent.TButton", command=save).pack(side="left", padx=10)
        ttk.Button(bar, text="从浏览器直接读（先退出浏览器）",
                   command=lambda: (win.destroy(), self._import_browser_cookie())).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        try:
            win.grab_set()
        except tk.TclError:
            pass

    def _import_browser_cookie(self) -> None:
        """兜底路线：直接从浏览器 cookie 库读（浏览器必须完全退出，否则库被锁）。"""
        def work() -> None:
            try:
                from yt_dlp.cookies import extract_cookies_from_browser
            except Exception as e:
                self.q.put(("cookie", (False, f"这个 yt-dlp 版本没有 cookie 导入接口：{e}")))
                return
            last = ""
            for b in ("edge", "chrome", "firefox"):
                try:
                    jar = extract_cookies_from_browser(b)
                except Exception as e:
                    last = f"{b}: {e}"
                    continue
                if not any(c.name == "SESSDATA" for c in jar):
                    last = f"{b}: 没读到 SESSDATA（那个浏览器没登录 B 站？）"
                    continue
                out = SCRIPT_DIR / "cookies_login.txt"
                try:
                    jar.save(str(out), ignore_discard=True, ignore_expires=True)
                except Exception as e:
                    last = f"{b}: 写文件失败 {e}"
                    continue
                self.q.put(("cookiefile", (str(out),)))
                self.q.put(("cookie", (True, f"已从 {b} 导入登录 Cookie")))
                return
            self.q.put(("cookie", (False,
                                   f"没能从浏览器读到登录 Cookie（{last}）——"
                                   "浏览器开着的时候锁着 cookie 库，先把它完全退出再试")))
        threading.Thread(target=work, daemon=True).start()

    def _set_cookie_status(self, text: str, ok: bool | None = None) -> None:
        self.lbl_cookie.configure(
            text=text,
            style="Ok.TLabel" if ok is True else ("Err.TLabel" if ok is False else "Hint.TLabel"),
        )
        self._ensure_fits()

    def _cookie_boot(self, cookie_path: str, force: bool = False) -> None:
        """只做网络 IO，参数在主线程取好传进来——工作线程碰 Tk 变量会直接抛异常。"""
        try:
            p = Path(cookie_path or DEFAULT_COOKIE)
            jar = GuestCookies(p)
            # 缺 buvid3/b_nut 就补一次访客 cookie：它们管的是「别被 412 挡掉」，
            # 和登录态无关，登录 cookie 里也照样需要。refresh() 不会清掉已有的 SESSDATA
            # ——它是先把文件读进 jar、再存回去，所以是合并而不是覆盖。
            need = force or not jar.jar or "buvid3" not in jar.names()
            names = jar.refresh() if need else jar.names()
            if not names:
                names = jar.refresh()
            if not names:
                self.q.put(("cookie", (False, "Cookie 获取失败")))
                return
            try:
                logged, who = probe_login(p)  # 问 B 站「我是谁」，SESSDATA 过期了也能查出来
            except Exception:
                logged, who = False, ""
            if logged:
                self.q.put(("cookie", (True, f"{who} · 会员画质可用")))
            else:
                short = who.split("（")[0] if who else "未登录"
                self.q.put(("cookie", (True,
                                       f"{short}（游客 Cookie：{', '.join(names[:3])}）· 会员画质取不到")))
        except Exception as e:
            self.q.put(("cookie", (False, f"Cookie 获取失败：{e}")))

    def _on_select(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0][1:])
        if 0 <= idx < len(self.items):
            self._show_thumb(self.items[idx].thumb_bytes)

    def _show_thumb(self, data: bytes | None) -> None:
        self._thumb_img = None  # 先丢掉旧引用，否则 Tk 图片对象泄漏
        placeholder = "解析后\n显示封面"
        if Image is None or not data:
            self.lbl_thumb.configure(image="", text=placeholder)
            return
        try:
            import io
            im = Image.open(io.BytesIO(data))
            im.thumbnail((THUMB_W, THUMB_H))
            self._thumb_img = ImageTk.PhotoImage(im)
            self.lbl_thumb.configure(image=self._thumb_img, text="")
        except Exception:
            self.lbl_thumb.configure(image="", text=placeholder)
        finally:
            self._ensure_fits()

    def _on_double_click(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[int(sel[0][1:])]
        if it.ok and it.outfile and Path(it.outfile).exists():
            try:
                os.startfile(it.outfile)  # noqa: S606
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"打不开文件：{e}")
        else:
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", it.url)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_TITLE, "正在下载，确定要退出吗？"):
            return
        self.cancel.set()
        self._save_settings()
        self.destroy()

    # ------------------------------------------------------------ 任务调度

    def _snapshot_job(self) -> dict:
        """把界面选项一次性拷成普通 dict，交给工作线程用。

        tkinter 的变量不是线程安全的：在主线程之外调用 var.get() 会抛
        RuntimeError（主线程不在 mainloop 里时必炸）。所以下载线程只读这份快照，
        绝不碰任何 Tk 对象。
        """
        return {
            "outdir": self.var_outdir.get(),
            "quality": self.var_quality.get(),
            "codec": self.var_codec.get(),
            "embed": bool(self.var_embed.get()),
            "playlist": bool(self.var_playlist.get()),
            "bvname": bool(self.var_bvname.get()),
            "replace": bool(self.var_replace.get()),
            "split": bool(self.var_split.get()),
            "cookie": self.var_cookie.get(),
            "ffmpeg": self.ffmpeg,
        }

    def _start(self, mode: str) -> None:
        if self.busy:
            return
        urls = extract_targets(self.txt.get("1.0", "end"))
        if not urls:
            messagebox.showinfo(APP_TITLE, "还没识别到地址。粘贴 B 站链接或 BV 号后再试。")
            return
        self._add_items(urls)
        # 勾了「同名文件直接替换」时，已成功的项也要重来一遍——否则第二次点下载
        # 什么都没发生，磁盘上还是上一版文件。没勾就只补失败的和新加的。
        replace = bool(self.var_replace.get())
        for it in self.items:
            if replace or it.ok is not True:
                it.status = "排队中" if mode == "download" else "待解析"
                it.progress = 0.0
                it.ok = None
                it.outfile2 = ""  # 别让上一轮「分开下载」的音频文件挂在这一次的结论上
        for idx in range(len(self.items)):
            self._refresh_row(idx)
        self.ffmpeg = find_ffmpeg()
        if self.ffmpeg is None:
            self._log("⚠ 没找到 ffmpeg：视频与音频将无法合并、封面也无法内嵌。")
        self.job = self._snapshot_job()  # 主线程取快照，工作线程只读它
        self._save_settings()
        self.cancel.clear()
        self.busy = True
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(mode,), daemon=True).start()

    def _stop(self) -> None:
        self.cancel.set()
        self._log("收到停止请求，正在中断当前任务…")
        self.lbl_prog.configure(text="正在停止…")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_probe, self.btn_dl):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # ------------------------------------------------------------ yt-dlp 选项

    def _base_opts(self, sink, item_dir: Path) -> dict:
        job = self.job
        opts = {
            "quiet": True,
            "noprogress": True,
            "no_warnings": False,
            "logger": YtdlpLogger(sink),
            "noplaylist": not job["playlist"],
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "continuedl": True,
            "overwrites": True,  # 同名文件直接替换；不追加序号、也不「已下载过就跳过」
            "windowsfilenames": True,
            "trim_file_name": 120,
            "ignoreerrors": False,
        }
        if job["ffmpeg"]:
            opts["ffmpeg_location"] = str(job["ffmpeg"])
        cookie = Path(job["cookie"])
        if cookie.exists():
            opts["cookiefile"] = str(cookie)
        name = "%(title).100s [%(id)s].%(ext)s" if job["bvname"] else "%(title).100s.%(ext)s"
        opts["outtmpl"] = str(item_dir / name)
        return opts

    def _probe(self, it: Item, sink) -> bool:
        opts = self._base_opts(sink, SCRIPT_DIR)
        opts.update(skip_download=True, noprogress=True)
        opts.pop("outtmpl", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(it.url, download=False)
        if not info:
            return False
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            it.n_entries = len(entries)
            first = entries[0] if entries else {}
            it.title = info.get("title") or first.get("title") or it.url
            it.uploader = info.get("uploader") or first.get("uploader") or ""
            it.duration = sum((e.get("duration") or 0) for e in entries)
            heights = [describe_formats(e)[0] for e in entries] or [0]
            it.top_height = max(heights)
            it.quality_note = f"合集 {it.n_entries} 个 · 最高 {it.top_height}P" if it.top_height else f"合集 {it.n_entries} 个"
            it.thumb_url = first.get("thumbnail") or ""
            it.note = f"这是一个含 {it.n_entries} 个视频的合集"
        else:
            it.title = info.get("title") or it.url
            it.uploader = info.get("uploader") or ""
            it.duration = info.get("duration") or 0
            it.top_height, it.quality_note = describe_formats(info)
            it.thumb_url = info.get("thumbnail") or ""
            it.note = ""
        return True

    def _download(self, it: Item, idx: int, sink) -> str:
        job = self.job
        preset = preset_by_label(job["quality"])
        outdir = Path(job["outdir"])
        outdir.mkdir(parents=True, exist_ok=True)
        opts = self._base_opts(sink, outdir)

        if job.get("split"):
            if preset["kind"] == "audio":
                self.q.put(("log", ("画质选的是「仅音频」，没有画面可拆 —— 这次只下音频。",)))
            else:
                return self._download_split(it, idx, sink, preset, outdir)

        state = {"phase": 0}
        multi = preset["kind"] == "video" and job["ffmpeg"] is not None

        def scaled(pct: float, phase: int) -> float:
            if not multi:
                return pct
            return pct * 0.92 if phase == 0 else 92.0 + pct * 0.08

        def hook(d):
            if self.cancel.is_set():
                raise DownloadCancelled("用户停止")
            st = d.get("status")
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                pct = (got / total * 100.0) if total else 0.0
                label = ("视频" if state["phase"] == 0 else "音频") if multi else "下载中"
                self.q.put(("progress", (idx, scaled(pct, state["phase"]), d.get("speed"), d.get("eta"), label)))
            elif st == "finished":
                self.q.put(("progress", (idx, scaled(100.0, state["phase"]), None, 0, "下载完成")))
                state["phase"] += 1

        def pp_hook(d):
            if d.get("status") == "started":
                name = d.get("postprocessor") or ""
                pretty = {
                    "Merger": "合并音视频…",
                    "MoveFiles": "整理文件…",
                    "EmbedThumbnail": "嵌入封面…",
                    "FFmpegMetadata": "写入元数据…",
                    "FFmpegExtractAudio": "提取音轨…",
                }.get(name, f"{name}…")
                self.q.put(("status", (idx, pretty)))

        opts["progress_hooks"] = [hook]
        opts["postprocessor_hooks"] = [pp_hook]

        pps: list[dict] = []
        if preset["kind"] == "audio":
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            codec = preset.get("acodec")
            if codec and codec != "best":
                # best = 原生封装，交给 yt-dlp 直接落 .m4a，不要再过一道 ffmpeg
                pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
                if codec == "mp3":
                    pp["preferredquality"] = "320"
                pps.append(pp)
        else:
            opts["format"] = build_video_format(preset.get("height"), codec_by_label(job["codec"])["re"])
            opts["merge_output_format"] = "mp4"

        if job["embed"]:
            # 注意：addmetadata / embedthumbnail 只在命令行生效，Python API 下
            # YoutubeDL 根本不读这两个键（yt_dlp/__init__.py 才把它们翻译成后处理器）。
            # 必须自己把后处理器按顺序摆好，否则封面和元数据会被静默跳过。
            opts["writethumbnail"] = True
            pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

        if pps:
            opts["postprocessors"] = pps

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(it.url, download=True)
            if info and info.get("requested_downloads"):
                it.outfile = info["requested_downloads"][0].get("filepath") or ""
        return it.outfile

    def _download_split(self, it: Item, idx: int, sink, preset: dict, outdir: Path) -> str:
        """分开下载：第 1 遍只要画面（落 mp4），第 2 遍只要声音（转 320k mp3）。

        为什么必须是两遍：格式选择是「一次调用挑一组 format」，没有哪一次调用能
        既只出画面、又只出声音。所以要两个独立的 yt-dlp 会话，各挑各的流。
        """
        job = self.job
        common = self._base_opts(sink, outdir)
        state = {"phase": 0, "label": "视频"}

        def hook(d):
            if self.cancel.is_set():
                raise DownloadCancelled("用户停止")
            st = d.get("status")
            base = 0.0 if state["phase"] == 0 else SPLIT_VIDEO_END
            span = SPLIT_VIDEO_END if state["phase"] == 0 else 100.0 - SPLIT_VIDEO_END
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                pct = (got / total * 100.0) if total else 0.0
                self.q.put(("progress", (idx, base + pct * span / 100.0,
                                         d.get("speed"), d.get("eta"), state["label"])))
            elif st == "finished":
                self.q.put(("progress", (idx, base + span, None, 0, state["label"] + "下载完成")))
                state["phase"] += 1
                state["label"] = "音频"

        def pp_hook(d):
            if d.get("status") == "started":
                name = d.get("postprocessor") or ""
                pretty = {
                    "FFmpegVideoRemuxer": "转封装 mp4…",
                    "FFmpegExtractAudio": "转成 320k mp3…",
                    "Merger": "合并音视频…",
                    "MoveFiles": "整理文件…",
                    "EmbedThumbnail": "嵌入封面…",
                    "FFmpegMetadata": "写入元数据…",
                }.get(name, f"{name}…")
                self.q.put(("status", (idx, pretty)))

        def pps_with(extra: list[dict]) -> list[dict]:
            pps = list(extra)
            if job["embed"]:
                # 和单遍下载同一个坑：addmetadata / embedthumbnail 只有命令行才认，
                # Python API 下必须自己按顺序摆好后处理器。
                pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
                pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
            return pps

        def run(fmt: str, pps: list[dict]):
            opts = dict(common)
            opts["format"] = fmt
            opts["progress_hooks"] = [hook]
            opts["postprocessor_hooks"] = [pp_hook]
            opts["postprocessors"] = pps
            if job["embed"]:
                opts["writethumbnail"] = True
            self.q.put(("log", (f"  ── 第 {state['phase'] + 1} 遍：{state['label']} ──",)))
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(it.url, download=True)

        # ① 画面：bv 不带音轨；再兜一道转封装，保证落下来的确实是 mp4
        info_v = run(
            build_video_only_format(preset.get("height"), codec_by_label(job["codec"])["re"]),
            pps_with([{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]),
        )
        if self.cancel.is_set():
            raise DownloadCancelled("用户停止")

        # ② 声音：bestaudio 转 320k mp3
        info_a = run(
            "bestaudio[ext=m4a]/bestaudio/best",
            pps_with([{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                       "preferredquality": "320"}]),
        )

        v = find_produced_file([info_v], outdir, "mp4")
        a = find_produced_file([info_a], outdir, "mp3")
        if not v:
            raise RuntimeError("分开下载：没找到视频成品（.mp4）")
        if not a:
            raise RuntimeError("分开下载：没找到音频成品（.mp3）")
        it.outfile, it.outfile2 = str(v), str(a)
        return it.outfile

    # ------------------------------------------------------------ 工作线程

    def _worker(self, mode: str) -> None:
        total = len(self.items)
        try:
            for idx, it in enumerate(self.items):
                if self.cancel.is_set():
                    self.q.put(("status", (idx, "已停止")))
                    break
                if mode == "download" and it.ok is True:
                    continue  # 上一轮已经拿到的不重复下载
                sink_lines: list[str] = []

                def sink(msg, _lines=sink_lines):
                    msg = msg.strip()
                    if not msg:
                        return
                    _lines.append(msg)
                    self.q.put(("log", (f"  {msg}",)))

                if mode == "probe" or not it.title:
                    self.q.put(("status", (idx, "解析中…")))
                    try:
                        it.url = resolve_short_url(it.url, None)
                        ok = self._probe(it, sink)
                    except DownloadCancelled:
                        self.q.put(("status", (idx, "已停止")))
                        break
                    except Exception as e:
                        ok = False
                        self.q.put(("log", (f"解析失败：{type(e).__name__}: {e}",)))
                        self.q.put(("log", (traceback.format_exc(),)))
                    if not ok:
                        it.ok = False
                        it.status = "解析失败"
                        self.q.put(("row", (idx,)))
                        continue
                    gated = any("premium member" in ln for ln in sink_lines)
                    if gated:
                        it.quality_note += " · 另有大会员画质"
                    it.status = f"已解析 · {it.quality_note}"
                    self.q.put(("row", (idx,)))
                    if it.thumb_url:
                        self._fetch_thumb(idx, it.thumb_url)
                    if mode == "probe":
                        continue

                if mode == "probe":
                    continue

                self.q.put(("status", (idx, "开始下载…")))
                self.q.put(("progress", (idx, 0.0, None, None, "")))
                try:
                    path = self._download(it, idx, sink)
                    if self.cancel.is_set():
                        # yt-dlp 有可能把 DownloadCancelled 吃掉后正常返回，这里兜一道
                        it.status = "已停止"
                        it.ok = False
                        self.q.put(("row", (idx,)))
                        break
                    it.ok = True
                    it.progress = 100.0
                    it.status = "✔ 完成（视频 + 音频）" if it.outfile2 else "✔ 完成"
                    if path and not Path(path).exists():
                        # 走过后处理器的文件可能改了扩展名，兜底在目录里找
                        stem = Path(path).stem
                        cands = list(Path(self.job["outdir"]).glob(f"{stem}.*"))
                        if cands:
                            it.outfile = str(max(cands, key=lambda p: p.stat().st_mtime))
                    self.q.put(("log", (f"✔ 已保存：{it.outfile or self.job['outdir']}",)))
                    if it.outfile2:
                        self.q.put(("log", (f"✔ 已保存：{it.outfile2}",)))
                except DownloadCancelled:
                    it.status = "已停止"
                    it.ok = False
                    self.q.put(("log", ("已停止当前任务。",)))
                    self.q.put(("row", (idx,)))
                    break
                except Exception as e:
                    it.ok = False
                    it.status = f"✖ 失败：{type(e).__name__}"
                    self.q.put(("log", (f"✖ 下载失败：{e}",)))
                self.q.put(("row", (idx,)))
                self.q.put(("overall", (idx + 1, total)))
        except Exception:
            self.q.put(("log", ("内部错误：\n" + traceback.format_exc(),)))
        finally:
            self.q.put(("busy", (False,)))
            self.q.put(("done_all", (mode,)))

    def _fetch_thumb(self, idx: int, url: str) -> None:
        def run():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                self.items[idx].thumb_bytes = data
                self.q.put(("thumb", (idx,)))
            except Exception as e:
                # 别静默吞掉：封面拿不到不影响下载，但得让人在日志里看见
                self.q.put(("log", (f"封面预览失败（不影响下载）：{type(e).__name__}: {e}",)))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ 事件泵

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        # 窗口已经 destroy() 了就别再排下一次：否则 Tk 会报
        # `invalid command name "…_pump"`（关窗时在控制台留一串红字）。
        if self._alive:
            self._pump_id = self.after(120, self._pump)

    def destroy(self) -> None:
        self._alive = False
        if self._pump_id is not None:
            try:
                self.after_cancel(self._pump_id)
            except Exception:
                pass
            self._pump_id = None
        super().destroy()

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(payload[0])
        elif kind == "status":
            idx, text = payload
            if 0 <= idx < len(self.items):
                it = self.items[idx]
                if it.ok is not None:
                    # 已经出结果的项：后处理器迟到的状态事件不许盖掉「✔ 完成」
                    return
                it.status = text
                self._refresh_row(idx)
        elif kind == "progress":
            idx, pct, speed, eta, label = payload
            if 0 <= idx < len(self.items):
                it = self.items[idx]
                it.progress = max(it.progress, min(100.0, pct))
                if label:
                    it.status = f"{label} {it.progress:.0f}%"
                self._refresh_row(idx)
                bits = [f"{it.title[:36] or it.url}", f"{it.progress:.0f}%"]
                if speed:
                    bits.append(human_speed(speed))
                if eta:
                    bits.append(f"剩余 {human_duration(eta)}")
                self.lbl_prog.configure(text="  ".join(bits))
                self.pb.configure(value=it.progress)
        elif kind == "row":
            self._refresh_row(payload[0])
            self._update_summary()
        elif kind == "overall":
            done, total = payload
            self.lbl_sum.configure(text=f"共 {total} 项 · 已处理 {done}")
        elif kind == "thumb":
            idx = payload[0]
            sel = self.tree.selection()
            if not sel:
                # 解析完没人事先选中某一行时，直接把封面亮出来。原先这里要求
                # 「该行必须已被选中」，于是封面下载完就被丢掉，面板一直停在占位符上。
                self.tree.selection_set(f"i{idx}")
                self._show_thumb(self.items[idx].thumb_bytes)
            elif int(sel[0][1:]) == idx:
                self._show_thumb(self.items[idx].thumb_bytes)
        elif kind == "cookie":
            ok, text = payload
            self._set_cookie_status(text, ok=ok)
            self._log(text)
        elif kind == "cookiefile":
            # Cookie 文件换了（从浏览器导入那条路）——Tk 变量只能在主线程碰
            self.var_cookie.set(payload[0])
            self._save_settings()
            threading.Thread(target=self._cookie_boot, args=(payload[0],), daemon=True).start()
        elif kind == "busy":
            self._set_busy(payload[0])
        elif kind == "done_all":
            mode = payload[0]
            self._update_summary()
            self.lbl_prog.configure(text="全部完成" if not self.cancel.is_set() else "已停止")
            if mode == "download":
                n_ok = sum(1 for it in self.items if it.ok is True)
                n_bad = sum(1 for it in self.items if it.ok is False)
                self._log(f"——— 本轮结束：成功 {n_ok}，失败 {n_bad} ———")


def main() -> int:
    # 高 DPI 必须在创建任何 Tk 窗口之前设置，否则界面在 125% 缩放下会发虚
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if yt_dlp is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            "没找到 yt-dlp。请先安装：\n\n    python -m pip install -U yt-dlp",
        )
        return 1
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
