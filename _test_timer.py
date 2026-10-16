# -*- coding: utf-8 -*-
"""计时器测试：顶栏「用时 HH:MM:SS」，**暂停时同步停表**。

需求：状态栏（「设置」右侧）显示本轮测试用时；断线挂起 / 换线暂停期间不计时。

覆盖：
  1. 初始显示 —（还没跑过）
  2. 起表：立刻 00:00:00，随时间增长
  3. 暂停：值冻结（等 1s 不变），文本带「已暂停」
  4. 继续：接着涨，且**暂停的那段没被算进去**
  5. MSG_NETLOST / MSG_NETOK（断线挂起 → 重连成功）走真实 _handle
  6. MSG_CONFIRM（换线暂停）弹窗：点「继续」恢复计时 / 点「中止测试」保持停表
  7. _finish_ui 收尾：定格、写日志、撤「已暂停」
  8. _fmt_hms 边界
  9. 集成：真跑模拟器（两张射频表 + 表间暂停），中途停 1.2s 点继续
     → 最终用时 ≈ 墙钟 − 暂停时间

用法：python _test_timer.py        （需要 2222 空闲；集成段自行起模拟器）
"""
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                     # noqa: E402
from tkinter import ttk                                   # noqa: E402
import promax_plan_gui as gui                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "mock_promax_server.py")
PY = sys.executable

OK = 0
BAD = 0


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % name)
    else:
        BAD += 1
        print("  [FAIL] %s   %s" % (name, detail))


DEMO = {
    "window": {"width": 1240, "height": 780},
    "ip_history": ["127.0.0.1"], "last_ip": "127.0.0.1", "port": 2222,
    "out_dir": "", "out_dir_history": [], "settle": 0.15,
    "settle_ter": 0.3, "settle_sat": 3.0, "retry_ter": 3, "retry_sat": 5,
    "retry_step_ter": 0.3, "retry_step_sat": 6.0,
    "pause_between": True, "auto_reconnect": True, "reconnect_interval": 10.0,
    "export_format": "xlsx", "log_dir": "", "log_name": "promax_test.log",
    "log_timestamps": True, "log_timing": True, "log_cmd_timing": False,
    "log_separate": False, "presets": [],
}
gui.load_config = lambda *a, **k: dict(DEMO)


