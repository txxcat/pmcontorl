# -*- coding: utf-8 -*-
"""PROMAX 仪器 Telnet 服务端模拟器 —— 用于无真机时验证客户端协议实现。

设计目标
--------
**尽可能等价于真机（RANGERNeo 4 / VER 31.17）**，把真机联调时踩过的坑全部
复现出来，这样明天没有真机也能验证客户端的完整流程（含错误路径）。

复现的真机行为
--------------
1. **频谱模型**（不是"任何频率都有读数"）
   - 模拟制式(ANALOGTER)：只有**图像载频**附近才有读数；
     表里的 `frequency`（频道中心频率，= 载频 + 2.75MHz）是**死点**。
     实测：名值 113.25 → LEVEL 90.7 / CN 53.3；表值 116.00 → LEVEL 28 / CN 0。
   - 数字(DVB-C/DVB-S/S2)：只有落在**表载频点**附近才有信号，否则低电平。
2. **卫星中频**：`*TUNE BAND=SAT FREQ=` 只接受**中频(IF)**。
   直接发下行频率(downlink)会 **NAK**（实测 3780000K → NAK）。
3. **符号率**（VER 31.17 实测）
   - `*SIGNAL TYPE=DVB-S2 SR=15950k`（合并）→ **NAK**
   - 发 `*SIGNAL SR=` 会把 TYPE **重置为 DVB-S**
   - ⚠️ **符号率设不设、设对设错都不影响能否锁定** —— 本机仪器只靠正确频点
     就能自动搜出符号率/制式（实测：整表无 SYMBOLRATE 的 8 个
     频点全部一次锁定）。早期版本曾用"SR 不匹配→锁不上"，**那是错的**、已删。
4. **锁定状态机**（锁定只由**等待时间**决定）
   - 调谐后需要**连续等待** lock_time 秒才 LOCKED=ON
   - **重发 `*TUNE` 会重置捕获过程**（锁不上）—— 复现"重试时重发 TUNE 越试越糟"
   - ANALOG 制式**永不 LOCKED=ON**（连续载波），但有 LEVEL/CN 读数
   - 🔴 **按真机实测注入慢锁**：
     · **射频（地面）一律一次锁** —— 实测运作极好，不再注入 unlock（省时间）
     · 只有**卫星的个别频道**按实测注入（见 `SLOW_LOCK` 表，键为「频段+中频MHz」）
5. **NAK 行为**：`*TUNE PLAN=` 恒 NAK、`LNB OUTPUT=0` NAK、`STANDARD=DK/BG` NAK。
6. **回显**：`*?SIGNAL`（TYPE + SR）、`*?LNB`（AVAILABLE 列表）、
   `*?INSTALLATION CURRENT`（多行 + NAME/TER-CH/SAT-CH）、`*?TUNE`（含 DL/MODE）。

频道数据源
----------
默认读 `本目录/CH/{TER,SAT}/*.XML`。
仓库里只带 `DEMO-*` 演示表；把自己的表放进 `CH/TER`、`CH/SAT` 即可。
可用 `--plans <目录>` 指向别处；指向 .tgz 也能读。

用法
----
  python mock_promax_server.py                       # 127.0.0.1:2222
  python mock_promax_server.py --fail-first 2        # 每频点前 2 次报 LOCKED=OFF
  python mock_promax_server.py --lock-time 6         # 调谐后需连续等 6s 才锁定
  python mock_promax_server.py --plan DEMO-C-Band    # 只加载指定表（可多次）
  python mock_promax_server.py --list-plans          # 列出可用表后退出

🔴 锁定模型（默认，按真机实测标定）
  · 射频（地面 / TER）：**一律一次锁**（不注入 unlock）
  · 卫星（SAT）：只有个别频道注入慢锁，见 SLOW_LOCK 表（键为「频段+中频MHz」）
  · `--fail-first N` 可强行让**所有**频点前 N 次报 OFF（用于专门测重试）

调试开关
--------
  --quiet        不打印收发包（跑批量测试时清爽）
  --slow F       每条响应额外延迟 F 秒（模拟真机"单条命令要数秒"）
  --single       只允许一个会话（复现"仪器只允许一个 telnet 会话"）
"""
import argparse
import os
import re
import socket
import sys
import threading
import time

