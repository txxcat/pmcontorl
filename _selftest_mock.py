# -*- coding: utf-8 -*-
"""对着本地模拟仪器做一轮协议级自检（不启动 GUI）。

用法：
    python mock_promax_server.py            # 默认 --lock-time 3.0（对齐现场）
    python _selftest_mock.py                # 默认按 3.0s 等待
    python _selftest_mock.py --lock-time 1.5
等待时长必须 >= 模拟器的 --lock-time，否则「该锁上的」会误判为失败。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promax_telnet_test import (PromaxTelnet, Promax, PromaxError,
                                parse_measure_all, parse_tune_ch)

# 模拟器锁定所需的最短连续等待（与启动 mock 的 --lock-time 一致）
LOCK_TIME = 3.0
if "--lock-time" in sys.argv:
    LOCK_TIME = float(sys.argv[sys.argv.index("--lock-time") + 1])
WAIT = LOCK_TIME + 0.8          # 留点余量，避免边界抖动

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


def measure_locked(inst, max_tries=20, wait=WAIT, poll=1.0):
    """连续测量直到 LOCKED=ON（最多 max_tries 次），返回 (lock, allv, tries)。

    2026-10-14：模拟器现在会按实测给**个别卫星中频**注入慢锁（前若干次 OFF），
    所以"等够时间就 ON"的断言要允许按需重试。重试**不重发 TUNE**。

    wait   —— 第一次调用前的预等待（默认 = LOCK_TIME + 0.8s，让基线频道先锁上）
    poll   —— 每次失败后的轮询间隔（默认 1.0s，确保 20s 总 budget 能覆盖
              SLOW_LOCK 表里 ≤16s 的所有慢锁频道）
    max_tries 默认 20：与客户端生产参数 retry=5 + retry_step=6（累计等待 3/9/15/21/27
              秒，最深 33s）大致对齐，能覆盖 16s 频道。

    2026-10-15：原版每次失败后只 sleep(0.4)，导致 SLOW_LOCK=16.0s 的频道在
    max_tries 内永远 OFF。本版改用 poll=1.0 默认。
    """
    try:
        time.sleep(wait)
        for n in range(1, max_tries + 1):
            txt, allv = inst.measure_all()
            lk = str(allv.get("LOCKED", (None, ""))[1]).upper()
            if lk == "ON":
                return lk, allv, n
            if n < max_tries:
                time.sleep(poll)
        return lk, allv, max_tries
    except Exception as e:                       # pragma: no cover
        return "ERR:%s" % e, {}, max_tries


conn = PromaxTelnet("127.0.0.1", 2222, timeout=8.0, fast=True)
conn.connect()
inst = Promax(conn)
print("=== 基本握手 ===")
check("VER", "31.17" in " ".join(inst.ver()))
check("SN", "010171194" in " ".join(inst.sn()))
check("UNITS", "DBUV" in " ".join(conn.query("?UNITS")).upper())
check("INSTALLATION NUMBER only", len(inst.installation()) == 1)
ic = " ".join(inst.installation_current())
check("INSTALL CURRENT 多行含 TER-CH", "TER-CH=" in ic, ic[:90])

print("\n=== 真机坑：恒 NAK 的命令 ===")
r = inst.tune_plan("DEMO-C-Band.XML")
check("TUNE PLAN= 恒 NAK", r["status"] == "nak", r["status"])
r = conn.command("LNB OUTPUT=0", want_response=False)
check("LNB OUTPUT=0 NAK", r["status"] == "nak", r["status"])
r = conn.command("LNB OUTPUT=OFF", want_response=False)
check("LNB OUTPUT=OFF ACK", r["ack"], r["status"])
r = conn.command("SIGNAL STANDARD=DK", want_response=False)
check("STANDARD=DK NAK", r["status"] == "nak", r["status"])

print("\n=== 模拟制式 DEMO-Analog（113.25 有读数 / 116 死点）===")
inst.set_band("TER"); time.sleep(0.4)
inst.signal_type("ANALOG")
inst.signal_standard("PAL")
r = inst.set_tune("TER", "113250K"); time.sleep(1.0)
check("调谐 113.250K", r["ack"], r["status"])
txt, allv = inst.measure_all()
check("113.25 LEVEL 有值", allv.get("LEVEL", (None,))[0] is not None, txt[:70])
check("113.25 LEVEL≈90", abs((allv.get("LEVEL", (0,))[0] or 0) - 90.67) < 1.0)
check("113.25 CN≈53", abs((allv.get("CN", (0,))[0] or 0) - 53.29) < 1.0)
check("113.25 不锁定(模拟)", str(allv.get("LOCKED", (None, "x"))[1]).upper() != "ON")
r = inst.set_tune("TER", "116000K"); time.sleep(1.0)
txt, allv = inst.measure_all()
lv = allv.get("LEVEL", (0,))[0] or 0
check("116.000 是死点", lv < 40, "LEVEL=%.2f" % lv)

print("\n=== 数字地面 DVB-C ===")
inst.signal_type("DVB-C")
r = inst.set_tune("TER", "474000K"); time.sleep(2.0)
txt, allv = inst.measure_all()
check("474 返回 POWER 无 LEVEL", "LEVEL" not in allv or allv["LEVEL"][0] is None, txt[:60])
check("474 POWER 有值", allv.get("POWER", (None,))[0] is not None)

print("\n=== 卫星 DEMO-C-Band（本振 5150，中频 = 5150 − 下行）===")
inst.set_band("SAT"); time.sleep(0.4)
r = inst.set_tune("SAT", "3780000K"); time.sleep(0.5)     # 直接发下行频率
check("直接发下行频率 NAK", r["status"] == "nak", r["status"])
# 符号率先、制式后
for label, rr in inst.set_sat_params("DVB-S2", 20000):
    check("卫星参数 %s" % label, rr["ack"], rr["status"])
sig = " ".join(inst.c.query("?SIGNAL"))
check("SIGNAL 回显 TYPE=DVB-S2 SR=20000k", "DVB-S2" in sig and "20000" in sig, sig)
r = inst.set_tune("SAT", "1250000K")   # 5150−3900=1250，DEMO-C-Band 的 DVB-S2 频道
lk, allv, tries = measure_locked(inst)
check("卫星中频 1250 锁定 ON", lk == "ON", "tries=%d" % tries)
check("卫星 MER 有值", allv.get("MER", (None,))[0] is not None)
check("卫星 OFFSET 有值", allv.get("OFFSET", (None,))[0] is not None)
t = " ".join(inst.get_tune())
check("?TUNE 含 DL=3900MHz", "DL=3900MHz" in t, t)

print("\n=== 符号率不影响锁定（用户实测：仪器自动搜 SR）===")
for label, rr in inst.set_sat_params("DVB-S", 21370):   # 故意用错 SR
    pass
r = inst.set_tune("SAT", "1110000K")    # 5150−4040=1110，表载 8500k
lk, allv, tries = measure_locked(inst)
check("SR 错也能锁定（仪器自动搜）", lk == "ON", "tries=%d" % tries)
# 不设 SR 也应能锁定（复现无 SYMBOLRATE 的频道表）
inst.signal_type("DVB-S")
r = inst.set_tune("SAT", "1370000K")   # 5150−3780=1370，表载 12000k
lk, allv, tries = measure_locked(inst)
check("不设 SR 也能锁定", lk == "ON", "tries=%d" % tries)

print("\n=== 重发 TUNE 打断锁定（真机坑复现）===")
for label, rr in inst.set_sat_params("DVB-S", 8500):
    pass
for i in range(4):                      # 每 0.5s 重调一次，永远锁不上
    inst.set_tune("SAT", "1110000K")
    time.sleep(0.5)
txt, allv = inst.measure_all()
lk = str(allv.get("LOCKED", (None, ""))[1]).upper()
check("重发 TUNE 导致锁不上", lk == "OFF", "LOCKED=%s" % lk)
# 只调一次然后连续等待 → 锁定
inst.set_tune("SAT", "1110000K")
lk, allv, tries = measure_locked(inst)
check("只调一次+连续等待 → 锁定", lk == "ON", "tries=%d" % tries)

print("\n=== 慢锁注入（仅卫星个别频道；射频一律一次锁）===")
# 中频取自 mock_promax_server.SLOW_LOCK 的「DEMO 演示组」，
# 对照项 IF=1215（DEMO-Ku-Band 的 12515 路）不在表里 → 应当一次锁。
for label, if_k, want_off in (
        ("IF1370 3780 慢锁1次", "1370000K", 1),
        ("IF1250 3900 慢锁2次", "1250000K", 2),
        ("IF1110 4040 慢锁1次", "1110000K", 1),
        ("IF1030 4120 慢锁1次", "1030000K", 1),
        ("IF1215 12515 表外对照", "1215000K", 0),
):
    inst.set_band("SAT"); time.sleep(0.2)
    inst.signal_type("DVB-S")
    inst.set_tune("SAT", if_k)
    time.sleep(WAIT)
    txt, allv = inst.measure_all()          # 第 1 次（慢锁项预计 OFF）
    lk1 = str(allv.get("LOCKED", (None, ""))[1]).upper()
    if want_off:
        check("%s：第1次 OFF" % label, lk1 == "OFF", "LOCKED=%s" % lk1)
        # 2026-10-15：max_tries=want_off+2 在 poll=0.4 时不够，现在默认 poll=1.0；
        # 但 8s/16s 频道仍需足量次数才能等到 → 显式放宽到 20 次以涵盖 SLOW_LOCK_max=16s
        lk, allv, tries = measure_locked(inst, max_tries=20, wait=0.0)
        check("%s：等够后 ON" % label, lk == "ON", "第%d次" % (tries + 1))
    else:
        check("%s：第1次即 ON" % label, lk1 == "ON", "LOCKED=%s" % lk1)

print("\n=== Ku 波段 DEMO-Ku-Band（本振 11300，中频 = 下行 − 11300）===")
inst.set_band("SAT"); time.sleep(0.3)
r = inst.set_tune("SAT", "1095000K")        # 12395 − 11300 = 1095
check("Ku 中频 1095 ACK", r["ack"], r["status"])

print("\n=== NAK 会话 / 收尾 ===")
r = conn.command("TUNE TYPE=DVB-S2 SR=15950k", want_response=False)
check("TYPE+SR 合并 NAK", r["status"] == "nak", r["status"])
conn.close()

print("\n=====================================")
print("通过 %d   失败 %d" % (OK, BAD))
sys.exit(1 if BAD else 0)
