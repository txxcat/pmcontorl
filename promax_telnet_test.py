# -*- coding: utf-8 -*-
"""
PROMAX RANGER Neo / HD RANGER 远程控制测试程序
================================================
依据文档：《Remote control commands for HD RANGER & RANGER Neo family》v2.2 (2017-02)

链路：Ethernet (IPCTRL 口) → Telnet / 2222
协议：
    PC  →  '*' + 命令 + CR(0x0D)
    仪器→  XOFF(0x13) → ACK(0x06) 或 NAK(0x15) → [响应行] → XON(0x11)
    仪器每秒发一次 XON(0x11) 表示就绪

用法：
    # 交互模式（手动输入频率，回车确认）
    python promax_telnet_test.py --host 192.168.1.50

    # 单次自动测试
    python promax_telnet_test.py --host 192.168.1.50 --freq 855M

    # 多点连续测试 + 读取电平
    python promax_telnet_test.py --host 192.168.1.50 -f 474M -f 498M -f 2000M --measure

    # 任意原始命令
    python promax_telnet_test.py --host 192.168.1.50 --raw "*?VER"

依赖：仅标准库（Python 3.7+），无需 telnetlib（3.13 已移除）
"""

import argparse
import atexit
import re
import select
import socket
import struct
import sys
import time
import weakref

# 进程退出时的兜底：把所有还开着的连接用 SO_LINGER RST 关掉。
# 仪器只允许一个 telnet 会话；进程被强杀/异常退出时若只做普通 close，
# socket 只发 FIN，仪器可能卡在 FIN_WAIT_2 不释放会话，
# 下一次连接就会"连得上、命令发得出去、但收不到任何回复"（踩过多次）。
_OPEN_CONNS = weakref.WeakSet()


@atexit.register
def _close_all_conns():
    for c in list(_OPEN_CONNS):
        try:
            c.close()
        except Exception:
            pass

# ---------------------------------------------------------------- 协议常量
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
XON, XOFF, ACK, NAK = 0x11, 0x13, 0x06, 0x15
CR, LF = 0x0D, 0x0A

DEFAULT_PORT = 2222


class PromaxError(Exception):
    """仪器通讯异常"""


# ---------------------------------------------------------------- 频率解析
_FREQ_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG]?)\s*(?:Hz)?\s*$"
)


def parse_frequency(text, band=None, sat_threshold=950.0):
    """把用户输入解析成 (频段, 频命令符, 频率MHz, 原始命令)

    支持: 855 / 855M / 855 MHz / 855000K / 1.2G / 474.0
    频段默认按频率自动判定: >=950MHz 判为卫星(SAT)，否则地面(TER)
    """
    m = _FREQ_RE.match(text)
    if not m:
        raise ValueError("频率格式无法识别: %r（示例：855 / 855M / 855MHz / 855000K）" % text)

    value = float(m.group(1))
    unit = (m.group(2) or "M").upper()          # 缺省按 MHz
    factor = {"K": 1e-3, "M": 1.0, "G": 1e3}[unit]
    mhz = value * factor

    if band is None:
        band = "SAT" if mhz >= sat_threshold else "TER"
    band = band.upper()
    if band not in ("TER", "SAT"):
        raise ValueError("频段只能是 TER 或 SAT，收到 %r" % band)

    khz = int(round(mhz * 1000))
    if khz <= 0:
        raise ValueError("频率必须大于 0")
    return band, "%dK" % khz, mhz, "*TUNE BAND=%s FREQ=%dK" % (band, khz)


def parse_measure(text):
    """*MEASURE POWER=68.26 dBuV → (68.26, 'dBuV')

    真机实测(RANGERNeo 4 / 固件 31.6)还有两种格式：
      *MEASURE CN=19.67 dB     → 键名是 CN，不是 C/N
      *MEASURE OFFSET=1.5E+04 Hz → 指数记数法，且单位 Hz（频偏）
    """
    m = re.search(r"=\s*(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)\s*"
                  r"([A-Za-zµ]*)\s*$", text.strip())
    if not m:
        return None, None
    try:
        return float(m.group(1)), m.group(2)
    except ValueError:
        return None, None


# --------------------------------------------------------------- 真机方言适配
# 以下格式在 RANGERNeo 4（固件 31.6）实测确认，与 2017 版手册有出入，
# 手册格式仍然兼容，这里只做补充解析。
REAL_INSTALL_CURRENT = None   # 缓存 *?INSTALLATION CURRENT 的原始响应


# 各测量项的单位兜底表。
# 真机 `*?MEASURE` 全量响应里，多数频点带单位（MER=19.9 dB），
# 但**有些频点会省略单位或单位前没有空格**（实测 MER=43.4 / MER=43.4dB），
# 此时解析出的单位是空串，导致日志里有的有 dB 有的没有。
# 这里给一张兜底表：解析不到单位时按表补，保证显示一致。
DEFAULT_UNITS = {
    "POWER": "dBuV", "CH_POWER": "dBuV", "BAND_POWER": "dBuV",
    "LEVEL": "dBuV", "FIELD_STRENGTH": "dBuV/m",
    "CN": "dB", "C/N": "dB", "MER": "dB", "LM": "dB", "SNR": "dB",
    "OFFSET": "Hz",
    "CBER": "", "VBER": "", "LBER": "", "SER": "", "ESR": "", "PER": "",
    "LOCKED": "",
}

# 电平类单位的取值来源：仪器的 *?UNITS 设置（TER/SAT 可不同）
LEVEL_UNIT_KEYS = ("POWER", "CH_POWER", "BAND_POWER", "LEVEL")