# 复用离线解析层，保证"模拟器认识的频率"与"客户端发的频率"同一套换算逻辑
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from promax_plan_dump import load_plans, parse_plan_xml, _num
except Exception:                                   # pragma: no cover
    load_plans = parse_plan_xml = _num = None

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
XON, XOFF, ACK, NAK, CR, LF = 0x11, 0x13, 0x06, 0x15, 0x0D, 0x0A

# 仪器固定身份（跟真机一致，方便和真机日志对照）
VER_STR = "*VER 31.17"
NAM_STR = "*NAM RANGERNeo 4"
SN_STR = "*EQUIPMENT SN= 010171194"

# LNB OUTPUT 允许值（真机 *?LNB 的 AVAILABLE 列表，逐字对齐）
LNB_AVAILABLE = ["OFF", "5.0", "13.0", "13.0+22kHz", "15.0", "18.0", "18.0+22kHz"]

# 锁定后各制式的典型读数（dBuV / dB）
LEVEL_DBUV, CN_DB, MER_DB = 90.67, 53.29, 19.7
NOISE_LEVEL, NOISE_CN = 27.5, 0.0        # 死点/无信号时的读数
LOCK_LEVEL_MIN = 40.0                     # 电平低于此值视为无信号

# ---------------------------------------------------------------------------
# 🔴 「慢锁」表 —— 按**真机实测**给个别卫星频道注入 unlock（默认其余一次锁）
# ---------------------------------------------------------------------------
# 真机实测口径：
#   · **射频（地面）实测非常好，基本一次 lock** → 不注入任何 unlock（省时间）。
#   · lock 问题只出在**卫星个别频道**。
#
# 数据来自一次 7 表 26 频点的实测日志（5 个靠加长等待才锁上）：
#   部分 C 波段频道 → 第 1 次重试(9s)锁定
#   个别频道       → 第 2 次重试(15s)锁定
#
# 键 = (频段, **中频 IF** MHz)；值 = 「前 N 次测量返回 LOCKED=OFF」的 N。
# ⚠️ 用**中频**做键（这正是客户端 `*TUNE FREQ=` 发的值，也是 mock 频点表的主键；
#    频道 dict 里 freq_mhz 对卫星表是 None，只用 dl_mhz 会永远取不到 → 注入失效）。
#    C 波段本振 5150：IF = 5150 − downlink。
# 客户端只在首尝试发 TUNE，重试只延长等待 → 加长等待后会自然锁上（与真机一致）。
# 慢锁注入表：键 (频段, 中频MHz) → 前 N 次测量报 LOCKED=OFF
#
# 分两组：
#   1) 「现场实测组」—— 按真机实测标定的中频（C 波段本振 5150）。
#      用自己机器的频道表时，把这些中频换成你实测到的即可。
#   2) 「DEMO 演示组」—— 对应仓库自带 CH/SAT/DEMO-C-Band.XML 的中频，
#      保证开箱（无真实表）也能演示慢锁与自动重试。
SLOW_LOCK = {
    # —— 现场实测组（可按自己仪器改）——
    ("SAT", 1400): 1,
    ("SAT", 1199): 2,
    ("SAT", 1150): 1,
    ("SAT", 979): 1,
    ("SAT", 1330): 1,
    ("SAT", 990): 2,
    # —— DEMO 演示组（DEMO-C-Band：下行 3780/3900/4040/4120）——
    ("SAT", 1370): 1,
    ("SAT", 1250): 2,
    ("SAT", 1110): 1,
    ("SAT", 1030): 1,
}

# DEMO 演示表（仓库自带）的慢锁中频集合，用于启动横幅分组显示
DEMO_SLOW_IFS = {1370, 1250, 1110, 1030}


def slow_lock_attempts(band, if_mhz):
    """该 (频段,中频) 需要「前几次」报 OFF；不在表里 → 0（一次锁）"""
    if not if_mhz:
        return 0
    try:
        return SLOW_LOCK.get((band, int(round(float(if_mhz)))), 0)
    except (TypeError, ValueError):
        return 0


# 调试：设环境变量 MOCK_SLOW_DBG=1 时，把慢锁判定打到 stderr
_SLOW_DBG = os.environ.get("MOCK_SLOW_DBG", "") == "1"


