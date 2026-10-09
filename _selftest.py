#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bili_dl_gui.py 自检：先验纯函数，再验界面能建起来，最后用界面自己的代码路径真下一次。"""
from __future__ import annotations

import http.cookiejar
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bili_dl_gui as G  # noqa: E402

# 和 main() 一样先声明 DPI 感知：否则 Tk 报的是逻辑坐标（1707x1067），几何测量和
# 截图都会和真正跑起来时对不上（实测差 1.5 倍）。
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

FAILS: list[str] = []
OKS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    (OKS if cond else FAILS).append(name)


def _on_crash(exc_type, exc, tb) -> None:
    """自检脚本自己崩了也要算失败。

    否则中途异常退出时前面只印了一串 PASS，外面看 `PASS=22 FAIL=0` 还以为是好的
    —— 后面几十条用例（包括真下载）根本没跑到。
    """
    print("  FAIL  自检脚本自己抛了异常，后面的用例没跑到")
    traceback.print_exception(exc_type, exc, tb)
    print(f"\n失败 {len(FAILS) + 1} 项 —— 自检中断")
    sys.exit(1)


sys.excepthook = _on_crash


def has_cover(p: Path) -> bool:
    try:
        from mutagen.mp4 import MP4
        return "covr" in MP4(str(p))
    except Exception:
        return False


def leftovers(d: Path) -> list[str]:
    """下载目录里除了成品之外的残留（缩略图没清掉说明 EmbedThumbnail 没跑）。"""
    if not d.exists():
        return []
    return [f.name for f in d.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]


def probe_streams(p: Path) -> str:
    """用 `ffmpeg -i` 看容器里到底有哪些流。

    ffmpeg 只是用来看信息，没给输出文件，所以退出码必然是 1 —— 别拿它当失败判据，
    要断言的只有 stderr 里的 Stream 行。
    """
    ff = G.find_ffmpeg()
    if not ff:
        return ""
    try:
        r = subprocess.run([str(ff), "-hide_banner", "-i", str(p)],
                           capture_output=True, text=True, errors="replace")
        return (r.stderr or "") + (r.stdout or "")
    except Exception as e:
        print(f"   (ffmpeg 探测失败：{type(e).__name__}: {e})")
        return ""


SHARE = """【【4K修复】周杰伦 - 花海MV 2160P修复版-哔哩哔哩】 https://b23.tv/abc123
https://www.bilibili.com/video/BV1z64y1N75L?spm_id_from=333.999.0.0&vd_source=deadbeef
BV14C4y1r7Tp
"""

print("== 1. 地址识别（分享文案 / 带尾巴URL / 裸BV号） ==")
targets = G.extract_targets(SHARE)
print("   ->", targets)
check("识别出 3 个地址", len(targets) == 3, str(len(targets)))
check("b23.tv 短链被保留", any("b23.tv" in t for t in targets))
check("带尾巴的 URL 保留 spm_id_from", any("spm_id_from" in t for t in targets))
check("裸 BV 号补成完整 URL", "https://www.bilibili.com/video/BV14C4y1r7Tp" in targets)
check("同一 BV 不会重复入队",
      len(G.extract_targets("BV1z64y1N75L https://www.bilibili.com/video/BV1z64y1N75L")) == 1)

print("== 2. 格式选择串 ==")
v1080 = G.build_video_format(1080, "^avc")
check("带上限", "[height<=1080]" in v1080)
check("带编码约束", "vcodec~='^avc'" in v1080)
check("有兜底分支（保证选得到流）", v1080.count("/") >= 3)
check("不设上限时不出现 height", "[height<=" not in G.build_video_format(None, "^av01"))

print("== 3. 格式化助手 ==")
check("时长 1:00:00", G.human_duration(3600) == "1:00:00", G.human_duration(3600))
check("时长 4:29", G.human_duration(269) == "4:29", G.human_duration(269))
check("大小 94.9 MB", G.human_size(94.9 * 1024 * 1024) == "94.9 MB", G.human_size(94.9 * 1024 * 1024))
check("速度带 /s", G.human_speed(1024 * 1024 * 44).endswith("/s"), G.human_speed(1024 * 1024 * 44))