def parse_measure_all(text, level_units=None):
    """解析 *?MEASURE 全量响应 → {键: (值, 单位)}

    真机实测样例：
      *MEASURE POWER=68.26 dBuV CH_POWER=68.26 dBuV CN=20.26 dB MER=19.9 dB
      CBER<1.000E-06 LBER!0 VBER<1.000E-08 SER!0 OFFSET=1.5E+04 Hz
      LOCKED=ON LM=13.8 dB ... BAND_POWER=83.2 dBuV

    注意与手册的三处差异：
      1. 键名是 CN / MER / POWER，不是 C/N（斜杠在命令里，响应里没有）
      2. 数值前有空格，分隔用单个空格而非多空格
      3. 存在 LOCKED / CH_POWER / BAND_POWER / LM 等手册未列的键

    level_units: 可选，仪器的电平单位设置（如 {"TER": "dBuV", "SAT": "dBmV"}），
                 用于在响应没带单位时兜底。
    """
    out = {}
    # 允许键名含字母数字下划线，值含指数/负号/小数/科学计数
    pat = r"([A-Z_][A-Z0-9_]*)\s*([<>=!]+)\s*" \
          r"(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?|[A-Z]+)"
    for m in re.finditer(pat, text):
        key, op, val = m.group(1), m.group(2), m.group(3)
        try:
            out[key] = (float(val), "")
        except ValueError:
            out[key] = (None, val)      # ON/OFF/ACQUIRED 之类
    # 补单位。
    # ⚠️ 两个坑都踩过：
    # ① 原来的 `\s+([A-Za-zµ%]+)` 会把**下一个键名**当成本项单位
    #    （`MER=43.4 CN=20.26` → MER 的单位变成 "CN"；
    #      `POWER=68.26 MER=19.9` → POWER 的单位变成 "MER"）
    # ② 要求必须有空格，仪器偶尔回 `MER=43.1dB`（无空格）时单位丢失
    # 解决：`\s*` 允许无空格 + 负向断言 `(?![A-Za-z0-9_]*\s*=)`
    #    排除"后面跟等号"的 token（那必然是键名，不是单位）
    unit_pat = re.compile(
        r"([A-Z_][A-Z0-9_]*)\s*=\s*"
        r"(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)"
        r"\s*([A-Za-zµ%/]+)"
        r"(?![A-Za-z0-9_]*\s*=)")
    for m in unit_pat.finditer(text):
        key, val, unit = m.group(1), float(m.group(2)), m.group(3)
        out[key] = (val, unit)
    # 单位兜底：响应里没带单位的，按 DEFAULT_UNITS 补
    for key, (val, unit) in list(out.items()):
        if unit:
            continue
        if key in LEVEL_UNIT_KEYS and level_units:
            u = level_units.get("TER") or level_units.get("SAT")
            if u:
                out[key] = (val, u)
                continue
        if key in DEFAULT_UNITS:
            out[key] = (val, DEFAULT_UNITS[key])
    return out


def parse_install_current(lines):
    """*?INSTALLATION CURRENT → dict

    真机实测（RANGERNeo 4）：
      *INSTALLATION NAME=<安装名> TER-CH=5 SAT-CH=18 DISEQC_PROGRAM=0
      PNG=723 DATALOGGER=18 SP=0 ECHOES=0 CONSTELLATION=0 MER=0

    ⚠️ 手册写的 `*?INSTALLATION CURRENT TER-CH[=nn]`（按 tag 取单项）在真机上
       返回 NAK；必须**不带参数**才能拿到全部字段。
    """
    text = " ".join(lines)
    out = {}
    for key in ("NAME", "TER-CH", "SAT-CH", "DISEQC_PROGRAM", "PNG",
                "DATALOGGER", "SP", "ECHOES", "CONSTELLATION", "MER",
                "NUMBER"):
        m = re.search(r"%s\s*=\s*([^\s]+)" % re.escape(key), text, re.I)
        if m:
            out[key] = m.group(1)
    return out


def parse_tune_ch(lines):
    """*?TUNE CH → (band, plan, ch)

    真机实测：*TUNE BAND=SAT PLAN=<频道表>.XML CH=3780
    ⚠️ PLAN 是**频道表 XML 文件名**，不是手册说的"简短代码"；
       CH 在卫星表下就是下行频率数值（3780MHz），不是频道编号。
    """
    text = " ".join(lines)
    b = re.search(r"BAND\s*=\s*([A-Z]+)", text, re.I)
    p = re.search(r"PLAN\s*=\s*(\S+)", text, re.I)
    c = re.search(r"\bCH\s*=\s*([A-Za-z0-9_\-]+)", text, re.I)
    return (b.group(1).upper() if b else None,
            p.group(1) if p else None,
            c.group(1) if c else None)


def parse_tune(lines):
    """*?TUNE → dict，含真机特有的 DL 与 MODE

    真机实测：*TUNE BAND=SAT FREQ=1370000K DL=3780MHz MODE=FREQ
    """
    text = " ".join(lines)
    out = {"raw": text.strip()}
    b = re.search(r"BAND\s*=\s*([A-Z]+)", text, re.I)
    f = re.search(r"FREQ\s*=\s*([0-9.]+)\s*([KMG]?)", text, re.I)
    dl = re.search(r"DL\s*=\s*([0-9.]+)\s*([KMG]?)", text, re.I)
    md = re.search(r"MODE\s*=\s*([A-Z]+)", text, re.I)
    out["band"] = b.group(1).upper() if b else None
    if f:
        out["khz"] = float(f.group(1)) * {"": 1, "K": 1, "M": 1000, "G": 1e6}[f.group(2).upper()]
    if dl:
        out["dl_mhz"] = float(dl.group(1)) * {"": 1, "K": 0.001, "M": 1, "G": 1000}[dl.group(2).upper()]
    out["mode"] = md.group(1).upper() if md else None
    return out