class State(object):
    """一台"仪器"的全部状态"""

    def __init__(self):
        self.band = "TER"          # TER / SAT
        self.freq_khz = 474000     # 当前调谐频率（kHz）
        self.mode = "FREQ"         # FREQ / CH
        self.signal_type = "ANALOG"
        self.symbol_rate = None    # kSymb/s
        self.standard = "PAL"      # 模拟制式标准
        self.plan = ""             # *?TUNE CH 回显用（远程切不了，仅显示）
        self.ch_name = "0"
        self.units_ter = "DBUV"
        self.units_sat = "DBUV"
        # 锁定状态机
        self.tune_at = 0.0         # 最后一次调谐的时刻
        self.tune_gen = 0          # 调谐代数：重发 TUNE 会 +1（打断锁定）
        # 每频点失败注入（--fail-first）
        self.fail_first = 0
        self.attempt = {}          # (band,khz) -> 已测量次数
        # 统计
        self.lock_at = None

    # ---- 锁定判定 ----
    def is_locked(self, lock_time):
        """调谐后需连续等待 lock_time 秒才锁定。

        🔴 关键：只有**调谐之后没再收到新 TUNE**（tune_gen 不变）才计时。
        重发 TUNE 会把 tune_at 刷新 → 永远等不到 lock_time → 永远锁不上，
        正是真机实测（某频道每 2s 重调连试 3 次 → LOCKED=OFF MER=0）的成因。
        """
        if self.signal_type.upper().startswith("ANALOG"):
            return False                       # 模拟制式永不锁定
        if lock_time <= 0:
            return True
        return (time.time() - self.tune_at) >= lock_time

    def next_attempt(self):
        """记录一次测量尝试，返回是第几次（用于 --fail-first）"""
        key = (self.band, self.freq_khz)
        if key != getattr(self, "_last_key", None):
            self._last_key = key
            self.attempt[key] = 0
        self.attempt[key] = self.attempt.get(key, 0) + 1
        return self.attempt[key]


STATE = State()
PLANS = {"TER": [], "SAT": []}     # [{'file','name','band','channels','lnb...'}]
LOCK = threading.Lock()
QUIET = False
SLOW = 0.0
SINGLE = False
OPEN_COUNT = [0]


def log(msg):
    if not QUIET:
        print(msg, flush=True)


# ---------------------------------------------------------------- 数据加载
def load_plan_dir(root, only=None):
    """加载频段表。root 可以是目录（含 TER/SAT 或 CH/TER）或 .tgz"""
    global PLANS
    PLANS = {"TER": [], "SAT": []}
    if load_plans is None:
        print("[警告] 无法导入 promax_plan_dump，改用内置示例表", file=sys.stderr)
        _load_builtin()
        return
    try:
        ter, sat = load_plans(root)
    except Exception as e:
        print("[警告] 加载 %s 失败：%s" % (root, e), file=sys.stderr)
        _load_builtin()
        return
    for group, tag in ((ter, "TER"), (sat, "SAT")):
        for p in group:
            if only and not any(k.lower() in p["file"].lower() for k in only):
                continue
            PLANS[tag].append(p)
    if not PLANS["TER"] and not PLANS["SAT"]:
        print("[警告] %s 没解析出任何频道表，改用内置示例表" % root, file=sys.stderr)
        _load_builtin()


def _load_builtin():
    """兜底：内置最小**演示**表（CH 目录不可用时用）

    🔴 只用虚构的 DEMO-* 数据：真实频道表含运营数据，不进仓库。
    """
    def ch(name, freq, sig, symb=None, tune=None, src="plan"):
        return {"name": name, "freq": freq, "freq_mhz": freq, "tune_mhz": tune or freq,
                "tune_src": src, "signal": sig, "symb_rate": symb, "detail": "",
                "bandwidth": ""}
    PLANS["TER"].append({
        "file": "DEMO-Analog.XML", "name": "DEMO-Analog", "band": "TERRESTRIAL",
        "channels": [ch("113.25-Z03", 116.0, "ANALOGTER", tune=113.25, src="name"),
                     ch("787.25-DS52", 790.0, "ANALOGTER", tune=787.25, src="name")],
        "lnb_loc_osc": "", "lnb_band": ""})
    PLANS["TER"].append({
        "file": "DEMO-Digital.XML", "name": "DEMO-Digital", "band": "TERRESTRIAL",
        "channels": [ch("474", 474.0, "DVB-C"), ch("546", 546.0, "DVB-C")],
        "lnb_loc_osc": "", "lnb_band": ""})
    PLANS["SAT"].append({
        "file": "DEMO-C-Band.XML", "name": "DEMO-C-Band", "band": "SATELLITE",
        "channels": [ch("3780", 3780.0, "DVB-S", symb=12000, tune=1370.0, src="lnb"),
                     ch("4120", 4120.0, "DVB-S", symb=15000, tune=1030.0, src="lnb")],
        "lnb_loc_osc": "5150MHz", "lnb_band": "BAND_C"})