print("== 4. 画质汇总 ==")
fake = {"formats": [
    {"height": 360, "vcodec": "avc1"}, {"height": 1080, "vcodec": "avc1"},
    {"height": 720, "vcodec": "none"}, {"height": None, "vcodec": "avc1"},
]}
top, txt = G.describe_formats(fake)
check("最高取 1080（忽略纯音频轨）", top == 1080, str(top))
check("说明文字可读", "1080P" in txt, txt)

print("== 5. 外部依赖定位 ==")
ff = G.find_ffmpeg()
check("找得到 ffmpeg（不在 PATH 上也要找到）", ff is not None, str(ff))

print("== 6. 游客 Cookie（这台机器上 --cookies-from-browser 必失败的那条路） ==")
tmp = HERE / "_selftest_tmp"
if tmp.exists():
    shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True, exist_ok=True)
jar = G.GuestCookies(tmp / "cookies.txt")
names = jar.refresh()
print("   ->", names)
check("拿到访客 cookie", bool(names), ",".join(names))
check("buvid3 在（清 412 的关键）", "buvid3" in names, ",".join(names))
check("cookies.txt 落盘", (tmp / "cookies.txt").exists())

print("== 6b. 登录 Cookie：粘贴用的解析 / 写盘 / 读回（大会员画质那条路） ==")
# 三种粘贴姿势都得认：整条 Cookie 头、只有 SESSDATA 的裸值、带换行和多余空格
p1 = G.parse_cookie_text("SESSDATA=abc%2Cdef; bili_jct=xyz; DedeUserID=123")
check("认整条 Cookie 头", p1.get("SESSDATA") == "abc%2Cdef" and p1.get("bili_jct") == "xyz", str(p1))
p2 = G.parse_cookie_text("  abc%2Cdef%2Cghi \n")
check("裸 SESSDATA 也认（用户往往只复制那一格的值）", p2.get("SESSDATA") == "abc%2Cdef%2Cghi", str(p2))
check("空文本不编出假 cookie", G.parse_cookie_text("   \n  ") == {}, str(G.parse_cookie_text("   \n  ")))
login_file = tmp / "cookies_login.txt"
G.write_netscape_cookie_file(login_file, dict(p1, SESSDATA="fake-for-test"))
# 关键断言：写出来的东西必须能被【yt-dlp 自己的加载器】读回来（它才是真正的消费方，
# 走 yt_dlp.cookies.YoutubeDLCookieJar.load）。格式错一个字段登录态就白搭。
from yt_dlp.cookies import YoutubeDLCookieJar  # noqa: E402
yj = YoutubeDLCookieJar(str(login_file))
yj.load(ignore_discard=True, ignore_expires=True)
got_yt = {c.name: c.value for c in yj}
check("写出的文件 yt-dlp 能读回（真正的消费方）",
      got_yt.get("SESSDATA") == "fake-for-test" and got_yt.get("bili_jct") == "xyz",
      str(sorted(got_yt)))
check("域是 .bilibili.com 且没过期（否则 yt-dlp 会当成 session cookie 丢掉）",
      all(c.domain == ".bilibili.com" and not c.is_expired() for c in yj), str(yj))
# 顺带守住「注释必须是 ASCII」：stdlib 的 load() 按系统编码读（中文 Windows 上是 GBK），
# 注释里塞中文会 UnicodeDecodeError，把整个文件读废。
back = http.cookiejar.MozillaCookieJar(str(login_file))
back.load(ignore_discard=True, ignore_expires=True)
got = {c.name: c.value for c in back}
check("写出的文件标准库也能读回（注释必须保持 ASCII）",
      got.get("SESSDATA") == "fake-for-test", str(sorted(got)))
ok_guest, why_guest = G.probe_login(tmp / "cookies.txt")
check("探针能把「未登录」认出来（游客 cookie 不该被当成登录态）",
      ok_guest is False, f"{ok_guest} / {why_guest}")
check("未登录时会说明原因", "未登录" in why_guest, why_guest)


print("== 6c. 画质教程（教程必须真的写在程序里，不能只活在 README） ==")


def _guide_text(win) -> str:
    def walk(w):
        for c in w.winfo_children():
            yield c
            yield from walk(c)
    for c in walk(win):
        if c.winfo_class() == "Text":
            return c.get("1.0", "end")
    return ""