# ---------------------------------------------------------------- Telnet 客户端
class PromaxTelnet(object):
    """PROMAX 仪器 Telnet 远程控制客户端（裸 socket 实现，处理 IAC 协商）"""

    def __init__(self, host, port=DEFAULT_PORT, timeout=20.0, verbose=False,
                 fast=False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.fast = fast         # 快速模式：遍历频道表时用，缩短各阶段等待
        # 登记到全局表，进程退出时由 atexit 兜底关闭（见文件头）
        try:
            _OPEN_CONNS.add(self)
        except TypeError:
            pass
        self._sock = None
        self._buf = bytearray()
        self._lines = []       # 响应行持久缓冲，配合 mark 使用避免竞态丢行
        self._raw = bytearray()  # 本次命令收到的原始字节（含控制字符），用于排查
        self._iac = 0        # 0=普通 1=刚收到IAC 2=收到verb 3=SB内
        self._iac_verb = 0

    # -------------------------------------------------- 连接
    def connect(self):
        last = None
        for family, socktype, proto, _, addr in socket.getaddrinfo(
                self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM):
            s = socket.socket(family, socktype, proto)
            try:
                s.settimeout(self.timeout)
                s.connect(addr)
                self._sock = s
                break
            except OSError as e:
                last = e
                s.close()
        if self._sock is None:
            raise PromaxError("无法连接 %s:%d — %s" % (self.host, self.port, last))
        self._sock.setblocking(False)
        self._log("已连接 %s:%d" % (self.host, self.port))
        return self

    def close(self):
        """关闭连接。

        实测问题：只做 shutdown(SHUT_RDWR) 时，若仪器不回 FIN，socket 会长期
        卡在 FIN_WAIT_2。仪器多��只允许一个 telnet 会话，残留连接会导致
        后续连接全部无响应（连得上、命令发得出去、收不到任何回复）。
        因此关闭时设置 SO_LINGER 超时，强制发 RST 立即释放。
        """
        if self._sock is not None:
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                      struct.pack("ii", 1, 0))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            _OPEN_CONNS.discard(self)
        except TypeError:
            pass
        self._log("连接已关闭")

    def __del__(self):
        # 对象被回收时兜底关一次（正常情况下 close() 已经关过，这里不会重复发）
        try:
            if getattr(self, "_sock", None) is not None:
                self.close()
        except Exception:
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # -------------------------------------------------- 底层收发
    def _log(self, msg):
        if self.verbose:
            print("    · %s" % msg)

    def _read_chunk(self, wait=0.2):
        """读一批字节；无数据返回 b''，对端关闭返回 None

        用 select 而非 settimeout：settimeout 在"部分读取后超时"场景下会让
        socket 进入不可恢复状态并丢数据（Python 文档明确不建议），而本程序
        恰恰依赖大量短超时轮询，必须走 select。
        """
        if self._sock is None:
            raise PromaxError("连接未建立")
        try:
            r, _, _ = select.select([self._sock], [], [], max(0.0, wait))
        except (OSError, ValueError) as e:
            raise PromaxError("select 失败: %s" % e)
        if not r:
            return b""
        try:
            chunk = self._sock.recv(4096)
        except OSError as e:
            raise PromaxError("读取失败: %s" % e)
        return chunk if chunk else None

    def _send_raw(self, data):
        if self._sock is None:
            raise PromaxError("连接未建立")
        try:
            self._sock.sendall(data)
        except OSError as e:
            raise PromaxError("发送失败: %s" % e)
        self._log("TX %r" % data)

    def _feed(self, chunk, events):
        """解析一批字节：剥离 telnet IAC 协商，抽取控制字符，文本行入持久缓冲"""
        self._raw.extend(chunk)
        for b in chunk:
            if self._iac == 1:                      # 收到 IAC，等 verb
                self._iac_verb = b
                self._iac = 3 if b == SB else 2
                continue
            if self._iac == 2:                      # IAC + verb，此时 b 是选项字节
                if b == IAC:                         # 转义的双 IAC
                    self._iac = 0
                    continue
                if self._iac_verb in (DO, DONT, WILL, WONT):
                    # 一律回"拒绝"。注意 IAC WONT/DONT <option> 是三字节命令，
                    # 必须把选项字节 b 一并回出，否则对端解析会错位。
                    reply = {DO: WONT, WILL: DONT, DONT: WONT, WONT: DONT}[self._iac_verb]
                    try:
                        self._sock.sendall(bytes([IAC, reply, b]))
                    except OSError:
                        pass
                self._iac = 0
                continue
            if self._iac == 3:                      # 子协商，丢弃到 SE
                if b == IAC:
                    self._iac = 4
                continue
            if self._iac == 4:
                self._iac = 0 if b == SE else 3
                continue

            if b == IAC:
                self._iac = 1
                continue
            if b == XON:
                events.add("XON")
            elif b == XOFF:
                events.add("XOFF")
            elif b == ACK:
                events.add("ACK")
            elif b == NAK:
                events.add("NAK")
            elif b in (CR, LF):
                self._flush_line()
            elif b != 0x00:
                self._buf.append(b)

    def _flush_line(self):
        if self._buf:
            self._lines.append(self._buf.decode("latin-1").strip())
            self._buf.clear()

    def _pump(self, duration):
        """在 duration 内持续读取；返回收到的事件集合"""
        events = set()
        end = time.time() + duration
        while True:
            remain = end - time.time()
            if remain <= 0:
                break
            chunk = self._read_chunk(min(0.2, remain))
            if chunk is None:
                raise PromaxError("仪器主动断开连接")
            if chunk:
                self._feed(chunk, events)
        return events

    def _collect(self, idle=1.0, timeout=20.0, min_wait=0.0, lines_wanted=1,
                 multi_line_grace=0.0):
        """读取直到「拿到完整响应行」或静默/超时。

        ⚠️ 关键：仪器**每秒发一次心跳 XON**，若用"静默"判断结束，
        心跳会不断刷新最后收包时间，导致永远等不到静默 → 每条命令都跑满
        timeout（真机 20 秒时，遍历 6 个频道就要 2 分钟，必超时卡死）。
        响应行以 CR(0x0D)+LF(0x0A) 结束，所以以**收完整行**作为结束标志，
        心跳不算数据。

        实测：ACK 与响应行几乎同时到达（waitACK 0.000s），
        所以默认 multi_line_grace=0 不再为"可能的第二行"白等 0.3s。
        仅当调用方明确知道会返回多行（如 `*?INSTALLATION CURRENT`）时
        才传正数 grace。
        """
        events = set()
        start = last = time.time()
        got = 0
        grace_from = 0
        while True:
            now = time.time()
            if (now - start) >= min_wait and got >= lines_wanted:
                if multi_line_grace <= 0:
                    break
                if grace_from == 0:
                    grace_from = now
                elif now - grace_from > multi_line_grace:
                    break
            if (now - last) > idle or (now - start) > timeout:
                break
            chunk = self._read_chunk(min(0.2, max(0.02, timeout - (now - start))))
            if chunk is None:
                raise PromaxError("仪器主动断开连接")
            if chunk:
                before = len(self._lines)
                self._feed(chunk, events)
                added = len(self._lines) - before
                if added:
                    got += added
                # 只有收到真实数据才刷新 last；纯心跳不算
                if added or not self._only_heartbeat(chunk):
                    last = time.time()
        return events

    @staticmethod
    def _only_heartbeat(chunk):
        """判断一段字节是否只含控制字符（心跳 XON 等），不含文本数据"""
        for b in chunk:
            if b in (XON, XOFF, ACK, NAK, CR, LF):
                continue
            return False
        return True
        return events

    def _wait_event(self, wanted, timeout):
        """等待指定事件；期间收到的文本行保留在持久缓冲，不会丢失"""
        events = set()
        start = time.time()
        while not (events & wanted):
            remain = timeout - (time.time() - start)
            if remain <= 0:
                break
            chunk = self._read_chunk(min(0.2, remain))
            if chunk is None:
                raise PromaxError("仪器主动断开连接")
            if chunk:
                self._feed(chunk, events)
        return events

    # -------------------------------------------------- 命令层
    def command(self, cmd, want_response=True, timeout=20.0, settle=1.2):
        """发送一条命令。

        cmd: 不含前导 '*'、不含 CR，例如 '?TUNE' 或 'TUNE BAND=TER FREQ=855000K'
        返回 {'ack':bool, 'lines':[...], 'events':set, 'raw':bytes, 'sent':bytes}

        fast=True 用于频道表遍历这类上百次连续查询：大幅缩短各阶段等待。
        仪器响应实测在 50ms 内到达，0.2s 静默窗口足够，
        否则每条命令要2~4 秒，遍历上百频道不可用。
        """
        cmd = cmd.strip()
        if cmd.startswith("*"):
            cmd = cmd[1:]
        payload = ("*" + cmd).encode("ascii", "replace") + bytes([CR])

        fast = self.fast
        # 注意：真机（RANGERNeo 4）单条命令响应可达数秒，且 *?MEASURE 全量响应
        # 有 200+ 字节。fast 模式虽缩短等待，但 min_wait/静默仍要留够时间，
        # 否则响应读不完整就被判成"没有响应"（表现为遍历中途卡死）。
        # 心跳 bug 已修（以 CR/LF 帧结束为判据），这里的窗口只需覆盖
        # "仪器算好测量值"的时间。0.4s 足够，1.2s 是历史遗留的过度保守值
        # （会让 46 频道遍历从 30 秒变成 100+ 秒）。
        t_drain = 0.05 if fast else 0.15# 清残留
        t_ready = 0.0 if fast else 0.4       # 等 XON
        # ⚠️ ACK 等待不能用 min(3.0, timeout) 这类短上限：真机（RANGERNeo 4）
        # 单条命令要数秒，3 秒必超时，会把"响应慢"误判成 NAK。
        # _wait_event 是事件驱动，收到 ACK 立即返回，所以给足 timeout 不会变慢，
        # 只在仪器真不响应时才等满。
        t_ack = timeout                      # 等 ACK
        t_min = 0.0 if fast else 0.4         # 响应最小等待（fast 下 ACK 与响应同时到）
        t_settle = max(settle, 0.4) if fast else settle
        t_tail = 0.05 if fast else 0.2       # 收尾

        # 0) 清掉上一条命令的残留行，保证本次响应边界清晰
        self._pump(t_drain)
        self._buf.clear()
        mark = len(self._lines)

        # 1) 短暂等待就绪的 XON（等不到也照发，多数型号容错）
        if t_ready:
            self._wait_event({"XON"}, t_ready)

        # 2) 发命令（发完后重新打标记：此前的行均属历史）
        self._raw.clear()
        self._send_raw(payload)
        mark = len(self._lines)

        # 3) 等 ACK / NAK
        events = self._wait_event({"ACK", "NAK"}, t_ack)
        if "NAK" in events and "ACK" not in events:
            self._log("收到 NAK，命令被拒绝")
            return {"ack": False, "status": "nak", "lines": [], "events": events,
                    "raw": bytes(self._raw), "sent": payload}
        if "ACK" not in events:
            # 既没 ACK 也没 NAK —— 是超时无响应，不是仪器拒绝。
            # 常见原因：仪器忙、单会话被占、前面板在别的界面、响应慢于超时。
            self._log("等待 ACK 超时（%.1fs），命令可能未被仪器接收" % t_ack)
            return {"ack": False, "status": "timeout", "lines": [],
                    "events": events, "raw": bytes(self._raw),
                    "sent": payload}

        # 4) 读响应行。判据是「收到完整行（CR+LF）」，心跳 XON 不算数据；
        #    multi_line_grace 仅在已知返回多行的命令上传正数（如 INSTALLATION）。
        if want_response and "ACK" in events:
            multi = 0.25 if cmd.upper().startswith("?INSTALLATION") else 0.0
            more = self._collect(idle=t_settle, timeout=timeout,
                                 min_wait=t_min, multi_line_grace=multi)
            events |= more

        # 5) 消化收尾（未收到结束符的行也补进缓冲）
        self._pump(t_tail)
        self._flush_line()

        lines = [l for l in self._lines[mark:] if l]
        return {"ack": "ACK" in events, "status": "ack", "lines": lines,
                "events": events, "raw": bytes(self._raw), "sent": payload}

    @staticmethod
    def render_raw(raw):
        """把原始字节渲染成可读形式：控制字符转成可读记号"""
        out = []
        for b in raw:
            if b == XON:
                out.append("<XON>")
            elif b == XOFF:
                out.append("<XOFF>")
            elif b == ACK:
                out.append("<ACK>")
            elif b == NAK:
                out.append("<NAK>")
            elif b == CR:
                out.append("<CR>")
            elif b == LF:
                out.append("<LF>")
            elif 32 <= b < 127:
                out.append(chr(b))
            else:
                out.append("<%02X>" % b)
        return "".join(out)

    def query(self, cmd, timeout=20.0):
        r = self.command(cmd, want_response=True, timeout=timeout)
        return r["lines"]


