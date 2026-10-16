# -*- coding: utf-8 -*-
"""生成 README 用的两张界面截图（**全部用演示数据**，不含真实 IP / 频道表 / 输出目录）。

  docs/pmcontorl_main.png         主界面（已应用演示预设）
  docs/screenshot-settings.png    设置界面

🔴 四道防线，缺一不可：
  1. monkeypatch load_config —— 界面会自己从磁盘读 plan_scan_gui.json，
     那里面有**真实仪器 IP** 与真实输出目录历史。
  2. 扫描**只含 DEMO-* 演示表的临时目录** —— 直接扫 CH/ 会把本地真实频道表
     （真实运营商表 / 真实卫星表名）列进界面并截进图里。
     真实表虽被 .gitignore 排除，**截图一公开就等于泄露**。（踩过）
  3. 截主界面前断言「所有表名都以 DEMO- 开头」，不满足直接报错退出。
  4. 截图前 `assert_no_local_user()` 扫界面上所有文字，
     出现本机用户名（形如 `D:\\<用户名>\\Downloads`）就拒绝截图。
     输出目录框默认读注册表的真实下载文件夹，只设文件名框拦不住。（踩过）
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                     # noqa: E402
import promax_plan_gui as gui                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

DEMO = {
    "window": {"width": 1240, "height": 780},
    "ip_history": ["127.0.0.1", "192.168.1.50"],
    "last_ip": "127.0.0.1",
    "port": 2222,
    "out_dir": "", "out_dir_history": [],
    "settle": 0.15,
    "settle_ter": 0.3, "settle_sat": 3.0,
    "retry_ter": 3, "retry_sat": 5,
    "retry_step_ter": 0.3, "retry_step_sat": 6.0,
    "pause_between": True,
    "auto_reconnect": True,
    "reconnect_interval": 10.0,
    "export_format": "xlsx",
    "log_dir": "", "log_name": "promax_test.log",
    "log_timestamps": True, "log_timing": True, "log_cmd_timing": False,
    "log_separate": False,
    "presets": [
        {"name": "射频数字_DEMO", "filename": "射频数字_DEMO_{date8}",
         "files": ["DEMO-Digital"], "pause": False},
        {"name": "射频全频道_DEMO", "filename": "射频全频道_DEMO_{date8}",
         "files": ["DEMO-Digital", "DEMO-Analog"], "pause": False},
        {"name": "卫星C波段_DEMO", "filename": "卫星C波段_DEMO_{date8}",
         "files": ["DEMO-C-Band"], "pause": True},
        {"name": "卫星Ku波段_DEMO", "filename": "卫星Ku波段_DEMO_{date8}",
         "files": ["DEMO-Ku-Band"], "pause": True},
        {"name": "混合演示_DEMO", "filename": "混合演示_DEMO_{date8}",
         "files": ["DEMO-Digital", "DEMO-C-Band"], "pause": True},
    ],
}

# 防线 1：拦掉磁盘配置
gui.load_config = lambda *a, **k: dict(DEMO)


def demo_only_dir():
    """把 CH/{TER,SAT} 里的 **DEMO-* 演示表**复制到一个临时目录，只扫它。

    这样界面里只会出现演示表，不会把本地真实频道表名截进图里。
    """
    tmp = tempfile.mkdtemp(prefix="pmcontorl_shot_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if not os.path.isdir(src):
            continue
        for f in os.listdir(src):
            if f.startswith("DEMO-"):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def settle(root, n=30, delay=0.05):
    """多轮 update + sleep 让控件渲染完。

    ⚠️ 只 update 一次会抓到空白控件（输入框还没填值）—— 那是**时序**问题，
    不是程序 bug；值一直在，多转几轮就稳定了。
    """
    for _ in range(n):
        root.update()
        time.sleep(delay)


def _collect_texts(w, acc):
    """递归收集控件上的文字（text / textvariable / Listbox 行）。"""
    for c in w.winfo_children():
        try:
            t = c.cget("text")
            if t:
                acc.append(str(t))
        except Exception:
            pass
        try:
            v = c.cget("textvariable")
            if v:
                try:
                    val = c.getvar(v)
                except Exception:
                    val = ""
                if val:
                    acc.append(str(val))
        except Exception:
            pass
        try:                        # Listbox / Text
            if c.winfo_class() == "Listbox":
                acc += [str(c.get(i)) for i in range(c.size())]
            elif c.winfo_class() == "Text":
                acc.append(str(c.get("1.0", "end")))
        except Exception:
            pass
        _collect_texts(c, acc)
    return acc


def assert_no_local_user(root):
    """防线 4：截图前扫界面上所有文字，出现**本机用户名**就拒绝截图。

    ⚠️ 起因：`outdir_var` 默认走 `default_out_dir()` 从注册表读**真实下载文件夹**
    （形如 `D:\\<本机用户名>\\Downloads`），只把文件名框设成演示值是拦不住的。
    这种泄漏就是路径中间一段，人眼看图极易漏掉，所以交给断言把关。
    """
    import getpass
    names = {n.lower() for n in (os.environ.get("USERNAME"),
                                 getpass.getuser()) if n}
    if not names:
        return
    hit = []
    for t in _collect_texts(root, []):
        if any(n in t.lower() for n in names):
            hit.append(t)
    hit = sorted(set(hit))
    if hit:
        raise SystemExit("!! 界面文字里出现本机用户名，拒绝截图：%s" % hit)


_SCALE = [None]


def screen_scale(root):
    """Tk 坐标 → 物理像素的缩放系数。

    ⚠️ Tk 的 winfo_* 返回**逻辑像素**，ImageGrab 按**物理像素**工作。
    本机 150% 缩放（逻辑 2048×1280 / 物理 3072×1920）时，
    直接拿 Tk 坐标去 grab 会整体偏移、把桌面截进来（踩过）。
    用「全屏抓图宽度 ÷ Tk 报告的屏宽」拿到真实比例。
    """
    if _SCALE[0] is None:
        from PIL import ImageGrab
        _SCALE[0] = ImageGrab.grab().size[0] / float(root.winfo_screenwidth())
        print("屏幕缩放系数：%.3f" % _SCALE[0])
    return _SCALE[0]


def grab(win, rel, root):
    """抓窗口区域的截图。

    🔴 必须先把窗口**提到最前**（lift + topmost + focus_force）：
    `ImageGrab` 抓的是**屏幕**上那块矩形，不是窗口自身渲染的内容 ——
    窗口被别的窗口盖住时会稳稳截到别人的界面（踩过）。
    """
    from PIL import ImageGrab
    win.update_idletasks()
    win.lift()
    win.attributes("-topmost", True)
    win.focus_force()
    win.update_idletasks()
    win.update()
    time.sleep(0.6)
    s = screen_scale(root)
    x = int(win.winfo_rootx() * s)
    y = int(win.winfo_rooty() * s)
    w = int(win.winfo_width() * s)
    h = int(win.winfo_height() * s)
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    win.attributes("-topmost", False)
    out = os.path.join(HERE, rel)
    img.save(out)
    print("saved %s  %s  (bbox=%d,%d,%dx%d)" % (out, img.size, x, y, w, h))


def main(which=("main", "settings", "progress")):
    """生成 README 用的界面截图。

    ⚠️ 必须放在 `if __name__ == "__main__"` 里执行：本模块的函数会被别的脚本
    import 复用，顶层代码若直接跑，import 一下就把 docs/ 下的图静默重写了（踩过）。
    """
    demo_dir = demo_only_dir()
    try:
        root = tk.Tk()
        app = gui.App(root, demo_dir)
        settle(root)

        # 防线 3：真要截图了，先确认扫描结果全是演示表
        files = [it.file for it in app.items]
        bad = [f for f in files if not f.startswith("DEMO-")]
        if bad:
            root.destroy()
            raise SystemExit("!! 扫描到非演示表，拒绝截图（会泄露真实数据）：%s" % bad)
        if not files:
            root.destroy()
            raise SystemExit("!! 没扫到任何演示表，检查 CH/ 下有没有 DEMO-*.XML")
        print("扫描到的表：%s" % ", ".join(files))

        # ---- 1) 主界面：应用一个演示预设，让界面有内容 ----
        if "main" in which:
            app._apply_preset(app.cfg["presets"][0])
            # 输出路径用中性演示值（别把本机用户名 / 下载目录截进去）
            # ⚠️ outdir_var 也必须设：它默认走注册表读**真实下载文件夹**
            #   （形如 D:\<本机用户名>\Downloads），只设文件名框是不够的。（踩过）
            app.outdir_var.set(r"D:\Demo\Downloads")
            app.out_var.set(r"D:\Demo\Downloads\射频数字_DEMO_20261015.xlsx")
            settle(root, 40)
            assert_no_local_user(root)          # 防线 4
            grab(root, os.path.join("docs", "pmcontorl_main.png"), root)

        # ---- 2) 设置界面 ----
        if "settings" in which:
            app._open_settings()
            settle(root, 40)

            win = None
            for w in root.winfo_children():
                if isinstance(w, tk.Toplevel):
                    win = w
            if win is None:
                root.destroy()
                raise SystemExit("!! 没找到设置窗")
            assert_no_local_user(root)          # 防线 4
            grab(win, os.path.join("docs", "screenshot-settings.png"), root)

        # ---- 3) 「已选频道表」进度高亮（三种状态同框）----
        if "progress" in which:
            app._toggle_all()                   # 4 张演示表全勾上
            keys = list(app.check_vars.keys())
            it0, it1 = app._by_key(keys[0]), app._by_key(keys[1])
            app.sel_done, app.sel_active = set(), None
            # 手工推一轮进度：第 1 张已测完（绿 ✓）、第 2 张正在测（琥珀 ▶）、后两张排队（白）
            # 消息走的是与真实测试同一条路径（MSG_PLAN_START / MSG_PLAN_DONE）
            app._handle(gui.TestEngine.MSG_PLAN_START, it0)
            app._handle(gui.TestEngine.MSG_PLAN_DONE, (it0, []))
            app._handle(gui.TestEngine.MSG_PLAN_START, it1)
            app.outdir_var.set(r"D:\Demo\Downloads")
            app.out_var.set(r"D:\Demo\Downloads\混合演示_DEMO_20261016.xlsx")
            settle(root, 40)
            assert_no_local_user(root)          # 防线 4
            grab(root, os.path.join("docs", "screenshot-progress.png"), root)

        root.destroy()
    finally:
        shutil.rmtree(demo_dir, ignore_errors=True)


if __name__ == "__main__":
    main()