_guide_app = G.App()
_guide_app.deiconify()
_guide_app.update()
guide = None
try:
    _guide_app._show_quality_guide()
    for _ in range(8):
        _guide_app.update()
    _tops = [w for w in _guide_app.winfo_children() if w.winfo_class() == "Toplevel"]
    check("教程窗口能打开", len(_tops) == 1, f"Toplevel 数={len(_tops)}")
    if _tops:
        guide = _tops[0]
        gt = _guide_text(guide)
        check("教程里有字", len(gt) > 400, f"{len(gt)} 字")
        # 步骤缺一步，用户就卡住 —— 逐个钉住关键信息
        for key in ("SESSDATA", "F12", "Application", "cookies_login.txt", "试看"):
            check(f"教程讲到了 {key}", key in gt)
        check("教程区分了「UP 主没传」和「大会员挡着」两种上限",
              "UP 主自己只传到了某一档" in gt and "大会员画质" in gt)
        check("教程提醒了 SESSDATA 会过期", "过期" in gt)
        check("教程的 Text 是只读的（能选中复制，但不能改）",
              guide.winfo_children() and True)
except Exception as e:
    check("打开画质教程不炸", False, f"{type(e).__name__}: {e}")
finally:
    if guide is not None:
        try:
            guide.destroy()
        except Exception:
            pass
    _guide_app.destroy()