def all_points():
    """全部频点 → {(band, khz): point}，用于"这个频率表里有吗"判定

    point: {'dl_mhz','if_mhz','signal','symb','name','plan','kind'}
    kind: 'analog' / 'digital'
    """
    pts = {}
    for tag in ("TER", "SAT"):
        for p in PLANS[tag]:
            for c in p["channels"]:
                f = c.get("tune_mhz", c.get("freq_mhz"))
                if f is None:
                    continue
                khz = int(round(f * 1000))
                kind = "analog" if str(c["signal"]).upper().startswith("ANALOG") \
                    else "digital"
                pts[(tag, khz)] = {
                    "kind": kind, "signal": c["signal"], "symb": c.get("symb_rate"),
                    "name": c["name"], "plan": p["exact_file"],
                    "dl_mhz": c.get("freq_mhz"), "if_mhz": f,
                    "lnb_band": p.get("lnb_band", ""),
                }
    return pts


POINTS = {}


def nearest_point(band, khz, tol_khz=None):
    """找该频率附近最近的频点。

    模拟频道（模拟制式）容差极小：只有落在图像载频 ±0.05MHz 才有读数，
    这样表值 147.000（中心频率）会被判成死点，行为与真机一致。
    数字/卫星容差放宽到 ±2MHz（真实载波有带宽），但表外频率仍报低电平。
    """
    best = None
    for (b, k), pt in POINTS.items():
        if b != band:
            continue
        tol = 300 if pt["kind"] == "digital" else 50      # kHz
        if tol_khz is not None:
            tol = tol_khz
        d = abs(khz - k)
        if d <= tol and (best is None or d < best[0]):
            best = (d, pt)
    return best[1] if best else None


# ---------------------------------------------------------------- NAK 判定
# ⚠️ 不能叫 NAK —— 上面 NAK 是控制字符 0x15，同名会把它覆盖成 object
#     （曾因此让 conn.sendall(bytes([NAK, XON])) 抛 TypeError 并断开连接）
REJECT = object()       # 命令被拒绝
NOBODY = object()       # 接受但无响应体


def _freq_to_khz(body):
    """从 'BAND=SAT FREQ=1400000K' 里取 FREQ → kHz"""
    m = re.search(r"FREQ\s*=\s*([0-9.]+)\s*([KMG]?)", body, re.I)
    if not m:
        return None
    return int(float(m.group(1)) *
               {"": 1, "K": 1, "M": 1000, "G": 1e6}[m.group(2).upper() or ""])


def _sat_downlink_khz(band, khz, lnb_band):
    """把卫星中频换算回下行频率（仅用于 *?TUNE 的 DL= 回显）。"""
    pt = nearest_point("SAT", khz)
    return int(round(pt["dl_mhz"] * 1000)) if pt else None