# ---------------------------------------------------------------- 业务封装
class Promax(object):
    """PROMAX 仪器高层操作"""

    def __init__(self, conn):
        self.c = conn

    def _log(self, msg):
        self.c._log(msg)

    # --- 基本信息 ---
    def ver(self):
        return self.c.query("?VER")

    def nam(self):
        return self.c.query("?NAM")

    def sn(self):
        return self.c.query("?EQUIPMENT SN")

    # --- 频道表 / 频道 ---
    def installation(self):
        """*?INSTALLATION → 当前安装包汇总（含 TER-CH=nn / SAT-CH=nn 数量）"""
        return self.c.query("?INSTALLATION")

    def installation_current(self, tag=""):
        """*?INSTALLATION CURRENT → 当前安装包全部字段

        ⚠️ 真机实测（RANGERNeo 4 / 固件 31.6）：**必须不带参数**。
        手册写的 `?INSTALLATION CURRENT TER-CH[=nn]` 在真机上返回 NAK。
        """
        if tag:
            self._log("真机上带 tag 的写法不被接受，改用无参数查询")
        return self.c.query("?INSTALLATION CURRENT")

    def tune_ch(self):
        """*?TUNE CH → *TUNE BAND=xx PLAN=<XML文件名> CH=xxx"""
        return self.c.query("?TUNE CH")

    def tune_plan(self, plan):
        """*TUNE PLAN=<频道表XML文件名>

        ⚠️ 真机实测 PLAN 是**XML 文件名**（如 DEMO-C-Band.XML），
        不是手册说的简短代码。
        """
        return self.c.command("TUNE PLAN=%s" % plan, want_response=False)

    def tune_ch_step(self, direction="NEXT"):
        """*TUNE CH NEXT / CH PREV"""
        return self.c.command("TUNE CH %s" % direction, want_response=False)

    def units(self):
        return self.c.query("?UNITS")

    # --- 测量（真机方言）---
    def measure_all(self):
        """*?MEASURE → (原始文本, 解析后的 {键: (值,单位)})

        真机实测：`*?MEASURE` 一次返回全部测量项，且**带参数的
        `*?MEASURE C/N` / `*?MEASURE LEVEL` 会被忽略参数、仍返回全部**。
        所以统一读全量再按需取键，比逐项查询快得多（一次 vs N 次往返）。
        """
        lines = self.c.query("?MEASURE")
        text = " ".join(lines)
        return text, parse_measure_all(text)

    def measure_one(self, key):
        """按键取测量值，兼容真机（CN 而非 C/N）"""
        text, allv = self.measure_all()
        if key in allv:
            return allv[key], text
        alt = {"C/N": "CN", "CN": "CN", "LEVEL": "POWER"}.get(key.upper())
        if alt and alt in allv:
            return allv[alt], text
        return None, text

    # --- 调谐 ---
    def tune_mode(self, mode="FREQ"):
        return self.c.command("TUNE MODE=%s" % mode, want_response=False)

    def set_band(self, band):
        """*TUNE BAND=TER|SAT —— 只切频段（不指定频率）"""
        return self.c.command("TUNE BAND=%s" % band, want_response=False)

    def set_tune(self, band, freq_cmd, fallback=True):
        """下发 *TUNE BAND=xx FREQ=nnnK；失败时自动拆成两条命令重试"""
        r = self.c.command("TUNE BAND=%s FREQ=%s" % (band, freq_cmd),
                           want_response=False)
        if r["ack"] or not fallback:
            return r
        self._log("合并命令未 ACK，拆分重试")
        self.c.command("TUNE BAND=%s" % band, want_response=False)
        r2 = self.c.command("TUNE FREQ=%s" % freq_cmd, want_response=False)
        return r2

    def get_tune(self):
        return self.c.query("?TUNE")

    # --- 测量 ---
    def measure(self, item=""):
        return self.c.query("?MEASURE %s" % item if item else "?MEASURE")

    # --- 频段相关配置（TER=射频测试 / SAT=卫星天线测试）---
    def signal_type(self, value):
        """*SIGNAL TYPE=ANALOG|DVB-C|DVB-T|DVB-S|DVB-S2"""
        return self.c.command("SIGNAL TYPE=%s" % value, want_response=False)

    def signal_standard(self, value):
        """*SIGNAL STANDARD=PAL|NTSC|SECAM"""
        return self.c.command("SIGNAL STANDARD=%s" % value, want_response=False)

    def signal_sr(self, sr_k):
        """*SIGNAL SR=nnnnk —— 符号率(kSymb/s)"""
        return self.c.command("SIGNAL SR=%sk" % sr_k, want_response=False)

    def set_sat_params(self, sig_type, sr_k):
        """设卫星解调参数（制式 + 符号率），返回 [(label, result), ...]

        🔴 真机实测（VER 31.17）两个坑：
        1. `TYPE=` 和 `SR=` **不能合并成一条**：
             *SIGNAL TYPE=DVB-S2 SR=15950k  → NAK
             *SIGNAL TYPE=DVB-S2            → ACK
             *SIGNAL SR=15950k              → ACK
        2. **发 `SR=` 会把 TYPE 重置成 DVB-S**：
             *SIGNAL TYPE=DVB-S2  → TYPE=DVB-S2 SR=10490k
             *SIGNAL SR=15950k    → TYPE=DVB-S  SR=15950k   ← TYPE 被改回 DVB-S
        所以**顺序必须是 SR 先、TYPE 后**，最终状态才是"想要的制式 + 想要的符号率"。

        不设对符号率会锁不上 —— 实测某频道(中频1110MHz) 在 SR=21370k 时
        LOCKED=OFF，改成表载的 9200k 后 LOCKED=ON / MER=19.7。
        """
        steps = []
        if sr_k:
            steps.append(("SIGNAL SR=%dk" % int(round(sr_k)),
                          self.signal_sr(int(round(sr_k)))))
        if sig_type:
            t = "DVB-S2" if "S2" in str(sig_type).upper() else "DVB-S"
            steps.append(("SIGNAL TYPE=%s" % t, self.signal_type(t)))
        return steps

    def lnb_enable(self, on=True):
        """*LNB ENABLE=ON|OFF"""
        return self.c.command("LNB ENABLE=%s" % ("ON" if on else "OFF"),
                              want_response=False)

    def lnb_output(self, value):
        """*LNB OUTPUT=nnn（本项目现场固定为 0）"""
        return self.c.command("LNB OUTPUT=%s" % value, want_response=False)

    def units(self, ter=None, sat=None):
        """*UNITS TER=DBUB / *UNITS SAT=DBMV"""
        out = []
        if ter:
            out.append(self.c.command("UNITS TER=%s" % ter, want_response=False))
        if sat:
            out.append(self.c.command("UNITS SAT=%s" % sat, want_response=False))
        return out

    def apply_profile(self, band, analog=True, lnb_output=0, raw_print=False):
        """按频段下发完整配置。band: 'TER' | 'SAT'

        TER 射频测试:   SIGNAL TYPE=ANALOG(或 DVB-C) + SIGNAL STANDARD=PAL
        SAT 卫星天线:   SIGNAL TYPE=DVB-S2  + LNB ENABLE=ON + LNB OUTPUT=nnn
        返回下发摘要行；raw_print=True 时附带收发的原始字节

        ⚠️ 真机实测（RANGERNeo 4 / 固件 31.6）：**必须在设TYPE 之后才设 STANDARD**。
           若仪器当前是 DVB-S/DVB-S2，直接发 `*SIGNAL STANDARD=PAL` 会 NAK
           （该字段只对 ANALOG 有意义）。所以这里把 STANDARD 放最后，
           且 NAK 只提示不中断流程。
        """
        steps = []
        if band == "TER":
            st = "ANALOG" if analog else "DVB-C"
            steps.append(("SIGNAL TYPE=%s" % st, self.signal_type(st)))
            # 仅模拟制式需要 PAL；数字制式设 STANDARD 会被 NAK
            if analog:
                steps.append(("SIGNAL STANDARD=PAL", self.signal_standard("PAL")))
        elif band == "SAT":
            # 真机实测 DVB-S2 会被接受；此处保持原口径
            steps.append(("SIGNAL TYPE=DVB-S2", self.signal_type("DVB-S2")))
            steps.append(("LNB ENABLE=ON", self.lnb_enable(True)))
            # ⚠️ LNB OUTPUT 只认**电压值或 OFF**：
            #    *?LNB -> AVAILABLE={OFF,5.0,13.0,13.0+22kHz,15.0,18.0,18.0+22kHz}
            #    传 0 / 0V / 13V 一律 NAK（实测），0V 要写 OFF
            lo_v = str(lnb_output).strip()
            if lo_v.upper() in ("0", "0V", "0.0", "NONE", "", "N/A"):
                lo_v = "OFF"
            steps.append(("LNB OUTPUT=%s" % lo_v, self.lnb_output(lo_v)))
        lines = []
        for label, r in steps:
            # status: ack / nak / timeout —— 三者含义不同，不能都显示成 NAK
            st = r.get("status", "ack" if r["ack"] else "nak")
            mark = {"ack": "ACK", "nak": "NAK",
                    "timeout": "无响应(超时)"}.get(st, st)
            note = ""
            if st == "nak" and "STANDARD" in label:
                note = "  (当前非模拟制式会 NAK，可忽略)"
            elif st == "timeout":
                note = "  (仪器未回应，检查是否忙/会话被占/前面板界面)"
            lines.append("  配置 %-24s -> %s%s" % (label, mark, note))
            if raw_print:
                lines.append("       TX %s" % self.c.render_raw(r["sent"]))
                lines.append("       RX %s" % self.c.render_raw(r.get("raw", b"")))
        return lines


