# -*- coding: utf-8 -*-
"""用 GUI 的测试引擎（TestEngine）对着模拟器跑完整表，验证端到端行为。

不开窗口，直接构造 PlanItem + TestEngine，把日志打印到 stdout。
"""
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promax_plan_dump import load_plans
from promax_plan_gui import PlanItem, TestEngine


def run_plan(engine, item, settle, retry, retry_step, timeout=8.0):
    engine.conn = engine._new_conn() if hasattr(engine, "_new_conn") else None


def main():
    host = "127.0.0.1"
    # 频道表目录：脚本所在目录下的 CH（仓库自带 DEMO-* 演示表）
    here = os.path.dirname(os.path.abspath(__file__))
    plans_dir = os.path.join(here, "CH")
    ter, sat = load_plans(plans_dir)
    by_file = {p["exact_file"]: p for p in ter + sat}

    want = sys.argv[1:] or ["DEMO-Analog.XML", "DEMO-Digital.XML",
                            "DEMO-C-Band.XML", "DEMO-Ku-Band.XML"]
    items = []
    for f in want:
        p = by_file.get(f) or by_file.get(f.strip())
        if p is None:
            print("!! 找不到表 %r；可用：%s" % (f, sorted(by_file)))
            return 2
        items.append(PlanItem(p, plans_dir))

    q = queue.Queue()
    eng = TestEngine(q)
    eng.host = host
    eng.quiet = False
    eng.cfg_run = {"log_timing": True, "log_cmd_timing": False}

    # 直接连（复用引擎的 run 逻辑，但只跑指定表）
    from promax_telnet_test import PromaxTelnet, PromaxError
    eng.conn = PromaxTelnet(host, 2222, timeout=8.0, fast=True)
    try:
        eng.conn.connect()
    except PromaxError as e:
        print("连接失败:", e)
        return 3

    def drain():
        out = []
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break
            if kind == TestEngine.MSG_LOG:
                out.append(payload)
            elif kind == TestEngine.MSG_ROW:
                item, row = payload
                out.append("    ROW [%s %s] %sMHz %s LOCK=%s LEVEL/POWER=%s CN=%s "
                           "MER=%s OFFSET=%s try=%s"
                           % (row["band"], row["ch"], row["freq_mhz"],
                              row["signal"], row["LOCKED"],
                              row.get("LEVEL", row.get("POWER")),
                              row.get("CN"), row.get("MER"),
                              row.get("OFFSET"), row["attempts"]))
            elif kind == TestEngine.MSG_CONFIRM:
                eng.resolve(True)      # 表间暂停：自动继续
        return out

    # ⚠️ 等待参数必须与「模拟器 --lock-time」匹配，否则所有频点第一次都锁不上
    #    （等不够 lock_time），把慢锁注入的效果淹没。
    #    默认用**生产参数**：settle=3.0 / retry=5 / step=6.0（递增 3→9→15→21→27）
    #    可用环境变量 E2E_SETTLE / E2E_RETRY / E2E_STEP 覆盖（例如配 --lock-time 1.5）。
    settle = float(os.environ.get("E2E_SETTLE", "3.0"))
    retry = int(os.environ.get("E2E_RETRY", "5"))
    step = float(os.environ.get("E2E_STEP", "6.0"))

    for it in items:
        print("\n" + "=" * 78)
        rows = eng.run_plan(it, settle=settle, retry=retry, retry_step=step)
        for line in drain():
            print(line)
        print("  -> 共 %d 行结果" % len(rows))
    eng.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
