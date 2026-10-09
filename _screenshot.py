"""把界面真跑起来、真解析一次，然后整屏截图（不做 bbox 裁剪，避免裁歪）。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bili_dl_gui as G  # noqa: E402

SHOTS = HERE / "screenshots"
SHOT = SHOTS / "main.png"
# --download 时存到另一个名字，别把「刚解析」那张覆盖掉
SHOT_DL = SHOTS / "download.png"
SHOT_SPLIT = SHOTS / "split.png"
SHOT_GUIDE = SHOTS / "guide.png"

# 界面上「保存到」那一栏会连同目录名一起被拍进图里，所以刻意用一个中性的演示路径，
# 而不是开发机上的真目录。想换成自己的目录就设环境变量 BILI_SHOT_DIR。
SCRATCH = Path(os.environ.get("BILI_SHOT_DIR", str(Path.home() / "Downloads" / "B站视频")))


def pump(app, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.update()
        time.sleep(0.05)


def main() -> int:
    # 截图会改 settings.json（换 outdir / 画质），跑完必须还原，否则污染用户保存的选项。
    settings_backup = G.SETTINGS_FILE.read_text(encoding="utf-8") if G.SETTINGS_FILE.exists() else None
    # 和 main() 一样先声明 DPI 感知：否则 Tk 报逻辑坐标(1707x1067)、ImageGrab 抓物理
    # 像素(2560x1600)，裁剪框会按 1.5 倍错位，截出来的图看着像界面被切了。
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    want_dl = "--download" in sys.argv
    want_split = "--split" in sys.argv
    want_guide = "--guide" in sys.argv
    SHOTS.mkdir(exist_ok=True)
    app = G.App()
    app.var_outdir.set(str(SCRATCH))
    if want_dl or want_split:
        # 截图脚本会真下载，所以画质压到 480P，下完再删掉，别留垃圾
        app.var_quality.set("480P")
        if want_split:
            app.var_split.set(True)
            app.update()
    app.txt.delete("1.0", "end")
    app.txt.insert("1.0", "https://www.bilibili.com/video/BV1z64y1N75L")
    app.update()
    pump(app, 0.4)

    app._start("download" if (want_dl or want_split) else "probe")
    t0 = time.time()
    while app.busy and time.time() - t0 < 600:
        app.update()
        time.sleep(0.05)
    pump(app, 1.0)

    # 日志第一行是「程序文件 <真路径>」，Cookie 那一栏也是真路径 —— 给文档拍图时
    # 统统换成中性的演示值。放在下载**之后**改，下载用的 cookie 早就快照过了，不影响。
    _log = app.log.get("1.0", "end").replace(str(HERE), r"D:\B站视频下载器")
    app.log.configure(state="normal")
    app.log.delete("1.0", "end")
    app.log.insert("1.0", _log)
    app.log.configure(state="disabled")
    app.var_cookie.set(str(Path(SCRATCH).parent / "B站视频下载器" / "cookies.txt"))

    app.lift()
    app.attributes("-topmost", True)
    app.focus_force()
    pump(app, 1.5)

    # --guide：把「画质教程」那个弹窗也拍一张（它是 transient 弹窗，得单独取它的几何）
    target = app
    if want_guide:
        app._show_quality_guide()
        pump(app, 1.2)
        tops = [w for w in app.winfo_children() if w.winfo_class() == "Toplevel"]
        if tops:
            target = tops[0]
            target.lift()
            target.attributes("-topmost", True)
            pump(app, 0.8)

    from PIL import ImageGrab

    target.update_idletasks()
    target.update()
    x, y = target.winfo_rootx(), target.winfo_rooty()
    w, h = target.winfo_width(), target.winfo_height()
    print(f"窗口几何 {w}x{h} @ ({x},{y})  屏幕 {app.winfo_screenwidth()}x{app.winfo_screenheight()}")
    print(f"  窗口底边 {y + h} / 屏幕高 {app.winfo_screenheight()}"
          f"  日志框 mapped={app.log.winfo_ismapped()} h={app.log.winfo_height()}")
    # 顺手确认按钮没被挤掉（图看得见，坐标也留一份）
    for bn in ("btn_probe", "btn_dl", "btn_stop", "btn_hq"):
        b = getattr(app, bn)
        print(f"  {bn}: mapped={b.winfo_ismapped()} h={b.winfo_height()} x={b.winfo_rootx()}")
    out = SHOT_GUIDE if want_guide else (SHOT_SPLIT if want_split else (SHOT_DL if want_dl else SHOT))
    img = ImageGrab.grab(bbox=(x - 12, y - 48, x + w + 12, y + h + 12))
    img.save(out)
    print(f"已保存 {out}  {img.size[0]}x{img.size[1]}")
    for it in app.items:
        print(f"  队列项: {it.title[:50]!r} | {G.human_duration(it.duration)} | {it.quality_note}")
        if it.outfile2:
            print(f"  产出: 视频 {it.outfile}")
            print(f"        音频 {it.outfile2}")
    time.sleep(0.2)
    if want_dl or want_split:
        # 只删这次下出来的文件，不删目录本身（SCRATCH 可能是用户自己的目录）
        for it in app.items:
            for p in (it.outfile, it.outfile2):
                if p:
                    try:
                        Path(p).unlink()
                    except OSError:
                        pass
    app.destroy()
    if settings_backup is not None:
        G.SETTINGS_FILE.write_text(settings_backup, encoding="utf-8")
    elif G.SETTINGS_FILE.exists():
        G.SETTINGS_FILE.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
