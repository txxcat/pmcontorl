# -*- coding: utf-8 -*-
"""「已选频道表」进度高亮的行为测试（不需要模拟器，纯消息层驱动）。

验证 GUI 里右侧「已选频道表」列表能标出**正在测试**的那张表：
  琥珀 + ▶  = 正在测试
  浅绿 + ✓  = 已完成
  白        = 排队中

覆盖：
  1. 初始：全部排队中
  2. 收到 PLAN_START → 该行琥珀，其它行不变
  3. PLAN_ROW 等其它消息不干扰高亮
  4. PLAN_DONE → 该行转浅绿，高亮撤下（等下一张的 START）
  5. 连测多张：已完成累积、当前行始终只有一张
  6. 测试中用户改勾选（_sync_sel 重建列表）→ 高亮/完成标记不丢
  7. 测试收尾（_finish_ui）→ 撤掉琥珀，已完成保留
  8. 选很多张表时 see() 不越界
  9. 集成：真起模拟器跑 3 张表，消息走**真实** TestEngine → GUI，高亮沿表推进

用法：python _test_highlight.py
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
    """把 CH/{TER,SAT} 下的 DEMO-* 表复制到临时目录，只扫它。

    ⚠️ 直接扫 CH/ 会把本地**真实频道表名**列进界面；虽然只是测试脚本，
    但一旦有人在带真实表的机器上跑并截图就等于泄露。隔离目录是最省事的防线。
    """
    tmp = tempfile.mkdtemp(prefix="pmcontorl_hl_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def bg(app, i):
    return str(app.sel_box.itemcget(i, "background")).lower()


def txt(app, i):
    return app.sel_box.get(i)


def active():
    return gui.SEL_ACTIVE_BG.lower()


def done():
    return gui.SEL_DONE_BG.lower()


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


def run_engine_integration(root, app, keys):
    """第 9 组：真起模拟器，用**真实 TestEngine** 跑前 3 张表。

    消息不是手工造的，而是引擎实际发出的 —— 验证 MSG_PLAN_START 的顺序与数量，
    以及 GUI 在处理真实消息流时"任何时刻只有一行 ▶"。
    """
    print("\n--- 9) 集成：真跑模拟器，高亮沿表推进")
    if port_busy():
        print("  [SKIP] 2222 被占用（可能是残留的模拟器进程），跳过集成测试")
        return
    mock = start_mock()
    try:
        time.sleep(1.2)
        items = [app._by_key(k) for k in keys[:3]]
        app.sel_done = set()
        app.sel_active = None
        app._paint_sel()

        q = queue.Queue()
        eng = gui.TestEngine(q, 2222)
        th = threading.Thread(
            target=eng.run,
            args=("127.0.0.1", items, dict(DEMO), 20.0, False),
            daemon=True)
        th.start()

        starts = []                 # 引擎实际发出的「开始测某张表」顺序
        arrows = []                 # 每条消息处理后，列表里 ▶ 的行数
        finished = [None]
        state = {"done": False}

        def pump():
            while True:
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    break
                if kind == gui.TestEngine.MSG_PLAN_START:
                    starts.append(payload.name)
                app._handle(kind, payload)              # 走真实的 GUI 处理
                arrows.append(sum(1 for i in range(app.sel_box.size())
                                  if app.sel_box.get(i).startswith("▶ ")))
                if kind in (gui.TestEngine.MSG_FINISH,
                            gui.TestEngine.MSG_STOPPED):
                    finished[0] = kind
            if th.is_alive():
                root.after(30, pump)
            else:
                state["done"] = True

        root.after(30, pump)
        t0 = time.time()
        while not state["done"] and time.time() - t0 < 240:
            root.update()
            time.sleep(0.02)
        th.join(timeout=10)

        want = [it.name for it in items]
        check("跑完（FINISH 收尾）", finished[0] == gui.TestEngine.MSG_FINISH,
              str(finished[0]))
        check("每张表各发一次「开始」，顺序与所选一致",
              starts == want, "实际=%s 期望=%s" % (starts, want))
        check("任何时刻最多只有一行 ▶",
              bool(arrows) and max(arrows) == 1,
              "max=%s" % (max(arrows) if arrows else None))
        check("高亮确实亮过（不是全程没有）",
              1 in arrows, str(sorted(set(arrows))))
        done_cnt = sum(1 for i in range(app.sel_box.size())
                       if app.sel_box.get(i).startswith("✓ "))
        check("跑完所选表全部转「已完成」",
              done_cnt == len(items)
              and not (app.sel_done ^ {it.key for it in items}),
              "✓=%d 期望=%d sel_done=%d"
              % (done_cnt, len(items), len(app.sel_done)))
        check("结束时没有残留的 ▶（琥珀已撤）",
              app.sel_active is None, str(app.sel_active))
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
            raise SystemExit("!! 扫到非演示表，拒绝继续（会泄露真实表名）：%s" % bad)
        print("演示表：%s" % ", ".join(it.file for it in app.items))

        # 勾选前 3 张表（不足就用现有的）
        keys = list(app.check_vars.keys())[:3]
        for k in keys:
            app._toggle(k)
        root.update()
        n = len(keys)
        print("\n--- 1) 初始状态（%d 张表，全部排队中）" % n)
        for i in range(n):
            check("第 %d 行：白底、无前缀" % i,
                  bg(app, i) == gui.SEL_IDLE_BG.lower()
                  and not txt(app, i).startswith(("▶", "✓")),
                  "bg=%s txt=%r" % (bg(app, i), txt(app, i)))
        check("没有正在测试的行", app.sel_active is None, str(app.sel_active))

        it0 = app._by_key(keys[0])
        print("\n--- 2) 收到 PLAN_START（第 1 张开始测）")
        app._handle(gui.TestEngine.MSG_PLAN_START, it0)
        root.update()
        check("第 1 行：琥珀 + ▶ 前缀",
              bg(app, 0) == active() and txt(app, 0).startswith("▶ "),
              "bg=%s txt=%r" % (bg(app, 0), txt(app, 0)))
        check("列表里带 ▶ 的只有这一行",
              sum(1 for i in range(n) if txt(app, i).startswith("▶ ")) == 1)
        for i in range(1, n):
            check("第 %d 行未被误标" % i,
                  bg(app, i) == gui.SEL_IDLE_BG.lower(), bg(app, i))

        print("\n--- 3) 中途收到行数据 / 日志，不影响高亮")
        it0_rows = it0.points()
        mhz, sig, chname, sr = it0_rows[0]
        app._handle(gui.TestEngine.MSG_ROW, (it0, {
            "idx": 1, "band": it0.band, "plan": it0.name, "ch": chname,
            "freq_mhz": "%.3f" % mhz, "signal": sig, "LOCKED": "ON",
            "symb_rate": "", "attempts": 1,
            "tune_s": 0.1, "wait_s": 0.1, "total_s": 0.2}))
        app._handle(gui.TestEngine.MSG_LOG, "  随便一条日志")
        root.update()
        check("高亮仍在第 1 行",
              bg(app, 0) == active() and app.sel_active == it0.key,
              "bg=%s" % bg(app, 0))

        print("\n--- 4) 第 1 张测完（PLAN_DONE）")
        app._handle(gui.TestEngine.MSG_PLAN_DONE, (it0, [{}]))
        root.update()
        check("第 1 行转浅绿 + ✓",
              bg(app, 0) == done() and txt(app, 0).startswith("✓ "),
              "bg=%s txt=%r" % (bg(app, 0), txt(app, 0)))
        check("已无正在测试的行（等下一张的 START）", app.sel_active is None,
              str(app.sel_active))
        check("列表里没有残留的 ▶",
              not any(txt(app, i).startswith("▶ ") for i in range(n)))

        print("\n--- 5) 第 2、3 张依次开始")
        it1 = app._by_key(keys[1])
        app._handle(gui.TestEngine.MSG_PLAN_START, it1)
        root.update()
        check("第 2 行琥珀、第 1 行保持浅绿",
              bg(app, 1) == active() and bg(app, 0) == done(),
              "0=%s 1=%s" % (bg(app, 0), bg(app, 1)))
        check("同时只有一行 ▶",
              sum(1 for i in range(n) if txt(app, i).startswith("▶ ")) == 1)
        app._handle(gui.TestEngine.MSG_PLAN_DONE, (it1, [{}]))
        root.update()
        it2 = app._by_key(keys[2])
        app._handle(gui.TestEngine.MSG_PLAN_START, it2)
        root.update()
        check("已完成 2 行 + 当前 1 行",
              bg(app, 0) == done() and bg(app, 1) == done()
              and bg(app, 2) == active(),
              "0=%s 1=%s 2=%s" % (bg(app, 0), bg(app, 1), bg(app, 2)))

        print("\n--- 6) 测试中用户改了勾选（列表被重建）")
        app._toggle(keys[0])        # 取消第 1 张的勾选 → 重建列表，只剩 2 行
        root.update()
        check("列表少了一行", app.sel_box.size() == n - 1,
              str(app.sel_box.size()))
        check("已完成标记不丢",
              any(txt(app, i).startswith("✓ ") for i in range(app.sel_box.size())),
              [txt(app, i) for i in range(app.sel_box.size())])
        check("正在测试那行仍高亮（key 还在列表里）",
              app.sel_active == it2.key
              and any(txt(app, i).startswith("▶ ")
                      for i in range(app.sel_box.size())),
              "active=%s" % app.sel_active)
        app._toggle(keys[0])        # 勾回来，恢复 3 行
        root.update()
        check("恢复 3 行", app.sel_box.size() == n, str(app.sel_box.size()))

        print("\n--- 7) 测试收尾（_finish_ui）")
        app.engine = None
        app.rows = {}
        app.out_path = os.path.join(HERE, "_hl_tmp_nosave.xlsx")
        app.log_path = None
        app.sep_log_path = None
        app._finish_ui()
        root.update()
        check("琥珀撤掉", app.sel_active is None)
        check("没有残留的 ▶",
              not any(txt(app, i).startswith("▶ ")
                      for i in range(app.sel_box.size())))
        check("已完成的绿底保留",
              sum(1 for i in range(app.sel_box.size())
                  if bg(app, i) == done()) >= 2,
              [bg(app, i) for i in range(app.sel_box.size())])

        print("\n--- 8) 表很多时 see() 不越界")
        app._toggle_all()
        root.update()
        total = app.sel_box.size()
        last = list(app.check_vars.keys())[-1]
        app._handle(gui.TestEngine.MSG_PLAN_START, app._by_key(last))
        root.update()
        check("选中 %d 张表后高亮最后一张不报错" % total,
              app.sel_active == last
              and bg(app, total - 1) == active(),
              "total=%d bg=%s" % (total, bg(app, total - 1)))

        run_engine_integration(root, app, keys)

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
