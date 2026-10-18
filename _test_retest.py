# -*- coding: utf-8 -*-
"""「重测失败频道」测试：一轮跑完后只补测未成功的频道，结果合并回同一个文件。

覆盖：
  1. `_store_row`：正常追加 / 重测时按频道名**覆盖**（不产生重复行）
  2. `_collect_failures`：按 (表, 频道) 去重
  3. `_update_retest_btn`：无失败 / 有失败但无结果文件 / 正常可点，三种状态
  4. `_pg_reset_subset`：「全部」分母 = 要重测的频道数
  5. 集成（真跑模拟器，`--fail-first 99` 让全部频道锁不上）：
     ① 整表 12 个频道全 LOCKED=OFF → 按钮变「重测失败频道 (12)」
     ② 只留 3 个待办 → 重测 → 这 3 个变 ON、其余 9 个仍是 OFF，
        行数仍是 12（覆盖不重复）、顺序按原序号、结果文件被更新
     ③ 再点一次重测剩下 9 个 → 全 ON、按钮灰掉、日志有【重测完成】

用法：python _test_retest.py      （需要 2222 空闲；集成段自行起模拟器）
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                     # noqa: E402
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


# 等待/重试调小，测试才快（生产值是 0.3/3/0.3）
DEMO = {
    "window": {"width": 1240, "height": 780},
    "ip_history": ["127.0.0.1"], "last_ip": "127.0.0.1", "port": 2222,
    "out_dir": "", "out_dir_history": [], "settle": 0.15,
    "settle_ter": 0.1, "settle_sat": 0.5, "retry_ter": 1, "retry_sat": 1,
    "retry_step_ter": 0.1, "retry_step_sat": 0.5,
    "pause_between": True, "auto_reconnect": True, "reconnect_interval": 10.0,
    "export_format": "xlsx", "log_dir": "", "log_name": "promax_test.log",
    "log_timestamps": True, "log_timing": True, "log_cmd_timing": False,
    "log_separate": False, "presets": [],
}
gui.load_config = lambda *a, **k: dict(DEMO)


class Dialogs(object):
    """把 messagebox 换成记录器（模态框会卡住测试）"""

    def __init__(self):
        self.calls = []
        self.answer = True

    def askyesno(self, title, msg="", **k):
        self.calls.append(("askyesno", title, msg))
        return self.answer

    def showinfo(self, title, msg="", **k):
        self.calls.append(("showinfo", title, msg))

    def showwarning(self, title, msg="", **k):
        self.calls.append(("showwarning", title, msg))

    showerror = showwarning

    def last(self, kind):
        for c in reversed(self.calls):
            if c[0] == kind:
                return c
        return None


DLG = Dialogs()
gui.messagebox.askyesno = DLG.askyesno
gui.messagebox.showinfo = DLG.showinfo
gui.messagebox.showwarning = DLG.showwarning
gui.messagebox.showerror = DLG.showerror


def demo_only_dir():
    """只扫「复制了 DEMO-* 表」的隔离目录（详见 _test_highlight.py 的说明）。"""
    tmp = tempfile.mkdtemp(prefix="pmcontorl_rt_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


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


def start_mock(extra=None, timeout=8.0):
    p = subprocess.Popen([PY, MOCK, "--quiet"] + list(extra or []), cwd=HERE,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_busy():
            return p
        time.sleep(0.1)
    return p


def stop_mock(p):
    try:
        p.kill()
    except Exception:
        pass
    t0 = time.time()
    while port_busy() and time.time() - t0 < 6.0:
        time.sleep(0.1)


def pump_until(root, cond, timeout=180.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def wait_finished(root, app, timeout=180.0):
    """等后台线程跑完，再跑一会儿事件循环让 _poll → _finish_ui 收完尾。"""
    ok = pump_until(root, lambda: app.thread is not None
                    and not app.thread.is_alive(), timeout)
    if ok:
        t0 = time.time()
        while time.time() - t0 < 1.5:
            root.update()
            time.sleep(0.02)
    return ok


def row_of(items, name):
    for it in items:
        if it.name == name:
            return it
    return None


def only_select(app, item):
    """只勾选这一张表"""
    for k, v in app.check_vars.items():
        if k != item.key and v.get():
            app._toggle(k)
    if not app.check_vars[item.key].get():
        app._toggle(item.key)


def main():
    demo_dir = demo_only_dir()
    outdir = tempfile.mkdtemp(prefix="pmcontorl_rtout_")
    root = tk.Tk()
    mocks = []
    try:
        app = gui.App(root, demo_dir)
        root.update()
        bad = [it.file for it in app.items if not it.file.startswith("DEMO-")]
        if bad:
            raise SystemExit("!! 扫到非演示表，拒绝继续：%s" % bad)

        ter = [it for it in app.items if it.band == "TER"]
        ter.sort(key=lambda it: len(it.points()))
        it = ter[-1]                       # 频道最多的射频表（DVB-C，要锁定）
        n_pt = len(it.points())
        print("演示表：%s（%d 个频道）" % (it.name, n_pt))

        print("\n--- 1) _store_row：正常追加 / 重测覆盖")

        def mkrow(ch, idx, lock):
            return {"idx": idx, "band": "TER", "plan": it.name, "ch": ch,
                    "freq_mhz": "100.000", "signal": "DVB-C", "LOCKED": lock,
                    "attempts": 1, "POWER": "60", "POWER_unit": "dBuV"}

        app.rows = {}
        app.retesting = False
        app._store_row(it, mkrow("CH1", 1, "OFF"))
        app._store_row(it, mkrow("CH2", 2, "ON"))
        check("正常模式：两条不同频道都追加", len(app.rows[it.name]) == 2,
              str(len(app.rows[it.name])))
        app.retesting = True
        app._store_row(it, mkrow("CH1", 1, "ON"))
        lst = app.rows[it.name]
        check("重测模式：同频道名覆盖，不新增行", len(lst) == 2, str(len(lst)))
        check("重测模式：被覆盖的那行变成 ON",
              [r for r in lst if r["ch"] == "CH1"][0]["LOCKED"] == "ON")
        app.retesting = False
        app._store_row(it, mkrow("CH3", 3, "ON"))
        check("重测模式关掉后又恢复追加", len(app.rows[it.name]) == 3)

        print("\n--- 2) _collect_failures 去重")
        p1 = (100.0, "DVB-C", "CH1", None)
        p2 = (101.0, "DVB-C", "CH2", None)

        class FakeEng(object):
            failures = [(it, 1, p1, "未锁定"), (it, 1, p1, "未锁定"),
                        (it, 2, p2, "设置失败")]

        old_eng = app.engine
        app.engine = FakeEng()
        got = app._collect_failures()
        check("同一频道重复记录被去掉", len(got) == 2,
              str([g[2][2] for g in got]))
        check("保留下来的频道名正确",
              [g[2][2] for g in got] == ["CH1", "CH2"])

        print("\n--- 3) 按钮状态")
        app.out_path = ""
        app.thread = None
        app.pending_retest = []
        app._update_retest_btn()
        check("没有失败频道 → 灰",
              str(app.retest_btn.cget("state")) == "disabled"
              and app.retest_btn.cget("text") == "重测失败频道",
              "%s / %s" % (app.retest_btn.cget("state"),
                           app.retest_btn.cget("text")))
        app.pending_retest = got
        app._update_retest_btn()
        check("有失败但没结果文件 → 仍灰",
              str(app.retest_btn.cget("state")) == "disabled")
        app.out_path = os.path.join(outdir, "x.xlsx")
        app._update_retest_btn()
        check("有失败且有结果文件 → 可点，且带数量",
              str(app.retest_btn.cget("state")) == "normal"
              and app.retest_btn.cget("text") == "重测失败频道 (2)",
              "%s / %s" % (app.retest_btn.cget("state"),
                           app.retest_btn.cget("text")))
        app.engine = old_eng

        print("\n--- 4) _pg_reset_subset：分母是要重测的频道数")
        app._pg_reset_subset({it.key: [(1, p1), (2, p2)]})
        check("「全部」分母 = 2（不是整表 %d）" % n_pt,
              app.pg_all_total == 2, str(app.pg_all_total))
        check("「全部」显示 0/2 (0%%)",
              app.pb_all_lbl.get() == "0/2 (0%)", app.pb_all_lbl.get())
        check("「当前频道表」显示 —",
              app.pb_cur_lbl.get() == "—", app.pb_cur_lbl.get())

        # ---------------- 集成 ----------------
        print("\n--- 5) 集成：整表锁不上 → 出现待办")
        if port_busy():
            print("  [SKIP] 2222 被占用（可能有残留模拟器进程），跳过集成测试")
        else:
            out = os.path.join(outdir, "retest_demo.xlsx")
            app.rows = {}
            app.ip_var.set("127.0.0.1")
            app.port_var.set("2222")
            only_select(app, it)            # ⚠️ 先选表：_toggle 会按预设重算文件名
            app.out_var.set(out)            # 再定输出路径，别被自动命名盖掉
            root.update()

            mocks.append(start_mock(["--fail-first", "99"]))
            app.start()
            ok = wait_finished(root, app)
            check("第 1 轮跑完", ok)
            print("     引擎记录失败=%d  待办=%d  输出=%s"
                  % (len(getattr(app.engine, "failures", []) or []),
                     len(app.pending_retest), app.out_path))
            rows = app.rows.get(it.name, [])
            check("整表 %d 行都在（未锁定也写行）" % n_pt, len(rows) == n_pt,
                  str(len(rows)))
            check("全部 LOCKED=OFF",
                  all(str(r.get("LOCKED")).upper() != "ON" for r in rows),
                  {r.get("LOCKED") for r in rows})
            check("待办 = %d 个频道" % n_pt, len(app.pending_retest) == n_pt,
                  str(len(app.pending_retest)))
            check("按钮变「重测失败频道 (%d)」且可点" % n_pt,
                  app.retest_btn.cget("text") == "重测失败频道 (%d)" % n_pt
                  and str(app.retest_btn.cget("state")) == "normal",
                  "%s / %s" % (app.retest_btn.cget("text"),
                               app.retest_btn.cget("state")))
            check("日志里有【未成功】提示",
                  "【未成功】" in app.log.get("1.0", "end"))

            print("\n--- 6) 重测全部 %d 个：全部锁定、行不重复" % n_pt)
            stop_mock(mocks.pop())
            mocks.append(start_mock([]))        # 这次不再注入失败
            DLG.calls.clear()
            app.retest_failed()
            ok = wait_finished(root, app)
            check("重测轮跑完", ok)
            rows = app.rows.get(it.name, [])
            on = [r["ch"] for r in rows
                  if str(r.get("LOCKED")).upper() == "ON"]
            check("行数仍是 %d（按频道名覆盖，不重复）" % n_pt,
                  len(rows) == n_pt, str(len(rows)))
            check("全部 %d 个都变 ON" % n_pt, len(on) == n_pt, str(len(on)))
            idxs = [int(r["idx"]) for r in rows]
            check("保存前按原序号排序", idxs == sorted(idxs), str(idxs))
            check("待办清空", app.pending_retest == [],
                  str(len(app.pending_retest)))
            check("按钮灰掉、文字回到无数量",
                  str(app.retest_btn.cget("state")) == "disabled"
                  and app.retest_btn.cget("text") == "重测失败频道",
                  "%s / %s" % (app.retest_btn.cget("state"),
                               app.retest_btn.cget("text")))
            check("日志有【重测完成】",
                  "【重测完成】" in app.log.get("1.0", "end"))
            check("弹了「重测完成」提示",
                  DLG.last("showinfo") is not None
                  or DLG.last("showwarning") is not None,
                  str([c[:2] for c in DLG.calls]))
            check("重测期间没弹「打开文件吗」那种窗",
                  all(c[1] != "完成" for c in DLG.calls),
                  [c[:2] for c in DLG.calls])

            print("\n--- 7) 结果文件已更新（%d 行 + 表头）" % n_pt)
            real = app.out_path or out
            print("     输出文件：%s" % real)
            try:
                from openpyxl import load_workbook
                ws = load_workbook(real, read_only=True).active
                check("xlsx 里 %d 行数据（不含表头）" % n_pt,
                      ws.max_row - 1 == n_pt, str(ws.max_row - 1))
            except ImportError:
                print("  [SKIP] 没装 openpyxl")

            print("\n--- 8) 重测后仍然失败 → 留在待办里（可以再补测）")
            # 挑 2 个频道造一份待办，模拟器再次注入"锁不上"
            rows = app.rows.get(it.name, [])
            two = rows[:2]
            app.pending_retest = [
                (it, int(r["idx"]),
                 (float(r["freq_mhz"]), r["signal"], r["ch"],
                  r.get("symb_rate") or None),
                 "模拟：未锁定") for r in two]
            app._update_retest_btn()
            stop_mock(mocks.pop())
            mocks.append(start_mock(["--fail-first", "99"]))
            DLG.calls.clear()
            app.retest_failed()
            ok = wait_finished(root, app)
            check("补测轮跑完", ok)
            rows = app.rows.get(it.name, [])
            check("行数仍是 %d" % n_pt, len(rows) == n_pt, str(len(rows)))
            check("这 2 个又变回 OFF",
                  sum(1 for r in rows
                      if str(r.get("LOCKED")).upper() == "ON") == n_pt - 2,
                  str(sum(1 for r in rows
                          if str(r.get("LOCKED")).upper() == "ON")))
            check("失败的 2 个留在待办", len(app.pending_retest) == 2,
                  str(len(app.pending_retest)))
            check("按钮变成「(2)」",
                  app.retest_btn.cget("text") == "重测失败频道 (2)",
                  app.retest_btn.cget("text"))
            check("弹的是「仍未成功」警告",
                  DLG.last("showwarning") is not None
                  and "仍未成功" in DLG.last("showwarning")[2],
                  str(DLG.last("showwarning")))

        print("\n=====================================")
        print("通过 %d   失败 %d" % (OK, BAD))
        return 1 if BAD else 0
    finally:
        for m in mocks:
            stop_mock(m)
        try:
            root.destroy()
        except Exception:
            pass
        shutil.rmtree(demo_dir, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