def describe_tune(lines):
    """解析 *TUNE BAND=xx FREQ=nnnK → (band, khz, MHz)"""
    raw = " ".join(lines)
    b = re.search(r"BAND\s*=\s*([A-Z]+)", raw, re.I)
    f = re.search(r"FREQ\s*=\s*([0-9.]+)\s*([KMG]?)", raw, re.I)
    band = b.group(1).upper() if b else "?"
    khz = None
    if f:
        khz = float(f.group(1)) * {"": 1, "K": 1, "M": 1000, "G": 1e6}[f.group(2).upper()]
    return raw.strip(), band, khz


# ---------------------------------------------------------------- 交互流程
def do_connect(host, port, timeout, verbose):
    print("正在连接 %s:%d ..." % (host, port))
    conn = PromaxTelnet(host, port, timeout, verbose)
    conn.connect()
    inst = Promax(conn)

    print("握手验证：")
    for label, fn in (("软件版本 *?VER", inst.ver),
                      ("设备名称 *?NAM", inst.nam),
                      ("序列号 *?EQUIPMENT SN", inst.sn)):
        try:
            out = fn()
            print("  %-22s -> %s" % (label, out[0] if out else "(空响应)"))
        except PromaxError as e:
            print("  %-22s -> 失败: %s" % (label, e))
    try:
        u = inst.units()
        if u:
            print("  %-22s -> %s" % ("单位 *?UNITS", u[0]))
    except PromaxError:
        pass
    print("连接正常。\n")
    return conn, inst