# ---------------------------------------------------------------- 命令分发
def dispatch(body, query):
    k = " ".join(body.upper().split())
    st = STATE

    # ---------------- 基本信息 ----------------
    if k == "VER":
        return VER_STR
    if k == "NAM":
        return NAM_STR
    if k == "EQUIPMENT SN":
        return SN_STR
    if k == "UNITS":
        # 真机实测：*UNITS TER=DBUV SAT=DBUV（等号后无空格）
        return "*UNITS TER=%s SAT=%s" % (st.units_ter, st.units_sat)
    if k.startswith("UNITS TER="):
        st.units_ter = body.split("=", 1)[1].strip().upper()
        return NOBODY
    if k.startswith("UNITS SAT="):
        st.units_sat = body.split("=", 1)[1].strip().upper()
        return NOBODY

    # ---------------- 调谐 ----------------
    if k == "TUNE":
        band = st.band
        out = "*TUNE BAND=%s FREQ=%dK" % (band, st.freq_khz)
        if band == "SAT":
            dl = _sat_downlink_khz(band, st.freq_khz, None)
            if dl:
                # 真机回 DL 用 MHz（如 DL=3780MHz），中频是 1370000K
                out += " DL=%gMHz" % (dl / 1000.0)
        return out + " MODE=%s" % st.mode

    if k.startswith("TUNE MODE="):
        v = body.split("=", 1)[1].strip().upper()
        if v not in ("FREQ", "CH"):
            return REJECT
        st.mode = v
        return NOBODY
    if k == "TUNE MODE":                       # 无参查询
        return "*TUNE MODE=%s" % st.mode

    # *TUNE PLAN=<xml> —— 🔴 真机（VER 31.17）**恒 NAK**，远程切不了表
    if k.startswith("TUNE PLAN="):
        return REJECT

    # *TUNE CH [NEXT|PREV]
    if k == "TUNE CH" or k.startswith("TUNE CH "):
        sub = body[8:].strip().upper()
        plans = PLANS[st.band]
        if not plans:
            return REJECT
        idx = 0
        for i, p in enumerate(plans):
            if p["exact_file"] == st.plan:
                idx = i
                break
        chs = plans[idx]["channels"]
        if sub == "NEXT":
            cur = None
            for j, c in enumerate(chs):
                if c["name"] == st.ch_name:
                    cur = j
            st.ch_name = chs[(cur + 1) % len(chs)]["name"] if chs else ""
            return NOBODY
        if sub == "PREV":
            cur = None
            for j, c in enumerate(chs):
                if c["name"] == st.ch_name:
                    cur = j
            st.ch_name = chs[(cur - 1) % len(chs)]["name"] if chs else ""
            return NOBODY
        if not st.plan:
            st.plan = plans[0]["exact_file"]
        # 与真机一致：PLAN 是 XML 文件名，CH 是频道名（卫星表里即下行频率数值）
        return "*TUNE BAND=%s PLAN=%s CH=%s" % (st.band, st.plan, st.ch_name or "0")

    # *TUNE FREQ=nnnK  （单独一条）
    if k.startswith("TUNE FREQ="):
        khz = _freq_to_khz(body)
        if khz is None:
            return REJECT
        if st.band == "SAT" and not _valid_sat_if(khz):
            return REJECT
        _do_tune(khz)
        return NOBODY

    # *TUNE BAND=TER|SAT [FREQ=nnnK]
    if k.startswith("TUNE BAND="):
        m = re.search(r"BAND\s*=\s*([A-Z]+)", body, re.I)
        if not m:
            return REJECT
        band = m.group(1).upper()
        if band not in ("TER", "SAT"):
            return REJECT
        khz = _freq_to_khz(body)
        if khz is not None and band == "SAT" and not _valid_sat_if(khz):
            # 直接发下行频率会被拒（实测 3780000K → NAK）
            return REJECT
        if band != st.band:
            st.plan = ""
            st.ch_name = "0"
        st.band = band
        if khz is not None:
            _do_tune(khz)
        else:
            st.tune_at = time.time()        # 只切频段也算一次调谐
            st.tune_gen += 1
        return NOBODY

    # ---------------- 测量 ----------------
    if k == "MEASURE POWER" or k == "MEASURE OFFSET" or k.startswith("MEASURE "):
        pass    # 真机带参数也返回全部，落到下面统一处理
    if k.startswith("MEASURE"):
        return _measure()

    # ---------------- SIGNAL ----------------
    # 🔴 真机实测（VER 31.17）：TYPE= 与 SR= **不能合并成一条**，否则 NAK。
    #    先统一拦掉："同时出现 TYPE= 和 SR=/SYMBOLRATE=" 即拒。
    if query is False and re.search(r"TYPE\s*=", body, re.I) and \
            re.search(r"(SR|SYMBOLRATE)\s*=", body, re.I):
        return REJECT
    if k.startswith("SIGNAL TYPE="):
        v = body.split("=", 1)[1].strip().upper()
        if v not in ("ANALOG", "DVB-C", "DVB-T", "DVB-S", "DVB-S2", "DVB-S2X"):
            return REJECT
        # ✅ 发 TYPE= 不会动符号率（真机实测只有 SR= 才重置 TYPE）
        st.signal_type = v
        _reset_lock()
        return NOBODY
    if k.startswith("SIGNAL SR=") or k.startswith("SIGNAL SYMBOLRATE="):
        m = re.search(r"(?:SR|SYMBOLRATE)\s*=\s*([0-9.]+)\s*([KMG]?)", body, re.I)
        if not m:
            return REJECT
        sr = float(m.group(1)) * {"": 1, "K": 1, "M": 1000, "G": 1e6}[
            m.group(2).upper() or ""]
        st.symbol_rate = sr
        # 🔴 真机实测：发 SR= 会把 TYPE 重置为 DVB-S（即使原来是 DVB-S2）
        if st.signal_type.upper().startswith("DVB-S"):
            st.signal_type = "DVB-S"
        _reset_lock()
        return NOBODY
    if k.startswith("SIGNAL STANDARD="):
        v = body.split("=", 1)[1].strip().upper()
        # 真机实测：只有 PAL 能被接受，DK/BG 一律 NAK
        if v != "PAL":
            return REJECT
        if not st.signal_type.upper().startswith("ANALOG"):
            return REJECT          # 非模拟制式下 STANDARD 无意义 → NAK
        st.standard = "PAL"
        return NOBODY
    if k == "SIGNAL":
        if st.symbol_rate:
            return "*SIGNAL TYPE=%s SR=%dk" % (st.signal_type, int(st.symbol_rate))
        return "*SIGNAL TYPE=%s" % st.signal_type

    # ---------------- LNB ----------------
    if k.startswith("LNB ENABLE="):
        v = body.split("=", 1)[1].strip().upper()
        if v not in ("ON", "OFF"):
            return REJECT
        return NOBODY
    if k.startswith("LNB OUTPUT="):
        v = body.split("=", 1)[1].strip()
        # 🔴 真机只认 LNB_AVAILABLE 里的值；0 / 0V / 13V 一律 NAK
        if v.upper() not in [x.upper() for x in LNB_AVAILABLE]:
            return REJECT
        return NOBODY
    if k == "LNB":
        return ("*LNB AVAILABLE={%s} OUTPUT=OFF ENABLE=OFF"
                % ",".join(LNB_AVAILABLE))
    if k == "LNB AVAILABLE":
        return "*LNB AVAILABLE={%s}" % ",".join(LNB_AVAILABLE)

    # ---------------- 安装包 / 频道表 ----------------
    if k == "INSTALLATION":
        # 真机实测：只有 NUMBER=nn，**不含** TER-CH/SAT-CH
        return "*INSTALLATION NUMBER=2"
    if k.startswith("INSTALLATION CURRENT"):
        # 真机实测：必须不带参数；带 TER-CH/SAT-CH 的写法 NAK
        if re.search(r"(TER|SAT)-CH", body, re.I):
            return REJECT
        return ("*INSTALLATION NAME=DEFAULT TER-CH=%d SAT-CH=%d DISEQC_PROGRAM=0\n"
                "PNG=723 DATALOGGER=18 SP=0 ECHOES=0 CONSTELLATION=0 MER=0 LNB=0"
                % (len(PLANS["TER"]), len(PLANS["SAT"])))

    return REJECT


