# -*- coding: utf-8 -*-
"""测试进度反馈的行为测试：频点日志带 `(n/总数)` + 中间区域两条进度条。

覆盖：
  1. 初始：两条都显示 `—`（没跑过，不该显示 0/0）
  2. 「全部」总数 = 所选表的**频点总数**（而不是表的张数）
  3. PLAN_START → 「当前频道表」总数换成这张表的频点数、进度归零
  4. 每个 MSG_ROW → 两条各前进一格
  5. 「全部」按**频道数加权**：跑完小表不等于 50%
  6. PLAN_DONE → 本表补齐到 100%（有个别频点失败不发 ROW 的情况）
  7. 下一张表 START → 当前条归零，但「全部」继续累加
  8. 频点日志格式 `[  3/20]`；总数未知时退化成 `[  3]`（不出现 `[3/0]`）
  9. 集成：真起模拟器跑 DEMO 表，结束时两条都满格、日志分母正确

不需要模拟器的部分（1~8）纯消息层驱动，第 9 组才需要 mock。

用法：python _test_progress.py
"""
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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
    tmp = tempfile.mkdtemp(prefix="pmcontorl_pg_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def bar_val(pb):
    return float(pb.cget("value"))


def log_text(app):
    return app.log.get("1.0", "end")


def fake_row(idx, name="CH"):
    """造一条 MSG_ROW 的 row（GUI 侧只用到 idx/ch/freq_mhz/LOCKED 与显示项）"""
    return {"idx": idx, "band": "TER", "plan": name, "ch": "CH%d" % idx,
            "freq_mhz": "%.3f" % (100 + idx), "signal": "DVB-C",
            "LOCKED": "ON", "symb_rate": "", "attempts": 1,
            "POWER": "68.2", "POWER_unit": "dBuV", "MER": "32.1", "MER_unit": "dB",
            "tune_s": 0.1, "wait_s": 0.1, "total_s": 0.2}


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


def start_mock():
    return subprocess.Popen([PY, MOCK, "--quiet"], cwd=HERE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def run_engine_integration(root, app, items):
    """第 9 组：真起模拟器跑选中的表，验证两条进度条最终满格、日志分母正确。"""
    print("\n--- 9) 集成：真跑模拟器，两条进度条跑到满格")
    if port_busy():
        print("  [SKIP] 2222 被占用（可能有残留模拟器进程），跳过集成测试")
        return
    mock = start_mock()
    try:
        time.sleep(1.2)
        app._pg_reset(items)
        # 清掉前几组造日志，只留本次真实测试的日志（否则分母会混入手工造的数据）
        app.log.configure(state="normal")
        app.log.delete("1.0", "end")
        app.log.configure(state="disabled")
        q = queue.Queue()
        eng = gui.TestEngine(q, 2222)
        th = threading.Thread(
            target=eng.run,
            args=("127.0.0.1", items, dict(DEMO), 20.0, False),
            daemon=True)
        th.start()

        finished = [None]
        row_msgs = [0]

        def pump():
            while True:
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    break
                if kind == gui.TestEngine.MSG_ROW:
                    row_msgs[0] += 1
                app._handle(kind, payload)
            if th.is_alive():
                root.after(30, pump)
            else:
                finished[0] = "done"

        root.after(30, pump)
        t0 = time.time()
        while finished[0] is None and time.time() - t0 < 300:
            root.update()
            time.sleep(0.02)
        th.join(timeout=10)

        want_all = sum(len(it.points()) for it in items)
        check("「全部」总数 = 所选表频点数之和",
              app.pg_all_total == want_all,
              "%s vs %s" % (app.pg_all_total, want_all))
        check("「全部」跑到 100%%",
              abs(app.pg_all_done - want_all) == 0
              and abs(bar_val(app.pb_all) - 100.0) < 1e-6,
              "done=%s/%s bar=%.1f"
              % (app.pg_all_done, app.pg_all_total, bar_val(app.pb_all)))
        check("「当前频道表」跑到 100%%",
              app.pg_cur_done == app.pg_cur_total
              and abs(bar_val(app.pb_cur) - 100.0) < 1e-6,
              "%s/%s bar=%.1f"
              % (app.pg_cur_done, app.pg_cur_total, bar_val(app.pb_cur)))
        # 日志里每条频点都应带 `[  n/总数]` 形式，且 n 从 1 连续到总数
        txt = log_text(app)
        # ⚠️ 只认**频点行**：表级日志是 `>>> [1/1] 表名 (...)`，若用宽松正则会把
        #    「第 1 张表 / 共 1 张表」也算进来，分母混入表的张数。频点行的特征是
        #    后面紧跟频道名与 `MHz`，用这个把两者区分开。
        tags = re.findall(r"\[ *(\d+)/(\d+)\][^\n]*?MHz", txt)
        n_this = len(items[0].points())
        check("日志里出现带总数的频点标号（如 [  1/%d]）" % n_this,
              bool(tags), "tags=%d" % len(tags))
        check("日志分母恒等于当前表的频点数 %d" % n_this,
              bool(tags) and all(int(b) == n_this for _, b in tags),
              str(sorted({int(b) for _, b in tags})))
        # 每个频点在日志里有**两行**（引擎侧耗时行 + GUI 侧测量值行，原有设计），
        # 所以序号集合去重后应恰好是 1..n
        seen = sorted({int(a) for a, _ in tags})
        check("频点序号覆盖 1..%d 无跳号" % n_this,
              seen == list(range(1, n_this + 1)), str(seen))
        check("没有出现 `[n/0]` 这种退化分母",
              not any(b == "0" for _, b in tags),
              [t for t in tags if t[1] == "0"][:3])
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

        # 按频点数选两张差异明显的 TER 表 + 一张 SAT，方便验证"按频道数加权"
        ter = [it for it in app.items if it.band == "TER"]
        sat = [it for it in app.items if it.band == "SAT"]
        ter.sort(key=lambda it: len(it.points()))
        small = ter[0]
        big = ter[-1]
        picked = [small, big] + sat[:1]
        for it in picked:
            if app.check_vars[it.key].get() == 0:
                app._toggle(it.key)
        root.update()
        n_small, n_big = len(small.points()), len(big.points())
        print("演示表：小表 %s=%d 频点 / 大表 %s=%d 频点"
              % (small.name, n_small, big.name, n_big))

        print("\n--- 1) 初始状态")
        check("「当前频道表」初始显示 —（不是 0/0）",
              app.pb_cur_lbl.get() == "—", app.pb_cur_lbl.get())
        check("「全部」初始显示 —",
              app.pb_all_lbl.get() == "—", app.pb_all_lbl.get())
        check("两条初始 value=0",
              bar_val(app.pb_cur) == 0 and bar_val(app.pb_all) == 0)

        print("\n--- 2) 「全部」总数按**频道数**合计（不是表的张数）")
        app._pg_reset(picked)
        root.update()
        want = n_small + n_big + len(sat[0].points())
        check("全部总数 = 各表频点数之和 %d（表张数只有 %d）"
              % (want, len(picked)),
              app.pg_all_total == want,
              "实际=%s" % app.pg_all_total)
        check("归零后「全部」显示 0/%d (0%%)（已知总数，不该是 —）" % want,
              app.pb_all_lbl.get() == "0/%d (0%%)" % want,
              app.pb_all_lbl.get())
        check("「当前频道表」仍显示 —（还不知道会测哪张表）",
              app.pb_cur_lbl.get() == "—", app.pb_cur_lbl.get())

        print("\n--- 3) 第 1 张表开始（小表 %d 频点）" % n_small)
        app._handle(gui.TestEngine.MSG_PLAN_START, small)
        root.update()
        check("当前条总数 = 本表频点数",
              app.pg_cur_total == n_small, str(app.pg_cur_total))
        check("当前条归零", app.pg_cur_done == 0 and bar_val(app.pb_cur) == 0)
        check("当前条标签 0/%d (0%%)" % n_small,
              app.pb_cur_lbl.get() == "0/%d (0%%)" % n_small,
              app.pb_cur_lbl.get())

        print("\n--- 4) 每来一条频点结果，两条各前进一格")
        for k in range(1, n_small + 1):
            app._handle(gui.TestEngine.MSG_ROW, (small, fake_row(k)))
        root.update()
        check("当前条 %d/%d" % (n_small, n_small),
              app.pg_cur_done == n_small and bar_val(app.pb_cur) == 100.0,
              "%s %s" % (app.pg_cur_done, bar_val(app.pb_cur)))
        check("全部条已走 %d（按频道数，不是 1/3 张表）" % n_small,
              app.pg_all_done == n_small, str(app.pg_all_done))
        exp = 100.0 * n_small / want
        check("全部条比例 = %d/%d" % (n_small, want),
              abs(bar_val(app.pb_all) - exp) < 0.05,
              "%.2f vs %.2f" % (bar_val(app.pb_all), exp))
        check("跑完**小**表不等于 50%%（加权生效）",
              abs(bar_val(app.pb_all) - 50.0) > 1.0,
              "%.1f%%" % bar_val(app.pb_all))

        print("\n--- 5) 本表收尾（PLAN_DONE）")
        app._handle(gui.TestEngine.MSG_PLAN_DONE, (small, [{}] * n_small))
        root.update()
        check("当前条保持满格", bar_val(app.pb_cur) == 100.0)
        check("全部条不被重复累加",
              app.pg_all_done == n_small, str(app.pg_all_done))

        print("\n--- 6) 下一张表 START → 当前条归零、「全部」继续累加")
        app._handle(gui.TestEngine.MSG_PLAN_START, big)
        root.update()
        check("当前条总数换成大表 %d" % n_big,
              app.pg_cur_total == n_big, str(app.pg_cur_total))
        check("当前条归零", bar_val(app.pb_cur) == 0.0)
        check("全部条保留在小表的进度",
              app.pg_all_done == n_small, str(app.pg_all_done))

        print("\n--- 7) 有个别频点失败（不发 ROW）→ PLAN_DONE 时补齐")
        for k in range(1, n_big):            # 只发 n_big-1 条，模拟最后一个频点失败
            app._handle(gui.TestEngine.MSG_ROW, (big, fake_row(k)))
        root.update()
        check("当前条差一格", app.pg_cur_done == n_big - 1,
              "%s/%s" % (app.pg_cur_done, n_big))
        app._handle(gui.TestEngine.MSG_PLAN_DONE, (big, [{}] * (n_big - 1)))
        root.update()
        check("收尾后当前条补满",
              app.pg_cur_done == n_big and bar_val(app.pb_cur) == 100.0,
              "%s %.1f" % (app.pg_cur_done, bar_val(app.pb_cur)))
        check("补齐的差值同步进了「全部」",
              app.pg_all_done == n_small + n_big, str(app.pg_all_done))

        print("\n--- 8) 频点日志格式")
        e = gui.TestEngine(queue.Queue(), 2222)
        e._pt_total = 20
        check("序号带总数：[  3/20]",
              e._ptag(3) == "[  3/20]", repr(e._ptag(3)))
        e._pt_total = 0
        check("总数未知时退化：[  3]（不出现 /0）",
              e._ptag(3) == "[  3]", repr(e._ptag(3)))
        # GUI 侧同样规则
        app.pg_cur_total = 0
        app._handle(gui.TestEngine.MSG_ROW, (big, fake_row(1)))
        root.update()
        check("GUI 日志在总数未知时不显示 /0",
              "[  1/0]" not in log_text(app), "见日志")
        app.pg_cur_total = 12
        app._handle(gui.TestEngine.MSG_ROW, (big, fake_row(1)))
        root.update()
        check("GUI 日志带 [  1/12]",
              "[  1/12]" in log_text(app), "见日志")

        run_engine_integration(root, app, picked[:1])

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