def measure_items(band, analog=True):
    """按频段/制式选择要**显示**的测量项（现场口径）

    TER 射频 + 模拟 ANALOG : LEVEL + C/N
    TER 射频 + 数字 DVB-C  : POWER + MER
    SAT 卫星天线: POWER + C/N + MER
    SAT 额外读频偏 OFFSET

    注意：这些键名是**显示用**的。实际读取时统一走 `*?MEASURE` 全量响应再取键
    （真机实测带参数的 `*?MEASURE C/N` 会忽略参数仍返回全部）。
    """
    if band == "TER":
        return ["LEVEL", "C/N"] if analog else ["POWER", "MER"]
    return ["POWER", "C/N", "MER"]


def tune_once(inst, freq_text, band=None, measure=False, settle=0.8,
              analog=True, lnb_output=0, profile=True, offset_cmd="OFFSET",
              raw_print=False):
    """执行一次：按频段配置 → 设置频率 → 回读 → （可选）读测量值

    band=None 时按频率自动判定：>=950MHz → SAT(卫星)，否则 TER(射频)
    raw_print=True 时打印每条命令收发的原始字节（控制字符转义显示）
    """
    try:
        b, fcmd, mhz, raw_cmd = parse_frequency(freq_text, band)
    except ValueError as e:
        print("  [输入错误] %s" % e)
        return False

    kind = "卫星天线测试" if b == "SAT" else "射频测试"
    print("目标频率: %.3f MHz  频段: %s  [%s]  (%s)" % (mhz, b, kind, raw_cmd))

    render = inst.c.render_raw
    if profile:
        for line in inst.apply_profile(b, analog=analog, lnb_output=lnb_output,
                                       raw_print=raw_print):
            print(line)

    r0 = inst.tune_mode("FREQ")
    r = inst.set_tune(b, fcmd)
    if raw_print:
        if r0.get("raw") is not None:
            print("       TX %s" % render(r0["sent"]))
            print("       RX %s" % render(r0.get("raw", b"")))
        print("       TX %s" % render(r["sent"]))
        print("       RX %s" % render(r.get("raw", b"")))
    if not r["ack"]:
        print("  [失败] 仪器返回 NAK，命令格式不被接受")
        return False
    print("  设置 -> ACK")

    if settle > 0:
        time.sleep(settle)          # 等待锁频/调谐完成

    rt = inst.c.command("?TUNE")
    lines = rt["lines"]
    if raw_print:
        print("       TX %s" % render(rt["sent"]))
        print("       RX %s" % render(rt.get("raw", b"")))
    if not lines:
        print("  [失败] *?TUNE 无响应")
        return False
    raw, rb, rkhz = describe_tune(lines)
    print("  回读 *?TUNE -> %s" % raw)
    if rkhz is not None:
        ok = abs(rkhz - mhz * 1000) < 1
        print("  校验: 期望 %.0f kHz / 实际 %.0f kHz  %s"
              % (mhz * 1000, rkhz, "一致" if ok else "不一致"))
    else:
        print("  校验: 响应中未解析到 FREQ 字段")

    if measure:
        # 真机实测：`*?MEASURE` 一次返回全部，带参数会被忽略仍返回全部。
        # 所以统一读一次全量再按需取键，比逐项查询快得多（1 次 vs N 次往返），
        # 也避免逐项查询时仪器反复重算导致的响应超时。
        rr = inst.c.command("?MEASURE")
        text = " ".join(rr["lines"])
        if raw_print:
            print("       TX %s" % render(rr["sent"]))
            print("       RX %s" % render(rr.get("raw", b"")))
        if not text:
            print("  测量 *?MEASURE -> 无响应 (NAK 或未返回)")
            return True
        allv = parse_measure_all(text)
        # 锁频状态：卫星/数字下没锁定时读数全是噪声底，必须显式提示
        if "LOCKED" in allv:
            lock = allv["LOCKED"][1] or allv["LOCKED"][0]
            print("  锁频 LOCKED=%s" % lock)
        for item in measure_items(b, analog):
            # 显示用键名 → 真机响应里的实际键名
            key = {"C/N": "CN", "LEVEL": "POWER"}.get(item, item)
            if key in allv:
                val, unit = allv[key]
                print("  测量 %-6s= %s %s" % (key, val, unit))
            else:
                print("  测量 %-6s= 无该项（本次响应未包含）" % key)
        if b == "SAT" and offset_cmd:
            # 频偏（Power Offset）。真机实测格式：OFFSET=1.5E+04 Hz
            key = offset_cmd
            if key in allv:
                val, unit = allv[key]
                if val is not None:
                    print("  频偏 %-6s= %g %s  (%.3f kHz)"
                          % (key, val, unit or "Hz", val / 1000.0))
                else:
                    print("  频偏 %-6s= %s" % (key, val))
            else:
                print("  频偏 %-6s= 本次响应未包含" % key)
    return True