def demo_only_dir():
    """只扫「复制了 DEMO-* 表」的隔离目录（详见 _test_highlight.py 的说明）。"""
    tmp = tempfile.mkdtemp(prefix="pmcontorl_tm_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def pump(root, seconds):
    """跑一会儿事件循环（after 定时刷新要靠它推进）"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.02)


def parse_used(txt):
    """'用时 00:01:02（已暂停）' → 62.0；非「用时」文本返回 None"""
    if not txt.startswith("用时 "):
        return None
    body = txt.split("（")[0].strip()[len("用时 "):]
    try:
        h, m, s = [int(x) for x in body.split(":")]
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def find_dialog(root, title="更换接口"):
    for w in root.winfo_children():
        try:
            if isinstance(w, tk.Toplevel) and w.title() == title:
                return w
        except Exception:
            pass
    return None


def click(win, text):
    """在窗口里递归找按钮并点击（返回是否点到）"""
    for w in win.winfo_children():
        try:
            if isinstance(w, ttk.Button) and w.cget("text") == text:
                w.invoke()
                return True
        except Exception:
            pass
        if click(w, text):
            return True
    return False


class FakeEngine(object):
    """只给 _confirm 用：记录用户点了什么"""

    def __init__(self):
        self.answers = []

    def resolve(self, v):
        self.answers.append(v)


def port_busy(port=2222):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run_engine_integration(root, app, items):
    """第 9 组：真跑模拟器（表间暂停开），中途停 1.2s 点「继续」。"""
    print("\n--- 9) 集成：真跑模拟器，暂停期间真的不计时")
    if port_busy():
        print("  [SKIP] 2222 被占用（可能有残留模拟器进程），跳过集成测试")
        return
    mock = subprocess.Popen([PY, MOCK, "--quiet"], cwd=HERE,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.2)
        app.log.configure(state="normal")
        app.log.delete("1.0", "end")
        app.log.configure(state="disabled")

        q = queue.Queue()
        eng = gui.TestEngine(q, 2222)
        app.engine = eng
        th = threading.Thread(
            target=eng.run,
            args=("127.0.0.1", items, dict(DEMO), 20.0, True),   # 表间暂停开
            daemon=True)
        app._timer_start()
        wall0 = time.time()
        th.start()

        st = {"done": False, "confirm": 0, "pause_at": None, "resume": None,
              "rows": 0}

        def click_continue():
            win = find_dialog(root)
            if win is not None and click(win, "继续"):
                st["resume"] = time.time()

        def pump_msgs():
            while True:
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    break
                if kind == gui.TestEngine.MSG_CONFIRM:
                    st["confirm"] += 1
                    st["pause_at"] = time.time()
                app._handle(kind, payload)
                if kind == gui.TestEngine.MSG_CONFIRM:
                    # 模拟"用户接线"花了 1.2 秒
                    root.after(1200, click_continue)
                elif kind == gui.TestEngine.MSG_ROW:
                    st["rows"] += 1
            if th.is_alive():
                root.after(30, pump_msgs)
            else:
                st["done"] = True

        root.after(30, pump_msgs)
        t0 = time.time()
        while not st["done"] and time.time() - t0 < 300:
            root.update()
            time.sleep(0.02)
        th.join(timeout=10)
        wall = time.time() - wall0                 # 墙钟：含暂停
        used = app._timer_stop()                   # 净用时：不含暂停
        root.update()

        check("测试期间弹出了换线暂停窗", st["confirm"] == 1, str(st["confirm"]))
        check("弹窗点「继续」成功恢复", st["resume"] is not None)
        frozen = parse_used(app.timer_var.get())
        time.sleep(1.0)
        root.update()
        check("停表后 1s 内数字不变（定格）",
              parse_used(app.timer_var.get()) == frozen,
              "%s → %s" % (frozen, app.timer_var.get()))
        pause_s = (st["resume"] - st["pause_at"]) if st["resume"] else 0.0
        check("净用时 ≈ 墙钟 − 暂停时长（±0.4s）",
              abs(used - (wall - pause_s)) < 0.4,
              "净用时=%.2fs 墙钟=%.2fs 暂停=%.2fs 期望=%.2fs"
              % (used, wall, pause_s, wall - pause_s))
        check("结果行数 = 两张表的频道数之和", st["rows"] == sum(
            len(it.points()) for it in items),
            "rows=%s" % st["rows"])
    finally:
        try:
            mock.kill()
        except Exception:
            pass


def main():
    demo_dir = demo_only_dir()
    root = tk.Tk()
    try:
        app = gui.App(root, demo_dir)
        root.update()
        bad = [it.file for it in app.items if not it.file.startswith("DEMO-")]
        if bad:
            raise SystemExit("!! 扫到非演示表，拒绝继续：%s" % bad)

        print("\n--- 1) 初始状态")
        check("顶栏初始显示「用时 —」（还没跑过）",
              app.timer_var.get() == "用时 —", app.timer_var.get())
        check("计时器初始不在跑", app._t_on is False and app._t_t0 is None)

        print("\n--- 2) 起表")
        app._timer_start()
        root.update()
        check("起表立刻显示 用时 00:00:00",
              app.timer_var.get() == "用时 00:00:00", app.timer_var.get())
        pump(root, 1.15)
        v1 = parse_used(app.timer_var.get())
        check("1.15s 后到 1 秒（顶栏由 after 定时刷新）", v1 == 1, str(v1))

        print("\n--- 3) 暂停：值冻结 + 文本标注")
        app._timer_pause()
        root.update()
        check("暂停后文本带「（已暂停）」",
              app.timer_var.get().endswith("（已暂停）"), app.timer_var.get())
        acc0 = app._t_acc
        frozen = parse_used(app.timer_var.get())
        pump(root, 1.2)
        check("暂停 1.2s 内数值不变",
              parse_used(app.timer_var.get()) == frozen
              and abs(app._t_acc - acc0) < 1e-9,
              "%s → %s" % (frozen, app.timer_var.get()))

        print("\n--- 4) 继续：接着涨，暂停那段没算进去")
        app._timer_resume()
        root.update()
        check("恢复后不再带「已暂停」",
              not app.timer_var.get().endswith("（已暂停）"))
        pump(root, 1.1)
        v2 = parse_used(app.timer_var.get())
        check("继续 1.1s 后只涨到 2 秒（暂停的 1.2s 未计入）", v2 == 2, str(v2))

        print("\n--- 5) 断线挂起 → 重连成功（真实 _handle）")
        app._handle(gui.TestEngine.MSG_NETLOST, "模拟断线")
        root.update()
        check("MSG_NETLOST 后停表", app.timer_var.get().endswith("（已暂停）"),
              app.timer_var.get())
        net0 = parse_used(app.timer_var.get())
        pump(root, 1.1)
        check("断线等待期间数值不变", parse_used(app.timer_var.get()) == net0)
        app._handle(gui.TestEngine.MSG_NETOK, 1)
        root.update()
        check("MSG_NETOK 后恢复计时",
              app._t_t0 is not None
              and not app.timer_var.get().endswith("（已暂停）"))
        pump(root, 1.1)
        check("重连后继续增长",
              parse_used(app.timer_var.get()) > net0,
              "%s → %s" % (net0, app.timer_var.get()))

        print("\n--- 6) 换线暂停弹窗：继续 / 中止")
        app.engine = FakeEngine()
        app._handle(gui.TestEngine.MSG_CONFIRM, "请把线换到下一颗卫星")
        root.update()
        win = find_dialog(root)
        check("弹出了「更换接口」窗", win is not None)
        check("弹窗期间停表", app.timer_var.get().endswith("（已暂停）"),
              app.timer_var.get())
        c0 = parse_used(app.timer_var.get())
        pump(root, 1.1)
        check("等用户接线时数值不变", parse_used(app.timer_var.get()) == c0)
        clicked = click(win, "继续") if win is not None else False
        root.update()
        check("点「继续」→ engine.resolve(True) 且恢复计时",
              clicked and app.engine.answers == [True] and app._t_t0 is not None,
              "answers=%s" % app.engine.answers)

        app._handle(gui.TestEngine.MSG_CONFIRM, "请把线换到下一颗卫星")
        root.update()
        win = find_dialog(root)
        click(win, "中止测试") if win is not None else None
        root.update()
        check("点「中止测试」→ resolve(False) 且保持停表",
              app.engine.answers[-1] is False and app._t_t0 is None,
              "answers=%s" % app.engine.answers)

        print("\n--- 7) 收尾定格")
        used = app._timer_stop()
        root.update()
        check("_timer_stop 返回已累计秒", used > 0, "%.2f" % used)
        check("收尾后不再「已暂停」且数字定格",
              not app.timer_var.get().endswith("（已暂停）"),
              app.timer_var.get())
        app.engine = None
        app.rows = {}
        app.out_path = os.path.join(HERE, "_tm_tmp_nosave.xlsx")
        app.log_path = None
        app.sep_log_path = None
        app._finish_ui()
        root.update()
        check("_finish_ui 后停表（_t_on=False）", app._t_on is False)
        check("日志里有【用时】一行",
              "【用时】" in app.log.get("1.0", "end"),
              app.log.get("1.0", "end")[-200:])
        check("顶栏仍显示最终用时（不是「—」）",
              parse_used(app.timer_var.get()) is not None,
              app.timer_var.get())

        print("\n--- 8) _fmt_hms 边界")
        for sec, want in ((0, "00:00:00"), (59, "00:00:59"), (60, "00:01:00"),
                          (3599, "00:59:59"), (3600, "01:00:00"),
                          (3661, "01:01:01"), (-5, "00:00:00")):
            check("%ss → %s" % (sec, want), gui._fmt_hms(sec) == want,
                  gui._fmt_hms(sec))

        # 集成：两张射频表（快），表间暂停开 → 用它验证"暂停不计时"
        ter = [it for it in app.items if it.band == "TER"]
        ter.sort(key=lambda it: len(it.points()))
        picked = ter[:2]
        for it in picked:
            if app.check_vars[it.key].get() == 0:
                app._toggle(it.key)
        run_engine_integration(root, app, picked)

        print("\n=====================================")
        print("通过 %d   失败 %d" % (OK, BAD))
        return 1 if BAD else 0
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        shutil.rmtree(demo_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