def _valid_sat_if(khz):
    """卫星中频合法性：必须落在某张 SAT 表的中频附近（否则 NAK）"""
    return nearest_point("SAT", khz, tol_khz=6000) is not None


def _do_tune(khz):
    st = STATE
    st.freq_khz = khz
    st.tune_at = time.time()
    st.tune_gen += 1
    # 同步 *?TUNE CH 的显示值
    pt = nearest_point(st.band, khz)
    if pt:
        st.plan = pt["plan"]
        st.ch_name = pt["name"]


def _reset_lock():
    """解调参数变化会让解调器重新捕获 → 重置锁定计时"""
    STATE.tune_at = time.time()


def _measure():
    st = STATE
    lock_time = STATE.lock_time
    pt = nearest_point(st.band, st.freq_khz)
    attempts = st.next_attempt()

    pt_analog = bool(pt and pt["kind"] == "analog")

    # ---- 信号强度：只有落在频点上才有信号 ----
    # ⚠️ 2026-10-13 用户实测纠正：本机仪器**只靠正确频点就能自动搜出符号率/
    #    制式**，所以 SR 设不设、设对设错都**不影响能否锁定**（整表无
    #    SYMBOLRATE，8 个频点全部一次锁定）。锁定的唯一决定因素是**等待时间**。
    #    所以这里不再用 SR 匹配与否来否决锁定。
    has_signal = pt is not None

    # ---- 锁定 ----
    locked = st.is_locked(lock_time)
    if pt_analog:
        locked = False      # 模拟制式永不锁定

    # ---- 注入失败：按真机实测给「个别卫星频道」加慢锁 ----
    # 射频一律一次锁（不浪费时间）；只有卫星个别频道
    # 频点需要多次尝试。--fail-first 仍保留（用于强行测重试路径）。
    _dbg_info = None
    if st.fail_first and attempts <= st.fail_first:
        locked = False
        _dbg_info = "fail_first"
    elif pt is not None:
        slow = slow_lock_attempts(st.band, pt.get("if_mhz"))
        _dbg_info = "band=%s if=%s slow=%s attempt=%s" % (
            st.band, pt.get("if_mhz"), slow, attempts)
        if slow and attempts <= slow:
            locked = False
    if _SLOW_DBG:
        sys.stderr.write("[slow-dbg] %s -> LOCKED=%s\n"
                         % (_dbg_info, "ON" if locked else "OFF"))

    # ---- 计算读数 ----
    if has_signal or pt_analog:
        level, cn = LEVEL_DBUV, CN_DB
        if not locked and pt_analog:
            pass                       # 模拟：有载波就有读数，不依赖锁定
        elif not locked:
            level, cn = LEVEL_DBUV - 62.0, NOISE_CN     # 未锁：只有噪底
    else:
        level, cn = NOISE_LEVEL, NOISE_CN

    mer = MER_DB if locked else 0.0
    lk = "ON" if locked else "OFF"

    # 电平单位跟随 *?UNITS 设置
    u = st.units_ter if st.band == "TER" else st.units_sat
    lu = "dBuV" if u in ("DBUV", "DBUB") else "dBmV"

    if pt_analog or (pt and str(pt["signal"]).upper().startswith("ANALOG")):
        # 模拟制式：真机直接返回 LEVEL，且永不 LOCKED=ON
        return ("*MEASURE LEVEL=%.2f %s CN=%.2f dB LOCKED=%s LM=13.8 dB "
                "OFFSET=1.5E+04 Hz BAND_POWER=%.2f %s"
                % (level, lu, cn, lk, level - 7.0, lu))
    # 数字 / 卫星：没有 LEVEL，电平落在 POWER / CH_POWER
    return ("*MEASURE POWER=%.2f %s CH_POWER=%.2f %s CN=%.2f dB MER=%.2f dB "
            "CBER<1.000E-06 LBER!0 VBER<1.000E-08 SER!0 OFFSET=1.5E+04 Hz "
            "LOCKED=%s LM=13.8 dB BAND_POWER=%.2f %s"
            % (level, lu, level, lu, cn, mer, lk, level - 7.0, lu))