def interactive(conn, inst, default_band, measure, analog=True, lnb_output=0,
                offset_cmd="OFFSET", raw_print=False):
    print("交互模式：输入频率后回车（855 / 855M / 855MHz / 855000K / 1.2G）")
    print("频段自动判定: >=950MHz 卫星天线(SAT)，否则 射频(TER)")
    print("  b 频段(ter/sat)  d 射频制式(analog/dvb-c)  m 测量开关"
          "  r 原始数据开关  *原文发命令  q 退出\n")
    band = default_band
    ok = fail = 0
    while True:
        try:
            raw = input("frequency> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出。")
            break
        if not raw:
            continue
        low = raw.lower()
        if low in ("q", "quit", "exit"):
            break
        if low in ("b", "band"):
            band = ask_band(band)
            continue
        if low in ("d", "digital"):
            analog = not analog
            print("  射频制式: %s（仅影响 TER 的 SIGNAL TYPE 与测量项）"
                  % ("ANALOG" if analog else "DVB-C"))
            continue
        if low in ("m", "measure"):
            measure = not measure
            print("  测量读取: %s" % ("开" if measure else "关"))
            continue
        if low in ("r", "raw"):
            raw_print = not raw_print
            print("  原始数据打印: %s" % ("开" if raw_print else "关"))
            continue
        if raw.startswith("*"):
            out = conn.command(raw, want_response=True)
            print("  TX %s" % conn.render_raw(out["sent"]))
            print("  RX %s" % conn.render_raw(out.get("raw", b"")))
            print("  ACK=%s 响应: %s" % (out["ack"], out["lines"] or "(无)"))
            continue
        if tune_once(inst, raw, band, measure, analog=analog, lnb_output=lnb_output,
                       offset_cmd=offset_cmd, raw_print=raw_print):
            ok += 1
        else:
            fail += 1
    print("\n本次会话: 成功 %d 次, 失败 %d 次" % (ok, fail))


