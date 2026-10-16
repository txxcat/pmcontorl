# -*- coding: utf-8 -*-
"""验证「WiFi 断线 → 测试暂停 → 重连 → 从当前频点继续」的端到端行为。

做法
----
1. 起 mock 模拟器（127.0.0.1:2222）
2. 用 TestEngine 跑一张射频表（DEMO-Digital，12 个 DVB-C 频道，跑得快）
3. 测到第 3 个频道时**杀掉模拟器进程**（模拟 WiFi 断网）
4. 等测试进入「已暂停」状态后，重新拉起模拟器（模拟网络恢复）
5. 断言：
   · 收到「连接丢失，已暂停」而**不是**直接结束
   · 自动重连成功
   · **从当前频点继续，已测完的频道不重测**（结果 12 行、序号 1..12 各一次）
   · 结果行数与表内频道数一致

用法：
    python _test_reconnect.py                # 断网后**自动**重连（默认路径）
    python _test_reconnect.py --manual       # 关掉自动重连，测「重试连接」按钮路径
    python _test_reconnect.py --cold-start   # 开始时模拟器还没起（测"首连失败挂起"）
    python _test_reconnect.py --boundary     # 🔴 断在**表级准备**阶段（换表瞬间掉线）

⚠️ `--boundary` 场景（2026-10-16 补）
    真机反馈："设备断开后恢复没有继续，而是中断了测试"。
    根因：`run_plan()` 里的表级准备 `_prepare_plan()`（切频段 / 下发 LNB / 校验
    BAND）**没有挂起重连**，而 `PromaxConnLost` 是 `PromaxError` 的子类 →
    被 `run()` 的 `except PromaxError` 吃掉 → `break` → 整场测试提前结束。
    表边界恰恰是断线高发点（用户正在动线、仪器也可能踢掉空闲会话），
    所以这个洞一碰就中。
    本场景用模拟器的 `--drop-on-band 1` 注入：第 1 条 `*TUNE BAND=` 直接断链，
    断言测试**照常跑完**（FINISH + 全部频道都有结果）而不是中断。
"""
import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promax_plan_dump import load_plans            # noqa: E402
from promax_plan_gui import PlanItem, TestEngine    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "mock_promax_server.py")
PY = sys.executable

MANUAL = "--manual" in sys.argv            # 关掉自动重连 → 只能靠"重试连接"按钮
COLD_START = "--cold-start" in sys.argv     # 开始时模拟器没起（首连就失败）
BOUNDARY = "--boundary" in sys.argv        # 断在表级准备阶段（换表瞬间掉线）

OK = 0
BAD = 0


def check(label, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK ] %s %s" % (label, extra))
    else:
        BAD += 1
        print("  [!! ] %s %s" % (label, extra))