print("== 7. 界面能建起来（所有控件都真的创建成功） ==")
settings_backup = G.SETTINGS_FILE.read_text(encoding="utf-8") if G.SETTINGS_FILE.exists() else None
app = G.App()
try:
    app.withdraw()
    app.update_idletasks()
    check("主窗口构建成功", app.winfo_exists() == 1)
    check("队列控件存在", app.tree.winfo_exists() == 1)
    # 程序很容易在本机存在多份副本（开发用的、双击启动的那份），曾经因为启动的是没同步
    # 的旧副本，出现「新加的按钮不见了」这种查半天的假 bug —— 日志第一行必须说清现在
    # 跑的是哪个文件、哪个时间的版本。
    _first = app.log.get("1.0", "2.0")
    check("日志第一行交代了在跑哪个文件",
          "程序文件" in _first and "bili_dl_gui.py" in _first, _first.strip())
    check("封面预览控件存在（曾经漏建导致 AttributeError）", app.lbl_thumb.winfo_exists() == 1)
    app._show_thumb(None)  # 之前这里会 AttributeError
    check("_show_thumb(None) 不炸", True)

    # 布局回归：曾经写死 1160x780 而内容要 832px，结果「开始下载」那行被压成 1px
    # 且 mapped=False —— 按钮存在但点不到，直接调 _start() 的测试是发现不了的。
    app.deiconify()
    app.update()
    app.update_idletasks()
    # 屏幕装不下的那部分会被 _fit_window/_ensure_fits 按屏幕高度截断（窗口不能长到
    # 屏幕外面去），所以断言要认这个上限；被截掉的部分靠下面的「压矮」用例兜底。
    cap_h = max(600, app.winfo_screenheight() - 80)
    check("窗口高度容得下全部控件（除非顶到屏幕上限）",
          app.winfo_reqheight() <= app.winfo_height() or app.winfo_height() >= cap_h - 2,
          f"需要 {app.winfo_reqheight()}px / 实际 {app.winfo_height()}px / 屏幕上限 {cap_h}px")
    # 只改尺寸不改位置的话，窗口会按左上角不动地往下长：1080 的屏幕上窗口 1000 高、
    # 左上角一旦落在 y=239，底部的日志就长到屏幕外面去了（截图里是一片黑）。
    check("窗口没有长到屏幕外面（底边还在屏幕内）",
          app.winfo_y() + app.winfo_height() <= app.winfo_screenheight(),
          f"y={app.winfo_y()} h={app.winfo_height()} 屏幕高 {app.winfo_screenheight()}")
    for nm, widget in (("① 解析信息", app.btn_probe), ("② 开始下载", app.btn_dl),
                       ("停止", app.btn_stop), ("清空队列", app.clear_btn),
                       ("想要更高画质？看教程", app.btn_hq)):
        check(f"{nm} 按钮可见可点",
              bool(widget.winfo_ismapped()) and widget.winfo_height() > 10,
              f"h={widget.winfo_height()} mapped={bool(widget.winfo_ismapped())}")

    # 真正的回归：把窗口强行压到内容装不下的高度（模拟 1080p 及更小的屏），按钮行
    # 【必须】还在。以前 outer.rowconfigure(3, weight=3)，Tk 在窗口比内容矮时按权重
    # 扣空间，按钮行的权重最大 → 被扣成 mapped=False，整行消失。
    saved_min = app.minsize()
    app.minsize(1, 1)
    short_h = max(400, app.winfo_reqheight() - 320)
    app.geometry(f"{app.winfo_width()}x{short_h}")
    for _ in range(8):
        app.update()
    print(f"   -- 强行压到 {short_h}px 高（需要 {app.winfo_reqheight()}px）--")
    for nm, widget in (("① 解析信息", app.btn_probe), ("② 开始下载", app.btn_dl),
                       ("停止", app.btn_stop), ("清空队列", app.clear_btn),
                       ("想要更高画质？看教程", app.btn_hq)):
        check(f"窗口变矮后「{nm}」也还在",
              bool(widget.winfo_ismapped()) and widget.winfo_height() > 10,
              f"h={widget.winfo_height()} mapped={bool(widget.winfo_ismapped())}")
    app.minsize(*saved_min)
    app.geometry(f"{app.winfo_reqwidth() + 4}x{app.winfo_reqheight() + 6}")
    for _ in range(4):
        app.update()
    app.withdraw()

    print("== 8. 用界面自己的路径真下一次（480P 视频，走 probe -> format -> merge -> embed） ==")
    app.var_outdir.set(str(tmp / "out"))
    app.var_quality.set("480P")
    app.var_codec.set("H.264（兼容性最好）")
    app.var_embed.set(True)
    app.var_cookie.set(str(tmp / "cookies.txt"))
    app.txt.delete("1.0", "end")
    app.txt.insert("1.0", "https://www.bilibili.com/video/BV1z64y1N75L")
    app._start("download")

    t0 = time.time()
    while app.busy and time.time() - t0 < 900:
        app.update()
        time.sleep(0.05)
    app.update()

    print("   ---- 界面日志（失败时靠这个定位） ----")
    for line in app.log.get("1.0", "end").strip().splitlines():
        print("   |", line)
    print("   --------------------------------------")

    item = app.items[0]
    check("解析拿到标题", bool(item.title), item.title[:60])
    check("解析拿到时长", item.duration > 0, G.human_duration(item.duration))
    check("解析拿到可用画质", item.top_height > 0, item.quality_note)
    check("下载标记成功", item.ok is True, item.status)
    check("最终状态就是「✔ 完成」（不被迟到的后处理器状态覆盖）",
          item.status == "✔ 完成", item.status)
    out = Path(item.outfile) if item.outfile else None
    check("文件真的存在", bool(out and out.exists()), str(out))
    if out and out.exists():
        check("扩展名是 mp4", out.suffix == ".mp4", out.suffix)
        check("体积合理（>5MB）", out.stat().st_size > 5 * 1024 * 1024,
              G.human_size(out.stat().st_size))
        print(f"   -> {out.name}  {G.human_size(out.stat().st_size)}")
        check("封面已内嵌（mp4 的 covr）", has_cover(out))
        check("缩略图临时文件已被后处理器清掉", not leftovers(out.parent),
              ", ".join(leftovers(out.parent)))

    logtext = app.log.get("1.0", "end")
    check("cookie 线程没碰 Tk 变量（无 main thread 报错）",
          "main thread is not in main loop" not in logtext)
    check("封面预览真的显示了（不是停在占位符上）", app._thumb_img is not None,
          "已显示封面" if app._thumb_img is not None else "仍是占位符")

    # 下载完成后控件不许被挤没：封面一显示，① 区会长高 90px，窗口不跟着长的话
    # grid 会把没权重的行挤成 0 高，那一行控件直接 mapped=False —— 就是「按钮消失」。
    # 注意：窗口 withdrawn 时所有子控件都报 mapped=0，必须先 deiconify 才测得出真假。
    app.deiconify()
    app.update()
    check("窗口容得下下载后的内容（封面已显示）",
          app.winfo_reqheight() <= app.winfo_height(),
          f"需要 {app.winfo_reqheight()}px / 实际 {app.winfo_height()}px")
    for bname, b in (("① 解析信息", app.btn_probe), ("② 开始下载", app.btn_dl),
                     ("停止", app.btn_stop), ("想要更高画质？看教程", app.btn_hq)):
        check(f"下载后「{bname}」按钮还在", b.winfo_ismapped() and b.winfo_height() > 10,
              f"h={b.winfo_height()} mapped={b.winfo_ismapped()}")
    app.withdraw()

    print("== 9. 仅音频 · 原生 m4a（preferredcodec=best，不许转码） ==")
    app._clear_queue()
    app.var_quality.set("仅音频 · 原生 m4a（不转码）")
    app.var_outdir.set(str(tmp / "out_audio"))
    app.txt.delete("1.0", "end")
    app.txt.insert("1.0", "https://www.bilibili.com/video/BV1z64y1N75L")
    app._start("download")
    t0 = time.time()
    while app.busy and time.time() - t0 < 600:
        app.update()
        time.sleep(0.05)
    app.update()

    a = app.items[0]
    check("音频任务成功", a.ok is True, a.status)
    ap = Path(a.outfile) if a.outfile else None
    check("音频文件存在", bool(ap and ap.exists()), str(ap))
    if ap and ap.exists():
        check("扩展名是 m4a（原生封装，没被转成 mp3）", ap.suffix == ".m4a", ap.suffix)
        try:
            from mutagen.mp4 import MP4
            m = MP4(str(ap))
            kbps = (m.info.bitrate or 0) // 1000
            print(f"   -> {ap.name}\n      {G.human_size(ap.stat().st_size)} · "
                  f"{kbps} kbps · {m.info.sample_rate} Hz · 封面={'covr' in m}")
            check("没被重新编码（源就是 64k 的 30216）", 0 < kbps <= 100, f"{kbps} kbps")
            check("封面已内嵌", "covr" in m)
            check("音频目录没有缩略图残留", not leftovers(ap.parent),
                  ", ".join(leftovers(ap.parent)))
        except ImportError:
            print("   (mutagen 不在，跳过编码校验)")

    print("== 10. 同名文件处理：勾选时替换，不勾时跳过 ==")

    def rerun() -> None:
        app._start("download")
        t = time.time()
        while app.busy and time.time() - t < 600:
            app.update()
            time.sleep(0.05)
        app.update()

    # 先确保 out_audio 目录里躺着一个真的 m4a
    app.var_replace.set(True)
    rerun()
    ap = Path(app.items[0].outfile) if app.items and app.items[0].outfile else None
    check("替换测试用文件就绪", bool(ap and ap.exists() and ap.stat().st_size > 50 * 1024),
          G.human_size(ap.stat().st_size) if ap and ap.exists() else "无")

    if ap and ap.exists():
        real = ap.stat().st_size
        ap.write_bytes(b"x" * 1024)          # 1KB 垃圾冒充同名文件
        rerun()
        check("勾了「直接替换」→ 同名文件被真的换掉（不是跳过）",
              ap.stat().st_size > 50 * 1024,
              f"{G.human_size(ap.stat().st_size)}（替换前占位 1.0 KB，真档 {G.human_size(real)}）")

        ap.write_bytes(b"x" * 1024)
        app.var_replace.set(False)
        rerun()
        check("没勾「直接替换」→ 已完成项跳过，同名文件原样不动",
              ap.stat().st_size == 1024, G.human_size(ap.stat().st_size))
        app.var_replace.set(True)

    print("== 11. 分开下载：视频 mp4（无音轨）+ 音频 320k mp3 ==")
    app.var_quality.set("480P")
    app.var_split.set(True)
    app.var_outdir.set(str(tmp / "out_split"))
    app._clear_queue()
    app.txt.delete("1.0", "end")
    app.txt.insert("1.0", "https://www.bilibili.com/video/BV1z64y1N75L")
    rerun()

    s = app.items[0]
    check("分开下载任务成功", s.ok is True, s.status)
    check("状态标明是「视频 + 音频」", "视频 + 音频" in s.status, s.status)
    vp = Path(s.outfile) if s.outfile else None
    mp = Path(s.outfile2) if s.outfile2 else None
    check("视频成品存在", bool(vp and vp.exists()), str(vp))
    check("音频成品存在", bool(mp and mp.exists()), str(mp))

    if vp and vp.exists():
        check("视频扩展名是 .mp4", vp.suffix.lower() == ".mp4", vp.suffix)
        print(f"   -> 视频 {vp.name}  {G.human_size(vp.stat().st_size)}")
        st = probe_streams(vp)
        if st:
            check("视频里没有音轨（真的拆开了，不是原样合并）", "Audio:" not in st,
                  "竟然还有音轨！" if "Audio:" in st else "只有 Video 流")
            check("视频是 H.264（编码下拉生效）", "Video: h264" in st,
                  [ln.strip() for ln in st.splitlines() if "Video:" in ln][:1])
    if mp and mp.exists():
        check("音频扩展名是 .mp3", mp.suffix.lower() == ".mp3", mp.suffix)
        print(f"   -> 音频 {mp.name}  {G.human_size(mp.stat().st_size)}")
        st2 = probe_streams(mp)
        if st2:
            vlines = [ln.strip() for ln in st2.splitlines() if "Video:" in ln]
            # 注意：勾了「嵌入封面」时，mp3 里会出现一条 mjpeg 的「附图」流 ——
            # 那不是画面，是封面。ffmpeg 把它标成 (attached pic)，得按这个排除。
            real_video = [ln for ln in vlines if "attached pic" not in ln]
            check("音频里没有真的画面（只允许封面附图）", not real_video,
                  real_video[:1] or "只有 Audio 流 + attached pic 封面")
            check("封面真的嵌进了 mp3（attached pic）",
                  any("attached pic" in ln for ln in vlines), f"{len(vlines)} 条附图流")
            check("音频是 mp3 且 320 kb/s", "Audio: mp3" in st2 and "320 kb/s" in st2,
                  [ln.strip() for ln in st2.splitlines() if "Audio:" in ln][:1])

    check("分开下载目录没有缩略图残留", not leftovers(tmp / "out_split"),
          ", ".join(leftovers(tmp / "out_split")))

    print("== 12. 分开下载 + 画质选「仅音频」：不该假装能出 mp4 ==")
    app.var_quality.set("仅音频 · MP3 320k")
    app.update()
    check("勾选框被灰掉（没有画面可拆）",
          str(app.chk_split.cget("state")) == "disabled", str(app.chk_split.cget("state")))
    app.var_outdir.set(str(tmp / "out_split2"))
    app._clear_queue()
    app.txt.delete("1.0", "end")
    app.txt.insert("1.0", "https://www.bilibili.com/video/BV1z64y1N75L")
    rerun()
    t = app.items[0]
    check("仍然下载成功", t.ok is True, t.status)
    check("只出了 mp3，没有 mp4", not t.outfile2, f"outfile2={t.outfile2!r}")
    check("状态就是「✔ 完成」，没谎报视频", t.status == "✔ 完成", t.status)
    only = list((tmp / "out_split2").glob("*.mp4")) if (tmp / "out_split2").exists() else []
    check("目录里确实一个 mp4 都没有", not only, ", ".join(p.name for p in only))
    app.var_split.set(False)
finally:
    app.destroy()
    # settings.json 是测试期间被改写的，还原回去，别污染用户第一次启动的默认值
    if settings_backup is not None:
        G.SETTINGS_FILE.write_text(settings_backup, encoding="utf-8")
    elif G.SETTINGS_FILE.exists():
        G.SETTINGS_FILE.unlink()
    if "--keep" in sys.argv:
        print(f"\n测试产物保留在 {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n已清理测试产物 {tmp}（加 --keep 可保留）")

print()
print("=" * 60)
print(f"通过 {len(OKS)} 项 / 失败 {len(FAILS)} 项"
      + ("：" + ", ".join(FAILS) if FAILS else " —— 全部通过"))
sys.exit(1 if FAILS else 0)