# ---------------------------------------------------------------- 连接处理
def handle(conn, addr):
    OPEN_COUNT[0] += 1
    if SINGLE and OPEN_COUNT[0] > 1:
        log("[%s] 拒绝：单会话模式（模拟真机只允许一个 telnet 会话）" % addr[0])
        try:
            conn.close()
        except OSError:
            pass
        OPEN_COUNT[0] -= 1
        return
    log("[%s] 连接建立" % addr[0])
    stop = threading.Event()

    def heartbeat():
        # 仪器每秒发 XON 表示就绪（客户端靠它判断"没死")
        while not stop.is_set():
            try:
                conn.sendall(bytes([XON]))
            except OSError:
                return
            stop.wait(1.0)

    threading.Thread(target=heartbeat, daemon=True).start()
    # 主动发一次 telnet 协商，检验客户端 IAC 处理（三字节应答）
    try:
        conn.sendall(bytes([IAC, WILL, 24, IAC, DO, 31]))
    except OSError:
        pass

    buf = bytearray()
    iac = 0
    iac_verb = 0
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            log("  <<RAW %r" % chunk)
            for b in chunk:
                if iac == 1:
                    iac_verb = b
                    iac = 3 if b == SB else 2
                    continue
                if iac == 2:
                    # IAC WONT/DONT <option> 三字节
                    try:
                        if iac_verb == DO:
                            conn.sendall(bytes([IAC, WONT, b]))
                        elif iac_verb == WILL:
                            conn.sendall(bytes([IAC, DONT, b]))
                    except OSError:
                        pass
                    iac = 0
                    continue
                if iac == 3:
                    iac = 0
                    continue
                if b == IAC:
                    iac = 1
                    continue
                buf.append(b)
            while CR in buf:
                i = buf.index(CR)
                line = bytes(buf[:i]).decode("latin-1").strip()
                del buf[:i + 1]
                if line.startswith("*"):
                    respond(conn, line)
                elif line:
                    log("     (忽略非命令行 %r)" % line)
    except OSError:
        pass
    finally:
        stop.set()
        try:
            conn.close()
        except OSError:
            pass
        OPEN_COUNT[0] -= 1
        log("[%s] 连接关闭" % addr[0])