def start_mock(extra=None):
    return subprocess.Popen([PY, MOCK, "--quiet"] + list(extra or []),
                            cwd=HERE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def main():
    global OK, BAD
    mode = []
    if MANUAL:
        mode.append("手动重连")
    if COLD_START:
        mode.append("冷启动")
    if BOUNDARY:
        mode.append("断在表级准备")
    print("模式：%s" % ("、".join(mode) if mode else "自动重连"))
    # 🔴 --boundary：模拟器自己不退出，只是在第 1 条 *TUNE BAND= 时把连接断掉，
    #    所以"网络恢复"是天然发生的（重连即可）—— 这正对应真机的瞬断场景。
    mock = None if COLD_START else start_mock(
        ["--drop-on-band", "1"] if BOUNDARY else None)
    if COLD_START:
        print(">>> 模拟器暂不启动（模拟：点了开始但网络还没通）")
    time.sleep(1.2)

    ter, sat = load_plans(os.path.join(HERE, "CH"))
    by_file = {p["exact_file"]: p for p in ter + sat}
    item = PlanItem(by_file["DEMO-Digital.XML"], os.path.join(HERE, "CH"))
    n_total = len(item.points())

    q = queue.Queue()
    eng = TestEngine(q, 2222)
    eng.timeout_run = 20.0
    cfg = {"log_timing": True,
           "auto_reconnect": not MANUAL,
           "reconnect_interval": 2.0}      # 2 秒自动重连，测试跑得快
    th = threading.Thread(target=eng.run,
                          args=("127.0.0.1", [item], cfg, 20.0, False),
                          daemon=True)
    th.start()

    rows_seen = 0
    net_lost = 0
    net_ok = 0
    killed_at = None
    restarted = False
    finished = None
    final_rows = None
    kill_mock_now = False

    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            kind, payload = q.get(timeout=0.5)
        except queue.Empty:
            if not th.is_alive():
                break
            continue

        if kind == TestEngine.MSG_ROW:
            rows_seen += 1
            # 冷启动 / 表边界场景没有"测到一半被杀掉"，全部频道一次跑完
            if not COLD_START and not BOUNDARY and rows_seen == 3 \
                    and killed_at is None:
                killed_at = rows_seen
                kill_mock_now = True
        elif kind == TestEngine.MSG_NETLOST:
            net_lost += 1
            print(">>> 已暂停：%s" % payload)
            if BOUNDARY:
                print(">>> 模拟器进程仍在（只是把连接断了）→ 重连即可恢复")
            elif not restarted:
                time.sleep(1.5)             # 装成"网络恢复需要一点时间"
                if COLD_START:
                    print(">>> 拉起模拟器（模拟网络恢复）")
                    mock = start_mock()
                else:
                    print(">>> 重新拉起模拟器（模拟网络恢复）")
                    mock = start_mock()
                restarted = True
            if MANUAL:
                print(">>> 点「重试连接」按钮")
                eng.retry_connect()
        elif kind == TestEngine.MSG_NETOK:
            net_ok += 1
            print(">>> 重连成功（第 %d 次）" % net_ok)
        elif kind == TestEngine.MSG_LOG:
            print("    " + payload)
        elif kind == TestEngine.MSG_PLAN_DONE:
            it, rows = payload
            print(">>> %s 完成 %d 行" % (it.name, len(rows)))
        elif kind in (TestEngine.MSG_FINISH, TestEngine.MSG_STOPPED):
            finished = kind
            if kind == TestEngine.MSG_FINISH and payload:
                final_rows = payload.get(item.name, [])
            break

        # 杀掉模拟器要放在消息处理之后，避免影响同一轮的计数
        if kill_mock_now:
            kill_mock_now = False
            print(">>> 第 3 个频道已测完，杀掉模拟器（模拟 WiFi 断网）")
            try:
                mock.kill()
            except Exception:
                pass

    try:
        th.join(timeout=10)
    except Exception:
        pass
    try:
        if mock is not None and mock.poll() is None:
            mock.kill()
    except Exception:
        pass
    eng.close()

    print("\n=====================================")
    print("已测频点(收到 ROW)=%d  最终结果行=%s  重连: 暂停 %d 次 / 成功 %d 次"
          % (rows_seen, len(final_rows) if final_rows is not None else "None",
             net_lost, net_ok))
    if COLD_START:
        check("首连失败也进入「已暂停」而不是直接结束", net_lost >= 1,
              "net_lost=%d" % net_lost)
    elif BOUNDARY:
        check("🔴 表级准备阶段断线也进入「已暂停」（曾直接中断整场测试）",
              net_lost >= 1, "net_lost=%d" % net_lost)
    else:
        check("断线后收到「已暂停」（没有直接中断）", net_lost >= 1,
              "net_lost=%d" % net_lost)
        check("断线发生在第 3 个频道测完之后", killed_at == 3,
              "killed_at=%s" % killed_at)
    check("重连成功", net_ok >= 1, "net_ok=%d" % net_ok)
    check("测试正常收官（FINISH 而不是 STOPPED）",
          finished == TestEngine.MSG_FINISH, "finished=%s" % finished)
    check("结果行数 == 表内频道数", final_rows is not None
          and len(final_rows) == n_total,
          "rows=%s total=%d" % (len(final_rows) if final_rows else None, n_total))
    if BOUNDARY:
        check("🔴 断线发生在**任何频道之前**（表级准备就断了，之后仍全部测到）",
              rows_seen == n_total and (final_rows or []) and
              len(final_rows) == n_total, "rows_seen=%d" % rows_seen)
    if final_rows:
        idxs = [r["idx"] for r in final_rows]
        check("序号连续无重复（未重测已完成的频道）",
              sorted(idxs) == list(range(1, n_total + 1)), str(idxs))
        check("全部频道都测到了（含断线点之后的）",
              all(any(r["idx"] == k for r in final_rows)
                  for k in range(1, n_total + 1)), "")

    print("=====================================")
    print("通过 %d   失败 %d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
