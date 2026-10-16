# -*- coding: utf-8 -*-
"""生成 docs/screenshot-settings.png —— 用**演示配置**（不含真实 IP / 输出目录）。

🔴 必须 monkeypatch load_config：设置窗会从磁盘读 plan_scan_gui.json，
   那里面有真实仪器 IP 与输出目录历史，不拦就会把它们截进图里。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                    # noqa: E402
import promax_plan_gui as gui                            # noqa: E402

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

# 关键：拦掉磁盘读取
gui.load_config = lambda *a, **k: dict(DEMO)

root = tk.Tk()
app = gui.App(root, os.path.join(HERE, "CH"))
root.update()
app._open_settings()

# 多轮 update + sleep，让 notebook / 输入框把值渲染完
# （踩过：只 update 一次会抓到空白控件；那是时序问题，不是程序 bug）
for _ in range(40):
    root.update()
    time.sleep(0.05)

win = None
for w in root.winfo_children():
    if isinstance(w, tk.Toplevel):
        win = w
if win is None:
    print("!! 没找到设置窗")
    sys.exit(1)

win.update_idletasks()
win.update()
time.sleep(0.8)
for _ in range(30):
    root.update()
    time.sleep(0.05)

from PIL import ImageGrab                                 # noqa: E402
x, y = win.winfo_rootx(), win.winfo_rooty()
w, h = win.winfo_width(), win.winfo_height()
img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
out = os.path.join(HERE, "docs", "screenshot-settings.png")
img.save(out)
print("saved %s  %s" % (out, img.size))
root.destroy()