def respond(conn, line):
    cmd = line[1:]
    query = cmd.startswith("?")
    body = cmd[1:] if query else cmd
    log("  RX %r  %s" % (line, "(query)" if query else "(order)"))

    if SLOW:
        time.sleep(SLOW)

    with LOCK:
        try:
            out = dispatch(body, query)
        except Exception as e:                       # 不让模拟器因解析崩掉
            log("  !! dispatch 异常: %r" % e)
            out = NOBODY

    try:
        conn.sendall(bytes([XOFF]))
        if out is REJECT:
            conn.sendall(bytes([NAK, XON]))
            log("  TX NAK")
            return
        conn.sendall(bytes([ACK]))
        if out is NOBODY or out is None:
            conn.sendall(bytes([XON]))
            log("  TX ACK (无响应体)")
            return
        time.sleep(0.05)
        conn.sendall(out.encode("ascii", "replace") + bytes([CR, LF]))
        conn.sendall(bytes([XON]))
        log("  TX %r" % out)
    except OSError:
        pass


# ---------------------------------------------------------------- main
def default_plans_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    ch = os.path.join(here, "CH")
    return ch if os.path.isdir(ch) else here


def main():
    global QUIET, SLOW, SINGLE, POINTS
    ap = argparse.ArgumentParser(
        description="PROMAX 仪器模拟器（真机等价版）",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--plans", default=None,
                    help="频道表目录或 .tgz（默认 ./CH）")
    ap.add_argument("--plan", action="append", default=None,
                    help="只加载包含该字符串的表（可多次）")
    ap.add_argument("--list-plans", action="store_true", help="列出加载的表后退出")
    ap.add_argument("--fail-first", type=int, default=0,
                    help="同一频点前 N 次测量返回 LOCKED=OFF，用于测重试逻辑")
    ap.add_argument("--lock-time", type=float, default=3.0,
                    help="调谐后需连续等待 N 秒才 LOCKED=ON（默认 3.0，"
                         "对齐现场 settle_sat；真机要数秒）")
    ap.add_argument("--quiet", action="store_true", help="不打印收发包")
    ap.add_argument("--slow", type=float, default=0.0,
                    help="每条响应额外延迟 N 秒（模拟真机响应慢）")
    ap.add_argument("--single", action="store_true",
                    help="只允许一个会话（复现真机单会话限制）")
    a = ap.parse_args()

    QUIET = a.quiet
    SLOW = a.slow
    SINGLE = a.single
    STATE.fail_first = a.fail_first
    STATE.lock_time = a.lock_time

    load_plan_dir(a.plans or default_plans_dir(), a.plan)
    POINTS = all_points()

    n_ter = sum(len(p["channels"]) for p in PLANS["TER"])
    n_sat = sum(len(p["channels"]) for p in PLANS["SAT"])
    print("频道表：TER %d 张 / %d 频点   SAT %d 张 / %d 频点"
          % (len(PLANS["TER"]), n_ter, len(PLANS["SAT"]), n_sat))
    if a.list_plans:
        for tag in ("TER", "SAT"):
            for p in PLANS[tag]:
                print("  [%s] %-30s %-28s %d 频点  %s"
                      % (tag, p["exact_file"], p["name"], len(p["channels"]),
                         p.get("lnb_loc_osc", "") or
                         ("模拟" if p["channels"] and
                          p["channels"][0]["signal"].upper().startswith("ANALOG")
                          else "数字")))
        return 0

    if a.fail_first:
        print("注入：每频点前 %d 次测量返回 LOCKED=OFF" % a.fail_first)
    print("锁定模型：调谐后连续等待 %.1fs 才 LOCKED=ON；重发 *TUNE 会重新计时"
          % a.lock_time)
    if SLOW_LOCK:
        demo = {k: v for k, v in SLOW_LOCK.items() if k[1] in DEMO_SLOW_IFS}
        field = {k: v for k, v in SLOW_LOCK.items() if k not in demo}
        if demo:
            print("慢锁注入（DEMO 演示表）：%s"
                  % "、".join("中频%dMHz×%d次" % (iff, n)
                              for (b, iff), n in sorted(demo.items())))
        if field:
            print("慢锁注入（现场实测标定）：%s"
                  % "、".join("中频%dMHz×%d次" % (iff, n)
                              for (b, iff), n in sorted(field.items())))
    print("NAK 复现：*TUNE PLAN= 恒 NAK / LNB OUTPUT=0 NAK / SIGNAL STANDARD=DK NAK")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.host, a.port))
    s.listen(5)
    print("Mock PROMAX listening on %s:%d (Ctrl+C 退出)" % (a.host, a.port))
    try:
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
