"""复现「下载完成后 ② 开始下载 按钮消失」——下载前/下载后各拍一次按钮几何。"""
import sys
import time
import shutil
from pathlib import Path

try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
import bili_dl_gui as G  # noqa: E402

TMP = Path(__file__).parent / "_probe_after_tmp"
URL = "https://www.bilibili.com/video/BV1z64y1N75L"

BTNS = ("btn_probe", "btn_dl", "btn_stop")


def dump(app, tag):
    app.update_idletasks()
    app.update()
    act = app.btn_probe.master
    print(f"\n---- {tag} ----")
    print(f"  窗口 {app.winfo_width()}x{app.winfo_height()}  "
          f"需要 {app.winfo_reqwidth()}x{app.winfo_reqheight()}")
    print(f"  按钮行 act 实际宽 {act.winfo_width()}  需要宽 {act.winfo_reqwidth()}")
    for n in BTNS:
        b = getattr(app, n, None)
        if b is None:
            print(f"  {n}: 属性不存在")
            continue
        print(f"  {n}: mapped={b.winfo_ismapped()} viewable={b.winfo_viewable()} "
              f"x={b.winfo_x()} w={b.winfo_width()} h={b.winfo_height()} "
              f"state={b.cget('state')!r} reqw={b.winfo_reqwidth()}")
    print(f"  汇总标签文本 = {app.lbl_sum.cget('text')!r}  "
          f"宽 {app.lbl_sum.winfo_width()} mapped={app.lbl_sum.winfo_ismapped()}")

    print("  -- outer 每一行 --")
    outer = act.master
    info = outer.grid_slaves()
    rows = {}
    for w in info:
        r = int(w.grid_info()["row"])
        rows.setdefault(r, []).append(w)
    for r in sorted(rows):
        for w in rows[r]:
            txt = ""
            try:
                txt = str(w.cget("text"))[:22]
            except Exception:
                pass
            print(f"     row{r:<2} {w.winfo_class():<12} reqh={w.winfo_reqheight():<4} "
                  f"h={w.winfo_height():<4} y={w.winfo_y():<5} mapped={w.winfo_ismapped()} "
                  f"weight={outer.grid_rowconfigure(r)['weight']} {txt!r}")


def shot(app, tag: str) -> None:
    """连图一起拍：光看坐标不够，得肉眼确认按钮真的在屏幕上。"""
    try:
        from PIL import ImageGrab
        app.update_idletasks()
        app.update()
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        d = Path(__file__).parent / "screenshots"
        d.mkdir(exist_ok=True)
        p = d / f"probe_{tag}.png"
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(p)
        print(f"  截图 -> {p.name}  ({w}x{h})")
    except Exception as e:
        print(f"  截图失败（不影响结论）: {type(e).__name__}: {e}")


def run_download(app):
    app.txt.delete("1.0", "end")
    app.txt.insert("1.0", URL)
    app._start("download")
    t0 = time.time()
    while app.busy and time.time() - t0 < 600:
        app.update()
        time.sleep(0.05)
    for _ in range(40):          # 让队列里剩下的 UI 事件跑完
        app.update()
        time.sleep(0.02)


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    app = G.App()
    app.var_outdir.set(str(TMP))
    app.var_quality.set("480P")
    app.deiconify()
    app.update()
    dump(app, "① 下载前")

    run_download(app)
    dump(app, "② 第一次下载后")
    shot(app, "after")

    print("\n文件:", [p.name for p in TMP.glob("*")])

    # 再来一次同一个视频 —— 同名文件已存在
    for it in app.items:
        it.ok = None
    run_download(app)
    dump(app, "③ 第二次（同名）下载后")
    shot(app, "after2")
    print("文件:", [p.name for p in TMP.glob("*")])

    app.destroy()
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