def ask_band(cur):
    label = {None: "自动", "TER": "TER 射频测试", "SAT": "SAT 卫星天线测试"}
    print("  当前频段: %s" % label.get(cur, cur))
    s = input("  输入 ter / sat / 回车自动: ").strip().lower()
    if s in ("ter", "sat"):
        return s.upper()
    return cur


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(
        description="PROMAX RANGER Neo / HD RANGER Telnet 远程控制测试程序",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n"
               "  python promax_telnet_test.py --host 192.168.1.50\n"
               "  python promax_telnet_test.py --host 192.168.1.50 -f 855M --measure\n")
    ap.add_argument("--host", "-H", required=True, help="仪器 IP（RANGER Neo 的 IPCTRL 口）")
    ap.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help="Telnet 端口（默认 2222）")
    ap.add_argument("--freq", "-f", action="append", default=[],
                    help="频率，可重复；如 855M / 474 / 855000K / 1.2G")
    ap.add_argument("--band", "-b", choices=["ter", "sat", "auto"], default="auto",
                    help="强制频段：ter=射频测试 / sat=卫星天线测试，默认按频率自动")
    ap.add_argument("--analog", dest="analog", action="store_true", default=True,
                    help="射频默认模拟制式（TER 下 SIGNAL TYPE=ANALOG，默认开）")
    ap.add_argument("--dvb-c", dest="analog", action="store_false",
                    help="射频改用数字制式（TER 下 SIGNAL TYPE=DVB-C）")
    ap.add_argument("--lnb-output", default="0",
                    help="卫星 LNB OUTPUT 电压值（现场固定 0，默认 0）")
    ap.add_argument("--no-profile", dest="profile", action="store_false", default=True,
                    help="不下发 SIGNAL/LNB 配置，只调谐")
    ap.add_argument("--offset-cmd", default="OFFSET",
                    help="卫星额外读取的频偏命令名（手册称 Power Offset，"
                         "默认 OFFSET；空字符串则不读）")
    ap.add_argument("--raw-print", action="store_true",
                    help="打印每条命令收发的原始字节（控制字符转义为 <XON> 等）")
    ap.add_argument("--measure", "-m", action="store_true",
                    help="调谐后读测量值（TER 模拟读 LEVEL，数字/SAT 读 POWER/C/N/MER）")
    ap.add_argument("--raw", "-r", help="只发一条原始命令并打印响应，如 '*?VER'")
    ap.add_argument("--timeout", "-t", type=float, default=20.0,
                    help="应答超时秒（默认 20；真机实测响应较慢，不宜过小）")
    ap.add_argument("--settle", type=float, default=0.8, help="调谐后等待秒（默认 0.8）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印收发原始数据")
    args = ap.parse_args()

    band = None if args.band == "auto" else args.band.upper()

    try:
        conn, inst = do_connect(args.host, args.port, args.timeout, args.verbose)
    except PromaxError as e:
        print("连接失败: %s" % e)
        return 2

    try:
        if args.raw:
            out = conn.command(args.raw, want_response=True, timeout=args.timeout)
            print("\n命令: %s" % args.raw)
            print("ACK: %s" % out["ack"])
            print("响应: %s" % (out["lines"] or "(无)"))
        elif args.freq:
            ok = fail = 0
            for f in args.freq:
                print("=" * 60)
                if tune_once(inst, f, band, args.measure, args.settle,
                             analog=args.analog, lnb_output=args.lnb_output,
                             profile=args.profile,
                             offset_cmd=args.offset_cmd.strip(),
                             raw_print=args.raw_print):
                    ok += 1
                else:
                    fail += 1
            print("=" * 60)
            print("合计: 成功 %d, 失败 %d" % (ok, fail))
        else:
            interactive(conn, inst, band, args.measure,
                        analog=args.analog, lnb_output=args.lnb_output,
                        offset_cmd=args.offset_cmd.strip(),
                        raw_print=args.raw_print)
    except PromaxError as e:
        print("\n通讯异常: %s" % e)
        return 3
    except KeyboardInterrupt:
        print("\n已被用户中断。")
        return 130
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())