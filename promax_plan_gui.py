# -*- coding: utf-8 -*-
"""
PROMAX 频道表批量测试 GUI
=========================
选频道表（可单选/多选）→ 填仪器IP → 开始检测 → 选输出文件 → 逐表测试。

特性
----
* 频道表列表分「射频 TER」「卫星 SAT」两组，可单选、可跨组多选
* IP 输入框带记忆，ComboBox 下拉可选（历史持久化到 config.json）
* 输出文件名自动按规则生成，可改；默认目录为系统下载文件夹
    单选：<频道表名>_YYYYMMDD.<xlsx|csv>
    多选：SAT_YYYYMMDD / TER_YYYYMMDD（后缀跟随「导出格式」）
* **导出格式可在设置里选**：xlsx（带表头样式）/ csv（utf-8-sig，Excel 直开）
* **结果列按频段裁剪**（见 BAND_EXCLUDE）：
    射频 TER → 不输出 OFFSET / SR（射频无此概念，仪器也不上报）
    卫星 SAT → 不输出 LEVEL（卫星只上报 POWER）
    射频+卫星混合测 → 取并集，保留任一频段要用的列
* 多表测试时，每张表测完弹暂停窗，提示下一个频道表、确认后继续
  （换频道表要换物理接口）
* 中止按钮随时停止当前测试
* 测完提示是否打开文件

用法
----
  python promax_plan_gui.py
  python promax_plan_gui.py --source CH      # 指定频道表目录

依赖：tkinter（标准库）、openpyxl（写 xlsx，缺失时自动退化为 CSV）
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from promax_gui_config import (CONFIG_NAME, band_settings, date6, date8,
                               default_out_dir, get_download_path, load_config,
                               log_path, match_preset, preset_filename,
                               preset_pause, presets, save_config)
from promax_plan_dump import load_plans, parse_plan_xml
from promax_plan_write import (convert_result_file, default_promax_path,
                               write_promax_for_results)
from promax_telnet_test import (PromaxTelnet, Promax, PromaxError,
                                PromaxConnLost, parse_measure_all,
                                parse_tune_ch)


def _ts():
    """日志行首时戳：HH:MM:SS.mmm"""
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % (
        (t % 1) * 1000)

def _fmt_hms(sec):
    """秒 → `HH:MM:SS`（计时器与日志共用）"""
    s = max(0, int(sec))
    return "%02d:%02d:%02d" % (s // 3600, (s // 60) % 60, s % 60)


# --------------------------------------------------------------- 频道表模型
# 「已选频道表」列表的进度配色：正在测 / 已完成 / 还没测
SEL_ACTIVE_BG = "#ffd966"       # 琥珀：正在测试的这一张
SEL_DONE_BG = "#d9f2d9"         # 浅绿：已测完
SEL_IDLE_BG = "#ffffff"         # 白：排队中


class PlanItem(object):
    """一个待测频道表"""

    def __init__(self, plan, path):
        self.name = plan["name"] or plan["file"]
        self.file = plan["exact_file"]
        self.path = path
        self.band = "SAT" if plan["band"].upper().startswith("SAT") else "TER"
        self.channels = plan["channels"]
        self.lnb_loc = plan.get("lnb_loc_osc", "")
        self.lnb_band = plan.get("lnb_band", "")   # BAND_C / BAND_KUKA
        # ⚠️ 仪器里 a0x-xxx.XML 与非前缀 xxx.XML 可能是同表名的不同版本
        #（频道数不同）。GUI 列表与勾选用唯一键，避免同名冲突。
        self.key = "%s|%s" % (self.band, self.file)
        # 展示名：同名不同版本（a0x 前缀 vs 非前缀）要能区分，
        # 直接用文件名（保留尾随空格信息在 tooltip 里说明）
        self.label = "%s (%s)" % (self.name, self.file.strip())

    @property
    def signal_kind(self):
        """该表以哪种制式为主：'analog' / 'digital'"""
        if not self.channels:
            return "digital"
        n_ana = sum(1 for c in self.channels
                    if str(c["signal"]).upper().startswith("ANALOG"))
        return "analog" if n_ana * 2 >= len(self.channels) else "digital"

    def points(self):
        """[(调谐频率MHz, 制式, 频道名, 符号率kSymb/s)]

        用 tune_mhz：模拟频道的调谐频率取自频道名的图像载频
        （表里的 frequency 是频道中心频率，调过去是死点）；
        卫星频道是中频（= LNB本振 − downlink）。
        """
        out = []
        for c in self.channels:
            f = c.get("tune_mhz", c.get("freq_mhz"))
            if f is not None:
                out.append((f, c["signal"], c["name"], c.get("symb_rate")))
        return out

    def tune_fix_notes(self):
        """调谐频率经过换算的项（卫星换算中频 / 模拟取图像载频），供日志提示"""
        return [(c["name"], c.get("freq_mhz"), c.get("tune_mhz"),
                 c.get("tune_src"))
                for c in self.channels
                if c.get("tune_src") in ("name", "lnb")]


# --------------------------------------------------------------- 测试引擎
class TestEngine(object):
    """在后台线程里跑测试，通过 queue 向 UI 发消息"""

    MSG_LOG = "log"
    MSG_ROW = "row"
    MSG_PLAN_START = "plan_start"   # 轮到某张表了（UI 据此高亮"已选频道表"里那行）
    MSG_PLAN_DONE = "plan_done"
    MSG_CONFIRM = "confirm"
    MSG_FINISH = "finish"
    MSG_STOPPED = "stopped"
    # 🔴 断线相关：仪器走 WiFi 时链路会抖。断线**不中止测试**，而是挂起等重连，
    #    网络恢复后从当前频点继续（已测完的频点不重测）。
    MSG_NETLOST = "net_lost"     # 连接丢失，已暂停，等用户点「重试连接」或自动重连
    MSG_NETOK = "net_ok"         # 重连成功，继续测试

    def __init__(self, out_q, port=2222):
        self.out = out_q
        self.port = port
        self.host = ""
        self._stop = threading.Event()
        self._ack = threading.Event()
        self._ack_val = None
        self.conn = None
        # 断线恢复：UI 点「重试连接」时 set，引擎线程被唤醒去重连
        self._retry_evt = threading.Event()
        self._net_lost = False
        self.timeout_run = 20.0
        # 每个频点当前的制式 / (制式,符号率) 缓存，仅在制式变化时才重发设置
        self._cur = {"sig": None, "sat": None}
        # 重连统计（最后一次测试的累计次数，用于日志与 UI 展示）
        self.reconnects = 0
        # 当前这张表的频点总数（日志里显示「第 n/总」用），_run_points 里赋值
        self._pt_total = 0
        # 本趟的第几个频点（**日志序号**，与 row["idx"] 分开）：
        # 正常测试时两者相同；「重测失败频道」时只跑几个频点，
        # 序号是 1..n，而 row["idx"] 仍保留原始频道序号（结果表里位置不变）。
        self._pt_seq = 0
        # 失败频点（未锁定 / 设置失败）：供「重测失败频道」用
        # 每项 = (item, idx, (mhz, sig, chname, sr), 原因)
        self.failures = []
        # 重测模式：{item.key: [(idx, point), ...]}，空 = 正常跑整表
        self._subset = {}

    def _ptag(self, idx):
        """频点日志前缀：`[  3/20]`。

        总数取自当前表（_run_points 设置）。取不到总数（极端情况）就退化成 `[  3]`，
        不显示成 `[3/0]` 那种误导性的分母。
        序号优先用 `_pt_seq`（本趟第几个），重测时才不会打印出 `[  7/3]`。
        """
        t = getattr(self, "_pt_total", 0)
        n = getattr(self, "_pt_seq", 0) or idx
        return "[%3d/%d]" % (n, t) if t else "[%3d]" % n

    def stop(self):
        self._stop.set()
        self._ack.set()
        self._retry_evt.set()       # 唤醒可能在等重连的循环，让它尽快看到 _stop

    def retry_connect(self):
        """UI 线程调用：立刻尝试一次重连（不等自动重连的轮询间隔）。"""
        self._retry_evt.set()

    def emit(self, kind, payload=None):
        self.out.put((kind, payload))

    def log(self, text):
        self.emit(self.MSG_LOG, text)

    def ask(self, text):
        """向 UI 请求确认，阻塞等待用户点击（中止时返回 False）"""
        self._ack_val = None
        self._ack.clear()
        self.emit(self.MSG_CONFIRM, text)
        while not self._ack.wait(0.2):
            if self._stop.is_set():
                return False
        return bool(self._ack_val)

    def resolve(self, val):
        self._ack_val = val
        self._ack.set()

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    # ---- 断线恢复（WiFi 抖动 → 暂停 → 重连 → 从当前频点继续）----
    def _wait_reconnect(self, reason=""):
        """断线后挂起：等「重试连接」按钮或自动轮询，直到重连成功。

        返回 True = 已重连（调用方应**重做当前频点**）；False = 用户中止。

        设计要点
        --------
        · **不丢已测数据**：只负责把连接弄回来；已完成的 rows 由调用方保留，
          重连后从**当前频点**继续，不从头重测。
        · **手动 + 自动**：默认每 reconnect_interval 秒自动试一次（设置里可关）；
          用户点「重试连接」立刻插队试一次。WiFi 恢复后即使没人盯也能自愈。
        · **重连必须预检**：TCP 连上≠仪器接受命令。仪器只允许一个 telnet 会话，
          掉线时仪器侧常挂着半死会话 → "连得上但收不到 ACK"。
          所以每次重连都发 *?VER 验证，不通就继续等（不误判成"已恢复"）。
        """
        self._net_lost = True
        self.emit(self.MSG_NETLOST, reason)
        self._retry_evt.clear()          # 清掉等待期间可能被点下的旧信号

        auto = bool(self.cfg_run.get("auto_reconnect", True))
        try:
            interval = max(2.0, float(self.cfg_run.get("reconnect_interval", 10.0)))
        except (TypeError, ValueError):
            interval = 10.0
        last_auto = time.time()          # 刚断线先留点时间，不立刻刷屏
        tries = 0

        while True:
            if self._stop.is_set():
                self._net_lost = False
                return False
            manual = self._retry_evt.wait(0.5)
            if manual:
                self._retry_evt.clear()
            else:
                if not (auto and (time.time() - last_auto) >= interval):
                    continue
                last_auto = time.time()

            tries += 1
            self.log("  [重连] 第 %d 次尝试（%s）..."
                     % (tries, "手动" if manual else "自动"))
            try:
                self.conn.reconnect()    # 内部先 SO_LINGER RST 关旧连接再重连
            except PromaxConnLost as e:
                self.log("  [重连] 失败：%s" % e)
                continue
            except Exception as e:       # 兜底：重连本身出错不该把测试打崩
                self.log("  [重连] 异常：%r" % e)
                continue

            try:                          # 预检：TCP 通了还要仪器真的应答
                pv = self.conn.command("?VER", timeout=min(self.timeout_run, 10.0))
            except PromaxConnLost as e:
                self.log("  [重连] 预检失败：%s" % e)
                continue
            if pv.get("status") != "ack":
                self.log("  [重连] TCP 已连接但仪器无应答（%s）——"
                         "旧会话可能还没释放，稍后自动再试" % pv.get("status"))
                continue

            self.reconnects += 1
            self.log("  [重连] 成功（第 %d 次）：仪器应答 %s"
                     % (tries, pv["lines"][0] if pv["lines"] else "OK"))
            self._net_lost = False
            self.emit(self.MSG_NETOK, tries)
            return True

    def _connect_and_probe(self):
        """建连 + 预检（*?VER）；失败**不放弃**，挂起等重连。

        返回 True = 已连上且仪器应答；False = 用户中止。

        ⚠️ 复用 _wait_reconnect()：它内部就是"重连 + *?VER 预检"，
        所以**首连失败**与**中途断线**走同一套恢复逻辑，行为一致。
        """
        try:
            self.log("连接 %s ..." % self.host)
            self.conn.connect()
            # 预检：TCP 连上不代表仪器接受命令（单会话被占/忙/前面板界面都会
            # 导致"连得上但收不到 ACK"）。先发一条 ?VER 探活。
            pv = self.conn.command("?VER", timeout=min(self.timeout_run, 10.0))
            if pv.get("status") == "ack":
                self.log("   仪器应答：%s"
                         % (pv["lines"][0] if pv["lines"] else "OK"))
                return True
            self.log("")
            self.log("!! 仪器没有应答（%s）" % pv.get("status"))
            self.log("   TCP 已连上，但命令发不出去。常见原因：")
            self.log("   1. 仪器只允许一个 telnet 会话，上一个会话没干净断开")
            self.log("      → 等 1~2 分钟，或在仪器上断开远程控制")
            self.log("   2. 仪器正忙（正在测量/扫描/捕获）")
            self.log("   3. 前面板停在需要确认的界面 → 回到主界面")
            self.log("   4. telnet 服务被关 → Preferences → Network 重新开启")
            self.log("   可用浏览器打开 http://%s/ 看仪器是否活着" % self.host)
            self.log("")
            self.log("已暂停：仪器恢复后会自动重连继续；"
                     "也可点「重试连接」立即重试。")
            return self._wait_reconnect("仪器无应答（%s）" % pv.get("status"))
        except PromaxConnLost as e:
            self.log("连接失败: %s" % e)
            self.log("已暂停：等仪器 / 网络恢复。默认会定时自动重连，"
                     "也可点「重试连接」立即重试，或点「中止」结束。")
            return self._wait_reconnect(str(e))

    # ---- 单张表 ----
    def _tune_and_measure(self, inst, item, mhz, chname, idx,
                          settle, retry, retry_step, require_lock=True):
        """设频率并测量，**锁定失败自动重试**（本程序的核心要求）

        重试策略：每次重试把稳定等待时间 +retry_step 秒，给仪器更多锁定时间。
        返回 (row, attempts)；row 为 None 表示设置阶段就失败（非锁定问题）。

        require_lock=False（模拟制式 ANALOG）：
            模拟信号是连续载波，仪器**不会报 LOCKED=ON**，但 LEVEL / CN
            照样能测。所以只要读到有效电平就算成功，不因"未锁定"而重试。
            取不到电平才重试。

        📊 耗时统计（写进 row，供日志与结果表分析）：
            tune_s     —— 调谐命令本身耗时
            wait_s     —— 为锁定累计等待的秒数（各次尝试之和）
            total_s    —— 本频点从开始到出结果的总耗时
        """
        khz = int(round(mhz * 1000))
        wait = settle
        t_start = time.time()
        t_tune = 0.0
        t_waited = 0.0
        for attempt in range(retry + 1):
            attempts = attempt + 1
            if attempt == 0:
                # 🔴 **只在第一次调谐，重试时绝不重发 TUNE**。
                # 真机实测（RANGERNeo 4，DVB-S 频点 979MHz/中频）：
                #   调一次后连续等 2s   → LOCKED=ON  MER=18.9
                #   每 2s 重调一次连试3次 → LOCKED=OFF MER=0（永远锁不上）
                # 重发 TUNE 会让解调器**重新开始捕获**，等于不断打断锁定，
                # 越重试越糟。所以重试只延长等待、保持同频。
                _t0 = time.time()
                r = inst.set_tune(item.band, "%dK" % khz)
                t_tune = time.time() - _t0
                if not r["ack"]:
                    st = r.get("status", "nak")
                    why = {"nak": "仪器拒绝(NAK)",
                           "timeout": "仪器无响应(超时)"}.get(st, st)
                    self.log("  %s %-14s %9.3f MHz  设置失败：%s"
                             % (self._ptag(idx), chname, mhz, why))
                    if st == "timeout":
                        self.no_resp += 1
                        if self.no_resp >= 3:
                            # 🔴 归为「连接丢失」而不是普通业务错误：连得上但连续
                            # 无响应，几乎都是链路半死（WiFi 掉包 / 旧会话没释放）。
                            # 抛 PromaxConnLost → 上层挂起等重连，网络好了接着测。
                            raise PromaxConnLost(
                                "连续 %d 条命令仪器无响应（链路可能已断）。\n"
                                "已暂停测试，等网络/仪器恢复后会自动重连继续；\n"
                                "也可点「重试连接」立即重试，或点「中止」结束。\n"
                                "排查：① 仪器是否忙 ② 是否被别的 telnet 会话占用\n"
                                "③ 前面板是否停在需确认的界面 ④ 浏览器打开 "
                                "http://%s/ 看仪器是否活着"
                                % (self.no_resp, self.host or "<仪器IP>"),
                                kind="recv", host=self.host, port=self.port)
                    else:
                        self.no_resp = 0
                    return None, attempts
                self.no_resp = 0

            if wait > 0:
                time.sleep(wait)
                t_waited += wait

            _t0 = time.time()
            rr = self.conn.command("?MEASURE")
            t_meas = time.time() - _t0
            if self.cfg_run.get("log_cmd_timing", False):
                self.log("      命令 ?MEASURE  发起=%s  耗时=%.3fs"
                         % (time.strftime("%H:%M:%S", time.localtime(_t0)),
                            t_meas))
            allv = parse_measure_all(" ".join(rr["lines"])) if rr["lines"] else {}
            lk = allv.get("LOCKED", (None, ""))
            lock = str(lk[1] or lk[0] or "")
            locked = lock.upper() in ("ON", "LOCKED", "ACQUIRED", "TRUE", "1")

            if require_lock:
                ok = locked
            else:
                # 模拟制式：有电平读数即为成功（不受 LOCKED 影响）
                ok = locked or self._has_level(allv)

            if ok:
                return {"allv": allv, "lock": lock, "attempts": attempts,
                        "tune_s": t_tune, "wait_s": t_waited,
                        "total_s": time.time() - t_start}, attempts

            # ---- 未成功：重试（每次把稳定等待 +retry_step）----
            if attempt < retry:
                if require_lock:
                    why = "LOCK=%s 未锁定" % (lock or "?")
                else:
                    why = "未读到电平"
                self.log("  %s %-14s %9.3f MHz  %s，"
                         "第 %d/%d 次重试（保持同频继续等待 %.1fs → %.1fs）"
                         % (self._ptag(idx), chname, mhz, why, attempt + 1,
                            retry, wait, wait + retry_step))
                wait += retry_step
            else:
                if require_lock:
                    self.log("  %s %-14s %9.3f MHz  LOCK=%s 重试 %d 次仍未锁定，"
                             "记录为锁定失败"
                             % (self._ptag(idx), chname, mhz, lock or "?",
                                retry))
                    fail_mark = lock or "FAIL"
                else:
                    self.log("  %s %-14s %9.3f MHz  重试 %d 次仍无电平读数，"
                             "记录为测量失败"
                             % (self._ptag(idx), chname, mhz, retry))
                    fail_mark = lock or "NONE"
                return {"allv": allv, "lock": fail_mark,
                        "attempts": attempts,
                        "tune_s": t_tune, "wait_s": t_waited,
                        "total_s": time.time() - t_start}, attempts
        return None, retry + 1

    # 判定"读到了电平"用的键（模拟 LEVEL / 数字 POWER）
    LEVEL_KEYS = ("LEVEL", "POWER", "CH_POWER", "BAND_POWER")

    def _has_level(self, allv):
        """响应里是否有有效电平读数"""
        for k in self.LEVEL_KEYS:
            v = allv.get(k)
            if v and v[0] is not None:
                return True
        return False

    def run_plan(self, item, settle=0.5, retry=3, retry_step=1.0):
        """跑完一张表。表级准备 + 频点循环分别见 _prepare_plan / _run_points。

        断线不会中断本方法：**两个阶段都挂起等重连**
          · 表级准备（_prepare_plan）—— 本方法内挂起，重连后重做整表准备
          · 频点循环（_run_points）—— 内部挂起，重连后只重做当前频点
        """
        inst = Promax(self.conn)
        analog = item.signal_kind == "analog"
        if item.band == "SAT":
            srs = [p[3] for p in item.points() if p[3]]
            n_all = len(item.points())
            n_sr = len(srs)
            if not srs:
                # 🔴 表里没标符号率**不是问题**：本机仪器只靠正确频点就能自动
                #    搜出符号率/制式（实测确认，共 8 个未标频点
                #    全部一次锁定）。锁定的决定因素是等待时间，与是否设 SR 无关。
                sr_txt = "表内未标（仪器自动搜，无需设置）"
            elif n_sr < n_all:
                sr_txt = ("%s（另有 %d 个频点表内未标，仪器自动搜）"
                          % ("、".join("%gk" % s for s in sorted(set(srs))),
                             n_all - n_sr))
            else:
                sr_txt = "、".join("%gk" % s for s in sorted(set(srs)))
            hint = ("稳定等待=%.1fs  锁定重试=%d次(+%.1fs)  符号率=%s"
                    % (settle, retry, retry_step, sr_txt))
        elif analog:
            # 模拟是连续载波，仪器不报 LOCKED=ON，所以不判锁定
            hint = "稳定等待=%.1fs  （模拟制式不判锁定，读到电平即成功）" % settle
        else:
            hint = ("稳定等待=%.1fs  锁定重试=%d次(+%.1fs)"
                    % (settle, retry, retry_step))
        self.log("【%s】%d 个频道  制式=%s  %s"
                 % (item.name, len(item.points()),
                    "模拟ANALOG" if analog else "数字DVB-C/DVB-S2", hint))
        # 调谐频率经过换算的，提示一下（否则看日志对不上表里的数字）
        fixes = item.tune_fix_notes()
        if fixes:
            sat_fix = [f for f in fixes if f[3] == "lnb"]
            ana_fix = [f for f in fixes if f[3] == "name"]
            if sat_fix:
                band_lbl = {"BAND_C": "C 波段", "BAND_KUKA": "Ku 波段"}.get(
                    item.lnb_band, item.lnb_band or "")
                self.log("  [说明] 卫星频道发的是**中频**，不是表里的下行频率："
                         "中频 = |下行 − LNB本振|（%s 本振 %s）"
                         % (band_lbl, item.lnb_loc or "?"))
                for nm, dl, if_, _s in sat_fix[:4]:
                    self.log("         DL=%-9.3f MHz → 中频=%.3f MHz" % (dl, if_))
                if len(sat_fix) > 4:
                    self.log("         ...（其余 %d 个同样换算）"
                             % (len(sat_fix) - 4))
            if ana_fix:
                self.log("  [说明] %d 个模拟频道的调谐频率取自频道名"
                         "（表里的值是频道中心频率，需 +2.75MHz 才是图像载频）："
                         % len(ana_fix))
                for nm, plan_f, tune_f, _s in ana_fix:
                    self.log("         %-14s 表值=%.3f MHz → 实际调谐=%.3f MHz"
                             % (nm, plan_f, tune_f))

        # 表级准备（切频段 + 下发 LNB/profile + 校验 BAND）。
        # 断线重连后要重做同一套 → 抽成 _prepare_plan 供两处调用。
        #
        # 🔴 表级准备**也会撞上断线，必须在这里挂起重连**（踩过）。
        #    以前这一句没保护，PromaxConnLost 直接抛到 run() 里被
        #    `except PromaxError` 吃掉 → break → **整场测试提前结束**，
        #    表现就是"设备断开后恢复，测试却没有继续，而是中断了"。
        #    而"表边界"恰恰是断线高发点：用户正在换物理接口、仪器也可能
        #    把空闲的 telnet 会话踢掉（单会话机型尤其明显）。
        #    重连成功后**重做整张表的准备**（新 socket 上仪器侧配置未必还在）。
        while True:
            try:
                self._prepare_plan(inst, item, analog)
                break
            except PromaxConnLost as e:
                self.log("  [断线] 表级准备阶段连接丢失：%s" % e)
                ok, inst = self._recover(item, analog, inst, str(e))
                if not ok:
                    self.log("  [中止] 测试已停止")
                    return []
                self.log("  连接已恢复，重做本表准备：%s" % item.name)
        return self._run_points(item, analog, inst, settle, retry, retry_step)

    def _prepare_plan(self, inst, item, analog):
        """表级准备：切频段 → 下发 LNB/profile → 设 FREQ 模式 → 校验频段。

        🔴 抽成独立方法的理由：**断线重连后必须重做这一整套**。
        重连是新 socket，仪器侧的解调/输入配置未必还在（也可能被重置），
        只重发 *TUNE 是不够的。调用方在重连成功后重新调用本方法做 resync。

        只在 BAND 真正不符时抛 PromaxError（避免误测成别的频段）；
        链路类故障会抛 PromaxConnLost，由调用方挂起等重连。
        """
        # 先把仪器切到目标频段，再做后续配置和校验。
        # 本程序按频率调谐（MODE=FREQ），频段由我们指定；
        # 如果先校验再切，仪器停在另一个频段时会误判成"测错频段"而中止。
        r_band = inst.set_band(item.band)
        time.sleep(0.4)

        for line in inst.apply_profile(item.band, analog=analog,
                                       lnb_output=0):
            self.log("  " + line.strip())

        r_mode = inst.tune_mode("FREQ")
        # 切完之后再查一次：只有**仍不符**才中止
        # （防止 BAND 命令被静默忽略、实际用别的频段测出"看起来正常"的数据）。
        # 只查一次 —— 以前是每个频道都查，51 频道白多 51 条命令（约 25 秒）。
        # 注意：返回的 PLAN 与本程序无关（我们按频率调谐，实测 *TUNE PLAN= 恒 NAK，
        # 仪器里的表切不了），所以只用来校验 BAND，不写进结果。
        ch_lines = inst.tune_ch()
        b_now, _p_now, _c = parse_tune_ch(ch_lines)

        # 🔴 表级"静默无响应"也要当断线处理（踩过）。
        #    半死的 WiFi 链路不会报 socket 错误，只会一直超时；此时三条表级
        #    命令全超时、`?TUNE CH` 一个字都收不到。若不判，程序会**当成正常**
        #    往下跑，白测好几个频点才在频点循环里攒够 3 次超时触发重连。
        #    三条同时超时才判 —— 避免"仪器忙但还活着"被误判（那时 ?TUNE CH
        #    一定答得上来）。
        if (not ch_lines
                and r_band.get("status") == "timeout"
                and r_mode.get("status") == "timeout"):
            raise PromaxConnLost(
                "表级准备阶段仪器连续无响应（链路可能已断）",
                kind="recv", host=self.host, port=self.port)

        # 只在 BAND **状态变化时**才打印，每张表都打一遍是纯噪音。
        # ⚠️ 不再显示仪器回报的"当前加载表"（2026-10-16）：该字段在卫星测试时
        #    始终不准（设备反馈错误），而 *TUNE PLAN= 远程又改不了（31.17 恒 NAK）
        #    —— 印出来只会误导。测的是哪张表，看界面「当前频道表」即可。
        state = b_now or "?"
        if state != getattr(self, "_last_inst_state", None):
            self._last_inst_state = state
            self.log("  仪器状态：BAND=%s" % state)
        if b_now and b_now.strip().upper() != item.band.upper():
            raise PromaxError(
                "已下发 *TUNE BAND=%s，但仪器仍停在 BAND=%s，已中止"
                "（避免误测成别的频段）" % (item.band, b_now))

    # ---- 频点循环（含"断线挂起 → 重连 → 重做当前频点"）----
    def _run_points(self, item, analog, inst, settle, retry, retry_step):
        """逐个频点测试；**断线时挂起等重连，然后重做当前频点**。

        🔴 这是"WiFi 断网不中断测试"的核心：
          · 断线（PromaxConnLost）不 break，而是 _recover() 挂起等重连
          · 重连成功后重新下发表级配置（仪表侧配置可能丢）+ 重做当前频点
          · 已测完的频点保留在 rows 里，**不重测**
          · 用户点「中止」→ _recover 返回 False → 干净退出
        """
        self._cur = {"sig": None, "sat": None}   # 制式缓存（本表内有效）
        self.no_resp = 0        # 连续无响应计数，达到阈值就按断线处理
        # 重测模式：只跑指定的几个频点（键 = item.key）
        sub = (self._subset or {}).get(item.key)
        if sub:
            pts = list(sub)                      # [(原始序号, (mhz,sig,ch,sr))]
            self.log("  [重测] 本表只跑 %d 个未成功频道" % len(pts))
        else:
            pts = [(k + 1, p) for k, p in enumerate(item.points())]
        # 本表频点总数：日志里统一显示「第 n/总数」，一眼看得出还剩多少
        self._pt_total = len(pts)
        rows = []
        i = 0
        while i < len(pts):
            if self._stop.is_set():
                self.log("  [中止] 用户停止测试")
                break
            idx, (mhz, sig, chname, sr) = pts[i]
            i += 1
            self._pt_seq = i        # 日志序号 = 本趟第几个（重测时 ≠ 原始序号）
            try:
                row = self._one_point(item, analog, inst, mhz, sig, chname, sr,
                                      idx, settle, retry, retry_step)
            except PromaxConnLost as e:
                self.log("  [断线] %s" % e)
                ok, inst = self._recover(item, analog, inst, str(e))
                if not ok:
                    self.log("  [中止] 测试已停止")
                    break
                self.log("  从第 %d/%d 个频点继续（此前已完成 %d 个，不回退重测）"
                         % (i, len(pts), len(rows)))
                i -= 1          # 当前频点只测了一半 → 回退一格重做
                continue
            if row is not None:
                rows.append(row)
                self.emit(self.MSG_ROW, (item, row))
                # 未锁定 → 记下来，收尾时汇总成「重测失败频道」的待办。
                # ⚠️ 模拟制式（ANALOG）本来就不报 LOCKED=ON（连续载波），
                #    不能算失败，否则射频表整张都会被列进去。
                if (not analog) and str(row.get("LOCKED", "")).upper() != "ON":
                    self.failures.append(
                        (item, idx, (mhz, sig, chname, sr),
                         "未锁定（尝试 %d 次）" % row.get("attempts", 0)))
            else:
                # 设置阶段就失败了（NAK / 无应答），连行都没有 → 同样要能重测
                self.failures.append(
                    (item, idx, (mhz, sig, chname, sr), "设置失败 / 无应答"))
        return rows

    def _recover(self, item, analog, inst, reason):
        """断线恢复：挂起等重连 → 重新下发表级配置。

        返回 (是否继续, 新的 inst)。中途再断会再次挂起（WiFi 抖动可能反复），
        只有用户点「中止」才返回 (False, inst)。
        """
        while True:
            if not self._wait_reconnect(reason):
                return False, inst
            inst = Promax(self.conn)      # 重连后 self.conn 是新的 socket
            self.no_resp = 0
            self.log("  重连成功，重新下发频段 / LNB 配置...")
            try:
                self._prepare_plan(inst, item, analog)
                return True, inst
            except PromaxConnLost as e:   # 刚回来又抖掉 → 再挂起一次
                self.log("  [断线] 配置阶段又断开：%s" % e)
                reason = str(e)

    def _one_point(self, item, analog, inst, mhz, sig, chname, sr, idx,
                   settle, retry, retry_step):
        """测一个频点：按需切换制式/符号率 → 调谐 + 等锁定（含重试）→ 组装 row。

        返回 row；设置阶段彻底失败返回 None。
        网络故障抛 PromaxConnLost，由 _run_points 挂起重连后**重做本频点**。
        """
        want = "ANALOG" if str(sig).upper().startswith("ANALOG") else (
            "DVB-C" if item.band == "TER" else "DVB-S2")
        if item.band == "SAT":
            # 卫星：符号率/制式每个频点可能不同，**变了就必须重设**。
            # 不设对符号率会锁不上（实测某频道在 SR=21370k 时 LOCKED=OFF，
            # 改成表载 9200k 后 LOCKED=ON）。
            key = (str(sig).upper(), sr)
            if key != self._cur["sat"]:
                for label, r in inst.set_sat_params(sig, sr):
                    st = r.get("status", "ack" if r["ack"] else "nak")
                    mark = {"ack": "ACK", "nak": "NAK",
                            "timeout": "无响应(超时)"}.get(st, st)
                    self.log("    设置 %-20s -> %s" % (label, mark))
                self._cur["sat"] = key
                self._cur["sig"] = want        # 制式已随参数设好
                time.sleep(0.5)
        elif want != self._cur["sig"]:
            inst.signal_type(want)
            if want == "ANALOG":
                inst.signal_standard("PAL")
            self._cur["sig"] = want
            time.sleep(0.3)

        m, attempts = self._tune_and_measure(
            inst, item, mhz, chname, idx, settle, retry, retry_step,
            # 模拟制式不要求锁定（连续载波，仪器不报 LOCKED=ON），
            # 但有电平读数就算成功 —— 否则会白重试且丢掉 LEVEL/CN
            require_lock=(want != "ANALOG"))
        if m is None:
            return None
        allv, lock = m["allv"], m["lock"]

        # 测量项：真机响应里没有 LEVEL 键，模拟电平在 POWER/CH_POWER 上
        #（`*?MEASURE` 全量返回 POWER=68.26 dBuV CH_POWER=68.26 dBuV CN=...）
        # 所以"LEVEL"显示项实际取 POWER，避免取空值。
        if want == "ANALOG" and item.band == "TER":
            use = ["LEVEL", "CN"]       # LEVEL → 取 POWER 键
        elif item.band == "TER":
            use = ["POWER", "MER"]
        else:
            use = ["POWER", "CN", "MER"]
        row = {"idx": idx, "band": item.band,
               # plan 列固定写「正在测的这张表」。
               # 仪器那边 *?TUNE CH 返回的 PLAN 与本程序无关：
               # 我们是按频率调谐（MODE=FREQ），且实测 *TUNE PLAN= 恒 NAK，
               # 仪器里的表根本切不了 —— 用它会让整列都是同一张表，误导。
               "plan": item.name,
               "ch": chname, "freq_mhz": "%.3f" % mhz,
               "signal": sig, "LOCKED": lock,
               "symb_rate": (int(sr) if sr else ""),
               "attempts": attempts,
               # 📊 耗时（秒）：调谐 / 累计等待 / 总计
               "tune_s": round(m.get("tune_s", 0.0), 3),
               "wait_s": round(m.get("wait_s", 0.0), 3),
               "total_s": round(m.get("total_s", 0.0), 3)}
        # 频点耗时日志：一眼看出哪些频点接近锁定边界
        if self.cfg_run.get("log_timing", True):
            self.log("  %s %-14s %9.3f MHz  LOCK=%-4s  调谐=%.2fs "
                     "等待=%.1fs  总计=%.1fs  (尝试 %d 次)"
                     % (self._ptag(idx), chname, mhz, lock, row["tune_s"],
                        row["wait_s"], row["total_s"], attempts))
        # 显示键名 → 真机响应里的实际键名。
        # 真机模拟制式(ANALOG)直接返回 LEVEL；数字制式没有 LEVEL，
        # 模拟电平落在 POWER / CH_POWER 上。
        # 所以**先按原名取，取不到再按映射名取**，两边都兼容。
        KEYMAP = {"LEVEL": "POWER"}
        for k in use:
            v, u = allv.get(k, (None, ""))
            if v is None:
                v, u = allv.get(KEYMAP.get(k, k), (None, ""))
            row[k] = "" if v is None else v
            row[k + "_unit"] = u or ""
        if item.band == "SAT":
            ov, ou = allv.get("OFFSET", (None, ""))
            row["OFFSET"] = "" if ov is None else ov
            row["OFFSET_unit"] = ou or "Hz"
        return row

    def run(self, host, items, cfg=None, timeout=20.0, pause_between=True,
            subset_map=None):
        self.host = host
        self.cfg_run = cfg or {}
        # 重测模式：subset_map = {item.key: [(原始序号, point), ...]}
        # 给了就只跑这些频点（「重测失败频道」用），否则正常跑整表
        self._subset = subset_map or {}
        self.failures = []
        self._last_inst_state = None    # 仪器状态只在变化时打印
        all_rows = {}
        try:
            # fast=True：每频道要发 3 条命令，46 频道就是 138 条。
            # 心跳 bug 已修（以 CR/LF 帧结束为判据），fast 模式安全，
            # 否则每条命令固定等 1.5~2 秒，一张表要 4 分钟。
            self.conn = PromaxTelnet(host, self.port, timeout=timeout,
                                     fast=True)
            self.timeout_run = timeout
            self.reconnects = 0
            # 🔴 首连/预检失败也**不直接结束**，而是挂起等重连：
            #    仪器走 WiFi 时"点开始时链路还没上来"很常见，
            #    挂起后能自动/手动重试，不用整个重来。
            if not self._connect_and_probe():
                self.emit(self.MSG_STOPPED)
                return

            for n, item in enumerate(items, 1):
                if self._stop.is_set():
                    break
                # 轮到这张表了 —— 放在「换线暂停」提示**之前**发：
                # 暂停时右侧「已选频道表」就已经亮着这张，用户一眼知道该接哪个接口。
                self.emit(self.MSG_PLAN_START, item)
                if n > 1:
                    self.log("")
                    if pause_between:
                        self.log("=" * 60)
                        self.log("准备测试第 %d/%d 张表：%s"
                                 % (n, len(items), item.name))
                        self.log("【请更换物理接口到对应信号源】")
                        self.log("=" * 60)
                        if not self.ask(
                                "下一张频道表：%s\n\n请把测试线接到对应接口，然后点"
                                "「继续」开始测试。\n（点「中止」则停止整个测试）"
                                % item.name):
                            break
                        if self._stop.is_set():
                            break
                    else:
                        self.log("--- 继续第 %d/%d 张表：%s（已配置表间不暂停）---"
                                 % (n, len(items), item.name))
                self.log("")
                _n_pt = len(self._subset.get(item.key, ())) or len(item.points())
                self.log(">>> [%d/%d] %s (%s, %s%d 个频道)"
                         % (n, len(items), item.name, item.band,
                            "重测 " if self._subset else "", _n_pt))
                try:
                    # 每张表按自己的频段取稳定等待/重试参数（卫星锁定慢，要分开设）
                    st, rt, stp = band_settings(self.cfg_run, item.band)
                    rows = self.run_plan(item, settle=st, retry=rt,
                                         retry_step=stp)
                except PromaxConnLost as e:
                    # 🔴 兜底网：run_plan 的**两个阶段**都已经自带挂起重连，
                    #    正常到不了这里。留这一层是因为 PromaxConnLost 是
                    #    PromaxError 的子类，一旦将来新增了没保护的仪器调用，
                    #    就会掉进下面的 `except PromaxError` 直接 break ——
                    #    那正是"设备断开后恢复、测试却中断了"这个坑。
                    #    这里改成：先挂起把连接找回来，再重跑这张表。
                    self.log("  [断线] %s" % e)
                    ok, _ = self._recover(item, item.signal_kind == "analog",
                                          Promax(self.conn), str(e))
                    if not ok:
                        self.log("  [中止] 测试已停止")
                        break
                    self.log("  连接已恢复，重跑本表：%s" % item.name)
                    try:
                        rows = self.run_plan(item, settle=st, retry=rt,
                                             retry_step=stp)
                    except PromaxError as e2:
                        self.log("  [通讯异常] %s" % e2)
                        break
                except PromaxError as e:
                    self.log("  [通讯异常] %s" % e)
                    break
                all_rows[item.name] = rows
                self.emit(self.MSG_PLAN_DONE, (item, rows))
        except Exception:
            self.log("内部错误:\n" + traceback.format_exc())
        finally:
            # ⚠️ 必须放在 finally：任何路径（正常结束/异常/中止）都要断开连接。
            # 仪器只允许一个 telnet 会话，残留连接会让后续连接全部无响应。
            # close() 内部用 SO_LINGER(1,0) 发 RST，能立即释放会话
            # （只做普通 close 会停在 FIN_WAIT_2，仪器迟迟不释放）。
            self.close()
        if self._stop.is_set():
            self.emit(self.MSG_STOPPED)
        else:
            self.emit(self.MSG_FINISH, all_rows)


# --------------------------------------------------------------- 写 xlsx
# --------------------------------------------------------------- 列定义
# (显示表头, row 里的键, 默认单位)  —— 单位直接写进表头，不再单独一列
COLUMNS = [
    ("序号", "idx", ""),
    ("频段", "band", ""),
    ("频道表", "plan", ""),
    ("频道", "ch", ""),
    ("频率(MHz)", "freq_mhz", ""),
    ("制式", "signal", ""),
    ("LOCK", "LOCKED", ""),
    ("LEVEL(dBuV)", "LEVEL", "dBuV"),
    ("CN(dB)", "CN", "dB"),
    ("POWER(dBuV)", "POWER", "dBuV"),
    ("MER(dB)", "MER", "dB"),
    ("OFFSET(kHz)", "OFFSET", "kHz"),
    ("SR(kSymb/s)", "symb_rate", ""),   # 卫星解调参数（TER 为空）
    ("尝试", "attempts", ""),      # 测量尝试次数，>1 说明发生过锁定重试
    ("调谐(s)", "tune_s", ""),      # 调谐命令耗时
    ("等待(s)", "wait_s", ""),      # 为锁定累计等待的秒数（重试之和）
    ("总计(s)", "total_s", ""),     # 本频点总耗时
]

# ---- 按频段裁剪列（2026-10-14 用户要求）----
#   射频 TER：有线电视没有「频偏」也没有「符号率」概念，且仪器不上报 → 去掉
#   卫星 SAT：仪器只上报 POWER，LEVEL 恒为空 → 去掉
BAND_EXCLUDE = {
    "TER": ("OFFSET", "symb_rate"),
    "SAT": ("LEVEL",),
}


def columns_for(bands):
    """按结果里实际出现的频段返回列定义（保持 COLUMNS 原有顺序）。

    只丢掉「对**所有**出现频段都无意义」的列：
      射频 TER → 丢 OFFSET / SR；卫星 SAT → 丢 LEVEL。
    · 单频段文件（纯射频 / 纯卫星）→ 各自裁剪
    · 混合文件（射频+卫星同一次测）→ 取并集，保留任一频段要用的列
    """
    bs = {str(b or "").upper() for b in (bands or [])} or set(BAND_EXCLUDE)
    drop = None
    for b in bs:
        ex = set(BAND_EXCLUDE.get(b, ()))
        drop = ex if drop is None else (drop & ex)
    drop = drop or set()
    return [c for c in COLUMNS if c[1] not in drop]


def _result_bands(results):
    """从 {表名: [row,...]} 里收集出现过的频段"""
    out = set()
    for rows in (results or {}).values():
        for r in rows or []:
            b = str(r.get("band") or "").upper()
            if b:
                out.add(b)
    return out


def fmt_suffix(cfg):
    """配置的导出格式 → 文件后缀（.xlsx / .csv）"""
    return ".csv" if str((cfg or {}).get("export_format", "")).lower() == "csv" else ".xlsx"


def _fix_suffix(path, cfg):
    """给输出路径补上配置的后缀（已带正确后缀则不动）"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".csv"):
        return path
    return path + fmt_suffix(cfg)



def _offset_khz(row):
    """OFFSET 换算成 kHz（原值单位 Hz，可能是负数）"""
    v = row.get("OFFSET")
    if v is None or v == "":
        return ""
    try:
        return round(float(v) / 1000.0, 3)
    except (TypeError, ValueError):
        return ""


def _cell(row, key):
    if key == "OFFSET":
        return _offset_khz(row)
    v = row.get(key, "")
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 2)
    return v


def write_xlsx(path, results, sheet_title=None):
    """results: {表名: [row,...]}

    - 单位写在表头（MER(dB)），不再单独单位列
    - **一次测试的所有频道表记录在同一个 sheet**（不分页），
      用「频道表」列区分是哪张表 —— 与按钮/预设选中的口径一致
    - OFFSET 由 Hz 换算为 kHz（保留负号）
    - 列按频段裁剪：射频去 OFFSET/SR，卫星去 LEVEL（见 columns_for）
    - sheet_title：单 sheet 的名字（预设名或首个表名）
    """
    cols = columns_for(_result_bands(results))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return write_csv(path, results)

    wb = Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    head_font = Font(bold=True)
    center = Alignment(horizontal="center")
    widths = [6, 6, 20, 16, 11, 12, 7, 13, 10, 13, 10, 12, 13, 6]

    def header(ws):
        ws.append([c[0] for c in cols])
        for c in ws[1]:
            c.fill = head_fill
            c.font = head_font
            c.alignment = center
        for i, w in enumerate(widths, 1):
            if i > len(cols):
                break
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
        ws.freeze_panes = "A2"

    if not results:
        ws = wb.create_sheet("结果")
        ws.append(["无数据"])
        wb.save(path)
        return path

    # 全部记录在一个 sheet（需求：按钮选中的测试内容保存在一页里）
    title = sheet_title or ("测试结果" if len(results) > 1
                            else next(iter(results)))
    title = re.sub(r"[\\/:*?\[\]]", "_", str(title))[:31] or "测试结果"
    ws = wb.create_sheet(title=title)
    header(ws)
    for name, rows in results.items():
        for r in rows:
            ws.append([_cell(r, c[1]) for c in cols])
    wb.save(path)
    return path


def write_csv(path, results, sheet_title=None):
    """写 CSV（utf-8-sig 带 BOM，Excel 双击直接打开不乱码）

    列与 write_xlsx 一致（按频段裁剪）。openpyxl 缺失时也走这条路径。
    """
    import csv
    cols = columns_for(_result_bands(results))
    base, ext = os.path.splitext(path)
    if ext.lower() != ".csv":
        path = base + ".csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c[0] for c in cols])
        for name, rows in (results or {}).items():
            for r in rows or []:
                w.writerow([_cell(r, c[1]) for c in cols])
    return path


def write_result(path, results, sheet_title=None):
    """按路径后缀分发：.csv → CSV，其它 → xlsx。返回最终写入路径。"""
    if os.path.splitext(path)[1].lower() == ".csv":
        return write_csv(path, results, sheet_title=sheet_title)
    return write_xlsx(path, results, sheet_title=sheet_title)


# --------------------------------------------------------------- GUI
class App(object):
    def __init__(self, root, source):
        self.root = root
        self.cfg = load_config()
        self.source = source       # 频道表目录（转换 Promax 时要回查下行）
        self.items = []            # 全部 PlanItem
        self.check_vars = {}       # name -> IntVar
        self.active_preset = None  # 当前应用的预设（决定表间是否暂停）
        self.log_fp = None         # 单一日志文件句柄（追加）
        self.log_path = None       # 单一日志文件路径
        self.sep_log_fp = None     # 独立日志文件句柄（本次，覆盖）
        self.sep_log_path = None   # 独立日志文件路径
        self.engine = None
        self.thread = None
        self.rows = {}             # 表名 -> [row]
        self.out_path = None
        # 「已选频道表」列表的进度态：key 列表（按 Listbox 行序）+ 已完成集合
        #   + 当前正在测的那张。三者一起决定每行的底色/前缀，见 _paint_sel()。
        self.sel_box_keys = []     # Listbox 第 i 行 → 频道表 key
        self.sel_done = set()      # 已测完的 key
        self.sel_active = None     # 正在测试的 key（None = 没在测）
        # 进度条计数（两个条都用"计数"驱动，见 _update_progress）
        #   当前频道表：本表已完成频点 / 本表频点总数
        #   全部      ：**按频道数加权** —— 所有选中表的频点总数，不是表张数
        self.pg_cur_done = 0
        self.pg_cur_total = 0
        self.pg_all_done = 0
        self.pg_all_total = 0

        # 计时器：_t_acc=已累计秒；_t_t0=本段起点（monotonic），None=已停表；
        # _t_on=本轮是否进行中（决定要不要继续定时刷新）
        self._t_acc = 0.0
        self._t_t0 = None
        self._t_on = False
        self._t_paused = False
        self._tick_ac = None

        # 失败频道（未锁定 / 设置失败）：收尾时从引擎汇总，供「重测失败频道」
        self.pending_retest = []
        self.retesting = False          # 当前这轮是不是"只重测失败频道"
        self._retest_subset = {}        # {item.key: [(原始序号, point), ...]}

        root.title("PROMAX 频道表批量测试")
        w = self.cfg.get("window", {})
        root.geometry("%dx%d" % (w.get("width", 1240), w.get("height", 780)))
        root.minsize(1000, 640)
        # 关窗口必须先停测试并断开连接：仪器只允许一个 telnet 会话，
        # 直接叉掉窗口会把后台线程连同 socket 一起丢弃 → 仪器侧会话残留
        # → 后续连接"连得上但收不到任何回复"。详见 _on_close。
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build(source)
        self._scan(source)

    # ---------------- 界面 ----------------
    def _build(self, source):
        # ---- 顶栏：IP / 端口 / 操作 ----
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="仪器 IP:").pack(side="left")
        self.ip_var = tk.StringVar(value=self.cfg.get("last_ip", ""))
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", 2222)))
        self.ip_box = ttk.Combobox(top, textvariable=self.ip_var, width=22,
                                   values=self.cfg.get("ip_history", []))
        self.ip_box.pack(side="left", padx=4)
        self.ip_box.bind("<Return>", lambda e: self._save_ip())
        ttk.Label(top, text="端口:").pack(side="left", padx=(10, 2))
        ttk.Spinbox(top, from_=1, to=65535, width=6,
                    textvariable=self.port_var).pack(side="left")

        ttk.Button(top, text="连接测试", command=self.start).pack(
            side="left", padx=6)
        self.stop_btn = ttk.Button(top, text="中止", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left")
        # 🔴 断线重试：仪器走 WiFi 时链路会抖。断线后测试**暂停**而不是中断，
        #    点这里立刻重连，并从**当前频点**继续（已测完的不会重测）。
        #    默认还会定时自动重连，所以不点它也能自愈。
        self.retry_btn = ttk.Button(top, text="重试连接", command=self.retry_conn,
                                    state="disabled")
        self.retry_btn.pack(side="left", padx=(4, 0))
        # 🔴 「重测失败频道」：一轮跑完后只补测未锁定 / 设置失败的那几条，
        #    结果按频道名覆盖回**同一个结果文件**。
        #    这是本程序的核心痛点（卫星批量测试个别频道锁不上 → 数据不全），
        #    以前只能整场重跑，白测几十个正常频道。
        self.retest_btn = ttk.Button(top, text="重测失败频道",
                                     command=self.retest_failed,
                                     state="disabled")
        self.retest_btn.pack(side="left", padx=(4, 0))
        ttk.Button(top, text="设置", command=self._open_settings
                   ).pack(side="left", padx=6)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.status).pack(side="left", padx=10)
        # ---- 计时器（顶栏最右，紧接「设置」之后）----
        # 只统计**真正在测**的时间：断线挂起、换线暂停时停表（见 _timer_pause）。
        # 用等宽字体 + 固定宽度，数字跳动时不会把左边的控件推来推去。
        self.timer_var = tk.StringVar(value="用时 —")
        ttk.Label(top, textvariable=self.timer_var, width=24, anchor="e",
                  font=("Consolas", 10)).pack(side="right")

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=4)

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)

        # ---- 中间区域底部：两个进度条 ----
        # ⚠️ pack 顺序：`side="bottom"` 必须在 left/right **之前** pack ——
        #    它们都是 fill/expand，先占位的会把后 pack 的挤出可视区。
        # 两条都用"计数"显示（ttk.Progressbar 本身不显示数字，自己配标签）：
        #   当前频道表 = 本表已完成频点 / 本表频点总数
        #   全部       = 全部已完成频点 / 全部选中表的频点总数（**按频道数加权**）
        self._build_progress(body)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="频道表（可单选 / Ctrl 或 Shift 多选）"
                 ).pack(anchor="w")
        cols = ("check", "name", "band", "count", "signal")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=16)
        for c, t, w in (("check", "选", 34), ("name", "频道表", 190),
                        ("band", "频段", 46), ("count", "频道数", 60),
                        ("signal", "制式", 96)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c != "name" else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._toggle_sel)

        ttk.Button(left, text="全选 / 全不选", command=self._toggle_all
                   ).pack(anchor="w", pady=4)

        # ---- 预设按钮区：由配置文件生成，不写死在代码里 ----
        ttk.Label(left, text="快捷测试组（在配置文件中定义）"
                 ).pack(anchor="w", pady=(6, 2))
        self.preset_box = ttk.Frame(left)
        self.preset_box.pack(anchor="w", fill="x")
        self._build_presets()

        self.sel_lbl = tk.StringVar(value="已选 0 个")
        ttk.Label(left, textvariable=self.sel_lbl).pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body, width=330)
        right.pack(side="right", fill="y", padx=8)
        right.pack_propagate(False)

        ttk.Label(right, text="输出文件").pack(anchor="w")
        ttk.Label(right, text="默认目录：系统下载文件夹（注册表读取）",
                  foreground="#555").pack(anchor="w", pady=(0, 6))

        # 目录选择 + 历史下拉
        dirrow = ttk.Frame(right)
        dirrow.pack(fill="x")
        self.outdir_var = tk.StringVar(value=default_out_dir(self.cfg))
        self.outdir_box = ttk.Combobox(dirrow, textvariable=self.outdir_var,
                                       width=30,
                                       values=self.cfg.get("out_dir_history", []))
        self.outdir_box.pack(side="left", fill="x", expand=True)
        ttk.Button(dirrow, text="浏览...", command=self._pick_outdir
                   ).pack(side="left", padx=4)

        self.out_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.out_var).pack(fill="x", pady=(4, 0))
        ttk.Label(right, text="文件名可用 {date8}/{date6} 占位符",
                  foreground="#777").pack(anchor="w")
        # 结果 → Promax 仪器格式（给只认仪器导出 XML 的程序用）
        ttk.Button(right, text="转换成Promax格式文件",
                   command=self.convert_promax).pack(anchor="w", pady=(6, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)

        ttk.Label(right, text="已选频道表").pack(anchor="w")
        # selectmode="none"：这是个**展示**列表，不需要它自己的选中态 ——
        # Tk Listbox 的选中行会用 selectbackground 盖掉 itemconfig 设的底色，
        # 关掉选中态，"正在测试 / 已完成"的行底色才能稳定显示。
        self.sel_box = tk.Listbox(right, height=10, selectmode="none",
                                  activestyle="none")
        self.sel_box.pack(fill="both", expand=True, pady=4)
        ttk.Label(right, text="▶ 正在测试    ✓ 已完成",
                  foreground="#777").pack(anchor="w")

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=4)
        bot = ttk.Frame(self.root, padding=8)
        bot.pack(fill="both", expand=True)

        ttk.Label(bot, text="测试输出").pack(anchor="w")
        self.log = tk.Text(bot, height=10, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(bot, orient="vertical", command=self.log.yview)
        lsb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=lsb.set)

    # ---------------- 进度条 ----------------
    def _build_progress(self, parent):
        """中间区域底部的两条进度条（当前频道表 / 全部）。

        不用 ttk.Progressbar 的数字（它根本不显示数字），
        比例交给 bar，数字交给右侧标签 `3/20 (15%)`。
        """
        box = ttk.Frame(parent)
        box.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=(0, 6))

        rows = (("当前频道表", "pb_cur", "pb_cur_lbl"),
                ("全部", "pb_all", "pb_all_lbl"))
        for title, bar_attr, lbl_attr in rows:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=title, width=10).pack(side="left")
            bar = ttk.Progressbar(row, mode="determinate", maximum=100.0)
            bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
            var = tk.StringVar(value="—")
            ttk.Label(row, textvariable=var, width=18, anchor="e"
                      ).pack(side="left")
            setattr(self, bar_attr, bar)
            setattr(self, lbl_attr, var)

    def _update_progress(self):
        """按当前计数刷新两条进度条。总数 0 时显示 `—`（而不是 0/0）。"""
        for bar, var, done, total in (
                (self.pb_cur, self.pb_cur_lbl, self.pg_cur_done,
                 self.pg_cur_total),
                (self.pb_all, self.pb_all_lbl, self.pg_all_done,
                 self.pg_all_total)):
            if total <= 0:
                bar.configure(value=0.0)
                var.set("—")
                continue
            done = max(0, min(done, total))     # 别因补齐逻辑溢出
            bar.configure(value=100.0 * done / total)
            var.set("%d/%d (%.0f%%)" % (done, total, 100.0 * done / total))

    def _pg_reset(self, items):
        """新一轮测试开始：两个条都归零，'全部'总数=选中表的**频点总数**。

        ⚠️ 「全部」必须按**频道数**加权，不能按表的张数 —— 一张 50 频点的表和
        一张 2 频点的表各占 50% 会严重失真（用户明确要求按频道数量算）。
        """
        self.pg_cur_done = 0
        self.pg_cur_total = 0
        self.pg_all_done = 0
        self.pg_all_total = sum(len(it.points()) for it in items)
        self._update_progress()

    def _pg_reset_subset(self, by_item):
        """重测模式：两条进度条按**要重测的频道数**归零。"""
        self.pg_cur_done = 0
        self.pg_cur_total = 0
        self.pg_all_done = 0
        self.pg_all_total = sum(len(v) for v in by_item.values())
        self._update_progress()

    # ---------------- 计时器 ----------------
    # 需求：顶栏显示本轮测试用时，**暂停时同时停表**。
    # 「暂停」有三种：断线挂起（MSG_NETLOST→NETOK）、换线暂停（MSG_CONFIRM，
    # 等用户点「继续」）、以及测试彻底结束。三者都走 _timer_pause，
    # 恢复走 _timer_resume，结束走 _timer_stop（保留最终用时）。
    def _timer_set(self):
        """把当前用时刷到顶栏。

        ⚠️ 「（已暂停）」标记必须由 `_t_paused` 决定，不能由调用方传参：
        暂停期间 200ms 的 `_timer_tick` 还在跑，它调 `_timer_set()` 时若不带
        标记，就会把刚打上的「已暂停」冲掉（表现时有时无，测试抓到过）。
        """
        e = self._t_acc
        if self._t_t0 is not None:
            e += time.monotonic() - self._t_t0
        if e <= 0 and not self._t_on:
            self.timer_var.set("用时 —")     # 还没跑过
            return
        txt = "用时 " + _fmt_hms(e)
        if self._t_on and self._t_paused:
            txt += "（已暂停）"
        self.timer_var.set(txt)

    def _timer_tick(self):
        self._tick_ac = None
        if not self._t_on:
            return
        self._timer_set()
        self._tick_ac = self.root.after(200, self._timer_tick)

    def _timer_cancel_tick(self):
        if self._tick_ac is not None:
            try:
                self.root.after_cancel(self._tick_ac)
            except Exception:
                pass
            self._tick_ac = None

    def _timer_start(self):
        """新一轮测试开始：归零、起表。"""
        self._timer_cancel_tick()
        self._t_acc = 0.0
        self._t_t0 = time.monotonic()
        self._t_on = True
        self._t_paused = False
        self._timer_set()
        self._timer_tick()

    def _timer_pause(self):
        """暂停计时（可重复调用，第二次起无副作用）。"""
        if self._t_t0 is not None:
            self._t_acc += time.monotonic() - self._t_t0
            self._t_t0 = None
        if self._t_on:
            self._t_paused = True
        self._timer_set()

    def _timer_resume(self):
        """继续计时（已在跑就不动）。"""
        if not self._t_on:
            return
        if self._t_t0 is None:
            self._t_t0 = time.monotonic()
        self._t_paused = False
        self._timer_set()

    def _timer_stop(self):
        """测试结束：停表并保留最终用时，返回总秒数。"""
        self._timer_pause()          # 先收掉最后一段
        self._t_on = False
        self._t_paused = False       # 定格时不带「已暂停」
        self._timer_cancel_tick()
        self._timer_set()
        return self._t_acc

    # ---------------- 频道表加载 ----------------
    def _scan(self, source):
        try:
            ter, sat = load_plans(source)
        except Exception as e:
            messagebox.showerror("扫描失败", str(e))
            return
        groups = (("TER 射频", ter), ("SAT 卫星", sat))
        self.items = []
        for gname, plans in groups:
            for p in plans:
                path = None
                if os.path.isdir(source):
                    for root, _, files in os.walk(source):
                        if p["file"] in files:
                            path = os.path.join(root, p["file"])
                            break
                self.items.append(PlanItem(p, path))
        self._refresh_tree()
        self.status.set("已加载 %d 个频道表（TER %d / SAT %d）"
                        % (len(ter) + len(sat), len(ter), len(sat)))

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.check_vars = {}
        for it in self.items:
            v = tk.IntVar(value=0)
            self.check_vars[it.key] = v
            kind = "模拟" if it.signal_kind == "analog" else "数字"
            self.tree.insert("", "end", iid=it.key,
                             values=("☐", it.label, it.band,
                                     len(it.channels), kind))
        self._sync_sel()

    def _on_click(self, ev):
        """点表格行切换勾选"""
        row = self.tree.identify_row(ev.y)
        if row and row in self.check_vars:
            self._toggle(row)

    def _toggle(self, key):
        if key not in self.check_vars:
            return
        self.check_vars[key].set(1 - self.check_vars[key].get())
        self.tree.set(key, "check",
                      "☑" if self.check_vars[key].get() else "☐")
        # 手动改选择后，预设的暂停设置不再适用，回退到全局默认
        self.active_preset = None
        self._sync_sel()

    def _toggle_sel(self, ev=None):
        for key in self.tree.selection():
            self._toggle(key)

    def _toggle_all(self):
        on = any(v.get() == 0 for v in self.check_vars.values())
        for key in self.check_vars:
            self.check_vars[key].set(1 if on else 0)
            self.tree.set(key, "check", "☑" if on else "☐")
        self.active_preset = None
        self._sync_sel()

    def _by_key(self, key):
        for it in self.items:
            if it.key == key:
                return it
        return None

    def _sync_sel(self):
        keys = [k for k, v in self.check_vars.items() if v.get()]
        self.sel_box_keys = keys
        self.sel_box.delete(0, "end")
        for _ in keys:
            self.sel_box.insert("end", "")      # 文字由 _paint_sel 统一填
        # ⚠️ sel_done **不清**：某张表已经测完了，用户把它取消勾选再勾回来，
        #    它依然是"测过"的 —— 清掉会让人以为白跑一趟。
        #    （新一轮测试开始时由 start() 统一重置。）
        if self.sel_active not in keys:
            self.sel_active = None
        self._paint_sel()
        self.sel_lbl.set("已选 %d 个" % len(keys))
        self._auto_name(keys)

    def _paint_sel(self):
        """重绘「已选频道表」：标出正在测试的那张，已完成的行淡化。

        配色：琥珀 = 正在测 / 浅绿 = 已完成 / 白 = 排队中。
        另加 ▶ ✓ 前缀 —— 颜色之外再给一层区分，色弱用户也看得清。
        """
        for i, k in enumerate(self.sel_box_keys):
            it = self._by_key(k)
            text = it.label if it else k
            if k == self.sel_active:
                text, bg = "▶ " + text, SEL_ACTIVE_BG
            elif k in self.sel_done:
                text, bg = "✓ " + text, SEL_DONE_BG
            else:
                bg = SEL_IDLE_BG
            # Listbox 没有"改某行文字"的方法，只能按行号删掉重插
            self.sel_box.delete(i)
            self.sel_box.insert(i, text)
            self.sel_box.itemconfig(i, background=bg)
        # 表多到列表装不下时，把正在测的那行滚进可视区
        if self.sel_active in self.sel_box_keys:
            self.sel_box.see(self.sel_box_keys.index(self.sel_active))

    def _auto_name(self, keys):
        """手动勾选时的默认文件名：单选用表名，多选用 SAT/TER+日期"""
        if not keys:
            self.out_var.set("")
            return
        sel = [self._by_key(k) for k in keys]
        sel = [i for i in sel if i]
        d = date8()
        if len(sel) == 1:
            name = sel[0].name + "_" + d
        else:
            bands = {i.band for i in sel}
            band = "SAT" if "SAT" in bands and "TER" not in bands else (
                "TER" if bands == {"TER"} else "MIX")
            name = "%s_%s" % (band, d)
        self.out_var.set(os.path.join(self.outdir_var.get(), name + fmt_suffix(self.cfg)))

    # ---------------- 预设按钮（配置驱动） ----------------
    def _build_presets(self):
        """按 json 配置里的 presets 生成按钮，不写死在代码里"""
        for w in self.preset_box.winfo_children():
            w.destroy()
        ps = presets(self.cfg)
        if not ps:
            ttk.Label(self.preset_box, text="（配置文件中没有预设组）",
                      foreground="#888").pack(anchor="w")
            return
        # 每行放 4 个，自动换行
        row = None
        for i, p in enumerate(ps):
            if i % 4 == 0:
                row = ttk.Frame(self.preset_box)
                row.pack(anchor="w", pady=2)
            ttk.Button(row, text=p.get("name", "未命名"),
                       width=13,
                       command=lambda pp=p: self._apply_preset(pp)).pack(
                           side="left", padx=3)

    def _apply_preset(self, preset):
        """点击预设：选中对应频道表 + 按规则生成文件名 + 记住暂停设置"""
        files = preset.get("files", [])
        if not files:
            messagebox.showwarning("预设为空", "该预设没有配置 files")
            return
        hit, miss = match_preset(self.items, files)
        if not hit:
            messagebox.showwarning(
                "没有匹配",
                "预设「%s」的 files 在当前频道表里一个都没匹配上：\n%s"
                % (preset.get("name"), "、".join(files)))
            return
        # 部分未匹配：只在日志里提示，不弹窗打断（配置文件可配 strict_miss）
        if miss and self.cfg.get("strict_miss"):
            messagebox.showwarning(
                "部分未匹配",
                "以下前缀在当前频道表里找不到对应文件：\n%s"
                % "\n".join(miss))
        elif miss:
            self._log("[提示] 预设「%s」有 %d 个前缀未匹配：%s"
                      % (preset.get("name"), len(miss), "、".join(miss)))
        # 只勾选命中的，其余取消
        want = {it.key for it in hit}
        for key in self.check_vars:
            self.check_vars[key].set(1 if key in want else 0)
            self.tree.set(key, "check",
                          "☑" if key in want else "☐")
        self._sync_sel()
        # 按预设规则生成文件名
        name = preset_filename(preset)
        self.out_var.set(os.path.join(self.outdir_var.get(),
                                      name + fmt_suffix(self.cfg)))
        # 记住当前预设，start() 要靠它决定表间是否暂停
        self.active_preset = preset
        pz = "暂停" if preset_pause(preset, self.cfg) else "不暂停"
        self.status.set("已应用预设「%s」：%d 个频道表（表间%s）"
                        % (preset.get("name"), len(hit), pz))

    def _pick_outdir(self):
        d = filedialog.askdirectory(title="选择输出文件夹",
                                    initialdir=self.outdir_var.get()
                                    or default_out_dir(self.cfg))
        if not d:
            return
        self.outdir_var.set(d)
        self._remember_outdir(d)
        self._rename_out()

    def _remember_outdir(self, d):
        hist = [h for h in self.cfg.get("out_dir_history", []) if h != d]
        hist.insert(0, d)
        self.cfg["out_dir_history"] = hist[:15]
        self.cfg["out_dir"] = d
        save_config(self.cfg)
        self.outdir_box.configure(values=hist)

    def _rename_out(self):
        """目录变了就把当前文件名搬到新目录"""
        cur = self.out_var.get().strip()
        if not cur:
            return
        self.out_var.set(os.path.join(self.outdir_var.get(),
                                      os.path.basename(cur)))

    def _pick_out(self):
        """选完整保存路径（含文件名）"""
        p = filedialog.asksaveasfilename(
            title="保存测试结果",
            defaultextension=fmt_suffix(self.cfg),
            initialdir=self.outdir_var.get() or default_out_dir(self.cfg),
            initialfile=os.path.basename(self.out_var.get()) or "",
            filetypes=[("Excel 表格", "*.xlsx"), ("CSV 文本", "*.csv"),
                       ("所有文件", "*.*")])
        if p:
            self.out_var.set(p)
            d = os.path.dirname(p)
            if d:
                self.outdir_var.set(d)
                self._remember_outdir(d)

    def _restore_out(self):
        """没有选中任何表时给个默认文件名"""
        if not self.out_var.get():
            keys = [k for k, v in self.check_vars.items() if v.get()]
            self._auto_name(keys)

    def _save_ip(self):
        """记住 IP / 端口"""
        ip = self.ip_var.get().strip()
        if not ip:
            return
        hist = [h for h in self.cfg.get("ip_history", []) if h != ip]
        hist.insert(0, ip)
        self.cfg["ip_history"] = hist[:20]
        self.cfg["last_ip"] = ip
        try:
            self.cfg["port"] = int(self.port_var.get())
        except (TypeError, ValueError):
            self.cfg["port"] = 2222
        save_config(self.cfg)
        self.ip_box.configure(values=hist)

    def _open_settings(self):
        """设置窗口：改配置 json"""
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.transient(self.root)
        win.grab_set()
        # 高度要给足：「基本」页有 IP/端口/窗口/暂停/导出/列裁剪/重连 +
        # 分频段参数两栏，内容比早期版本多，太矮会把下面的输入框挤出可视区
        # （ttk.Notebook 不会自动滚动，超出就真的看不到）。
        win.geometry("940x800")
        win.minsize(820, 600)

        # ⚠️ 从磁盘重读：用户可能手动改过 json，用内存里的旧 self.cfg 会
        # 把磁盘修改覆盖掉。但要**防御性合并** —— 磁盘读取异常时绝不能把
        # 内存里已记住的 IP / 新增预设冲成默认值（踩过：设置改完 IP 没了）。
        disk = load_config()
        if isinstance(disk, dict) and disk.get("ip_history"):
            self.cfg = disk
        else:
            # 磁盘那份看着像空的/读失败 → 保留内存态，只补齐缺失键
            for k, v in disk.items():
                self.cfg.setdefault(k, v)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # ---- 基本 ----
        f1 = ttk.Frame(nb, padding=10)
        nb.add(f1, text="基本")
        r = 0
        ttk.Label(f1, text="默认 IP").grid(row=r, column=0, sticky="w", pady=4)
        e_ip = ttk.Entry(f1, width=26)
        e_ip.insert(0, self.cfg.get("last_ip", ""))
        e_ip.grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(f1, text="端口").grid(row=r, column=0, sticky="w", pady=4)
        e_port = ttk.Entry(f1, width=10)
        e_port.insert(0, str(self.cfg.get("port", 2222)))
        e_port.grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(f1, text="窗口宽度").grid(row=r, column=0, sticky="w", pady=4)
        e_w = ttk.Entry(f1, width=10)
        e_w.insert(0, str(self.cfg.get("window", {}).get("width", 1240)))
        e_w.grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(f1, text="窗口高度").grid(row=r, column=0, sticky="w", pady=4)
        e_h = ttk.Entry(f1, width=10)
        e_h.insert(0, str(self.cfg.get("window", {}).get("height", 780)))
        e_h.grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(f1, text="表间暂停（默认）").grid(row=r, column=0,
                                                sticky="w", pady=4)
        pause_var = tk.StringVar(
            value="yes" if self.cfg.get("pause_between", True) else "no")
        ttk.Combobox(f1, textvariable=pause_var, width=8,
                     values=["yes", "no"], state="readonly"
                     ).grid(row=r, column=1, sticky="w", pady=4)
        ttk.Label(f1, text="手动多选时生效",
                  foreground="#777").grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        ttk.Label(f1, text="导出格式").grid(row=r, column=0, sticky="w", pady=4)
        fmt_var = tk.StringVar(value=fmt_suffix(self.cfg).lstrip(".").lower())
        ttk.Combobox(f1, textvariable=fmt_var, width=8,
                     values=["xlsx", "csv"], state="readonly"
                     ).grid(row=r, column=1, sticky="w", pady=4)
        ttk.Label(f1, text="结果文件格式；csv 通用性最好，xlsx 带表头样式",
                  foreground="#777").grid(row=r, column=2, sticky="w", padx=8)
        # 切换格式时同步改当前输出文件名后缀（否则主窗口还留着旧后缀）
        def _sync_fmt(*_):
            want = ".csv" if fmt_var.get().strip().lower() == "csv" else ".xlsx"
            cur = self.out_var.get().strip()
            if not cur:
                return
            base = os.path.splitext(cur)[0]
            if os.path.splitext(cur)[1].lower() in (".xlsx", ".csv"):
                self.out_var.set(base + want)
        fmt_var.trace_add("write", _sync_fmt)
        r += 1
        # ---- 同时生成 Promax 格式（给只认仪器导出文件的第三方软件）----
        ttk.Label(f1, text="Promax 格式").grid(row=r, column=0, sticky="w", pady=4)
        promax_var = tk.BooleanVar(value=bool(self.cfg.get("export_promax", False)))
        ttk.Checkbutton(f1, text="同时生成 Promax 格式文件（.XML）",
                        variable=promax_var).grid(row=r, column=1, columnspan=2,
                                                  sticky="w", pady=4)
        r += 1
        ttk.Label(f1, text="射频列裁剪").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Label(f1, text="固定：不输出 OFFSET / SR（射频无此概念）",
                  foreground="#777").grid(row=r, column=1, columnspan=2,
                                         sticky="w", pady=4)
        r += 1
        ttk.Label(f1, text="卫星列裁剪").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Label(f1, text="固定：不输出 LEVEL（卫星只报 POWER）",
                  foreground="#777").grid(row=r, column=1, columnspan=2,
                                         sticky="w", pady=4)
        # ---- 断线自动重连（仪器走 WiFi 时链路会抖）----
        # 做成一行（开关 + 间隔），否则 f1 会超出窗口高度把下面的分频段参数挤掉
        r += 1
        ttk.Label(f1, text="断线自动重连").grid(row=r, column=0, sticky="w",
                                            pady=4)
        rrow = ttk.Frame(f1)
        rrow.grid(row=r, column=1, columnspan=2, sticky="w", pady=4)
        auto_var = tk.StringVar(
            value="yes" if self.cfg.get("auto_reconnect", True) else "no")
        ttk.Combobox(rrow, textvariable=auto_var, width=5,
                     values=["yes", "no"], state="readonly"
                     ).pack(side="left")
        ttk.Label(rrow, text="每", foreground="#777").pack(side="left",
                                                         padx=(8, 2))
        e_ri = ttk.Entry(rrow, width=6, justify="right")
        e_ri.insert(0, str(self.cfg.get("reconnect_interval", 10.0)))
        e_ri.pack(side="left")
        ttk.Label(rrow, text="秒自动重连（最小 2 秒）；"
                             "断线后测试暂停，恢复即从当前频点续跑",
                  foreground="#777").pack(side="left", padx=(2, 0))

        # ---- 分频段：射频 / 卫星 两栏并排，一目了然 ----
        r += 1
        band_box = ttk.Frame(f1)
        band_box.grid(row=r, column=0, columnspan=3, sticky="we", pady=(12, 4))
        band_box.columnconfigure(0, weight=1, uniform="band")
        band_box.columnconfigure(1, weight=1, uniform="band")

        ents = {}
        for col, (title, sfx, hint, d_settle, d_retry, d_step) in enumerate((
                # 默认值来自真机实测（与 promax_gui_config.DEFAULT_CONFIG 一致）
                ("射频 TER", "ter", "有线 / 地面，锁定快（实测一次锁）",
                 0.3, 3, 0.3),
                ("卫星 SAT", "sat",
                 "需对星，锁定慢（实测：基线一次锁，个别频道需 1~2 次重试）",
                 3.0, 5, 6.0))):
            lf = ttk.LabelFrame(band_box, text=title, padding=10)
            lf.grid(row=0, column=col, sticky="nsew",
                    padx=(0, 6) if col == 0 else (6, 0))
            ttk.Label(lf, text=hint, foreground="#888").grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
            for i, (label, key, dflt) in enumerate((
                    ("稳定等待 (秒)", "settle_" + sfx,
                     self.cfg.get("settle_" + sfx, d_settle)),
                    ("锁定重试次数", "retry_" + sfx,
                     self.cfg.get("retry_" + sfx, d_retry)),
                    ("重试步进 (秒)", "retry_step_" + sfx,
                     self.cfg.get("retry_step_" + sfx, d_step))), 1):
                ttk.Label(lf, text=label).grid(row=i, column=0,
                                               sticky="w", pady=3)
                e = ttk.Entry(lf, width=8, justify="right")
                e.insert(0, str(dflt))
                # 紧跟标签，不向右侧拉伸（否则窄窗口下输入框会被推到边界外）
                e.grid(row=i, column=1, sticky="w", padx=(14, 0), pady=3)
                ents[key] = e
            lf.columnconfigure(2, weight=1)

        ttk.Label(f1, text="锁定失败时自动重试：每次把稳定等待 +步进 秒；"
                           "用尽仍失败会记为「锁定失败」并写入结果表",
                  foreground="#777", justify="left"
                  ).grid(row=r + 1, column=0, columnspan=3,
                         sticky="w", pady=(0, 4))

        # ---- 预设组 ----
        f2 = ttk.Frame(nb, padding=8)
        nb.add(f2, text="快捷测试组")
        ttk.Label(f2, text="每组一行：按钮名 | 文件名规则 | 文件前缀(逗号分隔) | 表间暂停(yes/no)"
                 ).pack(anchor="w")
        ttk.Label(f2, text="文件名可用 {date8} 8位日期 / {date6} 6位日期；"
                           "第4列省略时按全局默认",
                  foreground="#777").pack(anchor="w")
        tbox = ttk.Frame(f2)
        tbox.pack(fill="both", expand=True, pady=4)
        tbox.rowconfigure(0, weight=1)
        tbox.columnconfigure(0, weight=1)
        # wrap="char"：长行自动折行显示，内容不会被截断。
        # tkinter 的折行只是显示层，get() 取回的仍是原行，按 | 切分不受影响。
        txt = tk.Text(tbox, height=16, wrap="char")
        vsb2 = ttk.Scrollbar(tbox, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb2.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb2.grid(row=0, column=1, sticky="ns")
        lines = []
        for p in presets(self.cfg):
            pz = p.get("pause")
            pz_txt = "" if pz is None else ("yes" if preset_pause(p, self.cfg)
                                            else "no")
            lines.append("%s | %s | %s | %s"
                         % (p.get("name", ""), p.get("filename", ""),
                            ",".join(str(x) for x in p.get("files", [])),
                            pz_txt))
        txt.insert("1.0", "\n".join(lines))

        # ---- 日志 ----
        fl = ttk.Frame(nb, padding=10)
        nb.add(fl, text="日志")
        rl = 0
        ttk.Label(fl, text="日志文件（追加式单文件，所有测试写在一起）",
                  font=("", 9, "bold")).grid(row=rl, column=0, columnspan=3,
                                             sticky="w", pady=(0, 6))
        rl += 1
        ttk.Label(fl, text="日志目录").grid(row=rl, column=0, sticky="w", pady=4)
        e_logdir = ttk.Entry(fl, width=46)
        e_logdir.insert(0, self.cfg.get("log_dir", "") or "")
        e_logdir.grid(row=rl, column=1, sticky="w", pady=4, padx=(6, 6))

        def pick_logdir():
            d = filedialog.askdirectory(
                title="选择日志目录",
                initialdir=e_logdir.get().strip() or os.getcwd())
            if d:
                e_logdir.delete(0, "end")
                e_logdir.insert(0, d)
        ttk.Button(fl, text="浏览...", command=pick_logdir).grid(
            row=rl, column=2, sticky="w")
        rl += 1
        ttk.Label(fl, text="留空 = 当前工作目录（程序所在目录）",
                  foreground="#777").grid(row=rl, column=1, sticky="w")
        rl += 1
        ttk.Label(fl, text="日志文件名").grid(row=rl, column=0, sticky="w", pady=4)
        e_logname = ttk.Entry(fl, width=30)
        e_logname.insert(0, self.cfg.get("log_name", "promax_test.log"))
        e_logname.grid(row=rl, column=1, sticky="w", pady=4, padx=(6, 6))
        rl += 1
        ttk.Separator(fl, orient="horizontal").grid(
            row=rl, column=0, columnspan=3, sticky="ew", pady=10)
        rl += 1
        ttk.Label(fl, text="时间记录", font=("", 9, "bold")).grid(
            row=rl, column=0, columnspan=3, sticky="w", pady=(0, 6))
        rl += 1
        ts_var = tk.StringVar(
            value="yes" if self.cfg.get("log_timestamps", True) else "no")
        ttk.Label(fl, text="行首时戳").grid(row=rl, column=0, sticky="w", pady=4)
        ttk.Combobox(fl, textvariable=ts_var, width=6, state="readonly",
                     values=["yes", "no"]).grid(row=rl, column=1, sticky="w",
                                                padx=(6, 6))
        ttk.Label(fl, text="每行日志前加 [HH:MM:SS.mmm]",
                  foreground="#777").grid(row=rl, column=2, sticky="w")
        rl += 1
        tm_var = tk.StringVar(
            value="yes" if self.cfg.get("log_timing", True) else "no")
        ttk.Label(fl, text="频点耗时").grid(row=rl, column=0, sticky="w", pady=4)
        ttk.Combobox(fl, textvariable=tm_var, width=6, state="readonly",
                     values=["yes", "no"]).grid(row=rl, column=1, sticky="w",
                                                padx=(6, 6))
        ttk.Label(fl, text="每个频点记：调谐/等待/总计 秒数（写入日志与表格）",
                  foreground="#777").grid(row=rl, column=2, sticky="w")
        rl += 1
        cm_var = tk.StringVar(
            value="yes" if self.cfg.get("log_cmd_timing", False) else "no")
        ttk.Label(fl, text="命令级时间").grid(row=rl, column=0, sticky="w", pady=4)
        ttk.Combobox(fl, textvariable=cm_var, width=6, state="readonly",
                     values=["yes", "no"]).grid(row=rl, column=1, sticky="w",
                                                padx=(6, 6))
        ttk.Label(fl, text="每条命令的发起时刻与耗时（日志量较大）",
                  foreground="#777").grid(row=rl, column=2, sticky="w")
        rl += 1
        ttk.Separator(fl, orient="horizontal").grid(
            row=rl, column=0, columnspan=3, sticky="ew", pady=10)
        rl += 1
        sep_var = tk.BooleanVar(
            value=bool(self.cfg.get("log_separate", False)))
        ttk.Checkbutton(fl, text="同时生成独立日志", variable=sep_var).grid(
            row=rl, column=0, sticky="w", pady=4)
        ttk.Label(fl, text="与结果文件同目录同主名（每次覆盖），只含本次测试，"
                           "便于调试时单独打开",
                  foreground="#777").grid(row=rl, column=1, columnspan=2,
                                          sticky="w", padx=(6, 0))
        rl += 1
        ttk.Separator(fl, orient="horizontal").grid(
            row=rl, column=0, columnspan=3, sticky="ew", pady=10)
        rl += 1
        ttk.Button(fl, text="读取日志",
                   command=lambda: self._view_log(win)).grid(
            row=rl, column=0, sticky="w")
        ttk.Label(fl, text="查看单一日志文件末尾（跨次累积，与上面的独立日志无关）",
                  foreground="#777").grid(row=rl, column=1, columnspan=2,
                                          sticky="w", padx=(6, 0))

        # ---- 原始 json ----
        f3 = ttk.Frame(nb, padding=8)
        nb.add(f3, text="原始 JSON")
        ttk.Label(f3, text="直接编辑配置文件，保存后重启生效"
                 ).pack(anchor="w")
        jbox = ttk.Frame(f3)
        jbox.pack(fill="both", expand=True, pady=4)
        jbox.rowconfigure(0, weight=1)
        jbox.columnconfigure(0, weight=1)
        raw = tk.Text(jbox, height=18, wrap="none")
        vsb3 = ttk.Scrollbar(jbox, orient="vertical", command=raw.yview)
        hsb3 = ttk.Scrollbar(jbox, orient="horizontal", command=raw.xview)
        raw.configure(yscrollcommand=vsb3.set, xscrollcommand=hsb3.set)
        raw.grid(row=0, column=0, sticky="nsew")
        vsb3.grid(row=0, column=1, sticky="ns")
        hsb3.grid(row=1, column=0, sticky="ew")
        raw.insert("1.0", json.dumps(self.cfg, ensure_ascii=False, indent=2))

        def do_save():
            try:
                # 预设组
                ps = []
                for ln in txt.get("1.0", "end").strip().splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    parts = [x.strip() for x in ln.split("|")]
                    if len(parts) < 3:
                        continue
                    entry = {"name": parts[0], "filename": parts[1],
                             "files": [f.strip() for f in parts[2].split(",")
                                       if f.strip()]}
                    # 第4列：表间暂停（yes/no），省略则不加该键
                    if len(parts) >= 4 and parts[3]:
                        entry["pause"] = parts[3].lower() in (
                            "1", "true", "yes", "y", "on", "是")
                    ps.append(entry)
                # 基底：以**当前内存配置**为准，再叠加「原始 JSON」文本框里
                # 用户手改的键。⚠️ 不能直接拿文本框内容当基底 —— 那是打开
                # 设置窗那一刻的快照，会把期间新增的预设/记住的 IP 冲掉
                # （踩过：新增快速测试组"消失"）。文本框只作为**额外键**的来源。
                base = json.loads(json.dumps(self.cfg))
                try:
                    extra = json.loads(raw.get("1.0", "end"))
                    if isinstance(extra, dict):
                        for k, v in extra.items():
                            base[k] = v
                except Exception:
                    pass
                base["last_ip"] = e_ip.get().strip()
                try:
                    base["port"] = int(e_port.get().strip())
                except ValueError:
                    base["port"] = 2222
                base["pause_between"] = pause_var.get().strip().lower() in (
                    "yes", "y", "true", "1", "on", "是")
                base["export_format"] = (
                    "csv" if fmt_var.get().strip().lower() == "csv"
                    else "xlsx")
                base["export_promax"] = bool(promax_var.get())
                # ---- 断线自动重连 ----
                base["auto_reconnect"] = auto_var.get().strip().lower() in (
                    "yes", "y", "true", "1", "on", "是")
                try:
                    base["reconnect_interval"] = max(
                        2.0, float(e_ri.get().strip()))
                except ValueError:
                    base["reconnect_interval"] = 10.0
                # 分频段参数（注意：变量名不要用 txt，会覆盖上面的预设 Text 控件）
                for key, e in ents.items():
                    v = e.get().strip()
                    if not v:
                        continue
                    # retry_ter/retry_sat 是整数次数；
                    # retry_step_* 也是以 retry 开头但必须是浮点，
                    # 所以不能用 startswith("retry_") 判断（曾导致步进值从未保存）
                    is_int = key in ("retry_ter", "retry_sat")
                    try:
                        base[key] = int(v) if is_int else float(v)
                    except ValueError:
                        pass
                try:
                    base.setdefault("window", {})["width"] = int(e_w.get().strip())
                    base["window"]["height"] = int(e_h.get().strip())
                except ValueError:
                    pass
                # ---- 日志设置 ----
                base["log_dir"] = e_logdir.get().strip()
                base["log_name"] = (e_logname.get().strip()
                                    or "promax_test.log")
                for key, var in (("log_timestamps", ts_var),
                                 ("log_timing", tm_var),
                                 ("log_cmd_timing", cm_var)):
                    base[key] = var.get().strip().lower() in (
                        "yes", "y", "true", "1", "on", "是")
                base["log_separate"] = bool(sep_var.get())
                if ps:
                    base["presets"] = ps
                save_config(base)
                self.cfg = base
                self.ip_var.set(base.get("last_ip", ""))
                self.port_var.set(str(base.get("port", 2222)))
                w = base.get("window", {})
                self.root.geometry("%dx%d" % (w.get("width", 1240),
                                              w.get("height", 780)))
                self._build_presets()
                self._sync_sel()
                messagebox.showinfo("已保存", "配置已写入 %s" % CONFIG_NAME)
                win.destroy()
            except Exception as e:
                messagebox.showerror("保存失败", str(e))

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="保存", command=do_save).pack(side="right")
        ttk.Button(btns, text="取消", command=win.destroy).pack(
            side="right", padx=6)

    # ---------------- 测试流程 ----------------
    def _selected(self):
        keys = [k for k, v in self.check_vars.items() if v.get()]
        out = []
        for k in keys:
            it = self._by_key(k)
            if it:
                out.append(it)
        return out

    def start(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning("缺少IP", "请先填写仪器 IP 地址")
            return
        self._save_ip()
        items = self._selected()
        if not items:
            messagebox.showwarning("未选频道表", "请至少勾选一个频道表")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("缺少输出文件", "请选择结果保存位置")
            return
        # 记住本次输出目录
        d = os.path.dirname(out)
        if d and os.path.isdir(d):
            self._remember_outdir(d)
        if not (out.lower().endswith(".xlsx") or out.lower().endswith(".csv")):
            out = _fix_suffix(out, self.cfg)
            self.out_var.set(out)
        # ---- 输出文件：同名提示覆盖 / 改名 ----
        out = self._resolve_output_path(out)
        if not out:
            return
        self.out_path = out
        self.rows = {}
        # 新一轮测试：清掉上一轮的进度标记（本轮高亮由 MSG_PLAN_START 驱动）
        self.sel_done = set()
        self.sel_active = None
        self._paint_sel()
        # 进度条归零；「全部」总数按所选表的**频道数**合计
        self._pg_reset(items)

        # ---- 打开日志文件（与结果同目录、同主名）----
        self._open_log_file(out)

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        self.stop_btn.configure(state="normal")
        self.retry_btn.configure(state="disabled")   # 新的一轮：先复位重连按钮
        # 新的一轮：清掉上一轮的"失败频道"待办
        self.pending_retest = []
        self.retesting = False
        self._retest_subset = {}
        self.retest_btn.configure(text="重测失败频道", state="disabled")
        self._timer_start()                          # 计时从这一刻起
        self.status.set("测试中...")
        self.root.title("PROMAX 频道表批量测试 — 测试中")

        self.q = queue.Queue()
        try:
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            port = 2222
        self.engine = TestEngine(self.q, port)
        # 表间是否暂停：预设里配了就听预设，否则用全局默认
        if self.active_preset:
            pause_between = preset_pause(self.active_preset, self.cfg)
            src = "预设「%s」" % self.active_preset.get("name", "")
        else:
            pause_between = bool(self.cfg.get("pause_between", True))
            src = "全局默认"
        if len(items) > 1:
            self._log("表间暂停：%s（来源：%s）"
                      % ("是" if pause_between else "否", src))
        self.thread = threading.Thread(
            target=self.engine.run,
            args=(ip, items, self.cfg, 20.0, pause_between),
            daemon=True)
        self.thread.start()
        self.root.after(120, self._poll)

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.status.set("正在停止...")

    def retry_conn(self):
        """点「重试连接」：立刻唤醒后台线程去重连（不阻塞 UI）。

        后台的 _wait_reconnect 收到信号后会立刻试一次；
        重连成功即从当前频点继续，失败则回到等待（日志里会说明原因）。
        """
        if self.engine:
            self.status.set("正在重连...")
            self.engine.retry_connect()

    def retest_failed(self):
        """只重测上一轮未成功的频道，结果**覆盖合并**回同一个结果文件。

        设计要点
        --------
        · 已成功的频道一条都不重测 —— 这正是本功能的意义（卫星整表重跑太贵）
        · 行按频道名覆盖，不产生重复行；收尾保存前按 idx 排序，位置与原表一致
        · 表级准备照做（切频段 / LNB / 校验 BAND）：重测可能跨表、跨频段
        · 断线重连、计时器、进度条全部复用正常测试那一套
        """
        if not self.pending_retest:
            return
        if self.thread and self.thread.is_alive():
            messagebox.showwarning("正在测试", "当前还有测试在跑，等它结束再重测")
            return
        out = self.out_path or ""
        if not out:
            messagebox.showwarning("没有结果文件",
                                   "上一轮没有保存结果，无法把重测数据合并进去")
            return
        # 按表分组（表顺序沿用本次测试的选中顺序）
        order = {it.key: n for n, it in enumerate(self.items)}
        by_item = {}
        for item, idx, pt, why in self.pending_retest:
            by_item.setdefault(item.key, []).append((idx, pt))
        items = [it for it in self.items if it.key in by_item]
        items.sort(key=lambda it: order.get(it.key, 0))
        names = "、".join("%s(%d)" % (it.name, len(by_item[it.key])) for it in items)
        if not messagebox.askyesno(
                "重测失败频道",
                "上一轮有 %d 个频道未成功：\n%s\n\n只重测这些频道，结果合并到：\n%s\n\n"
                "（已成功的频道不会重测）"
                % (len(self.pending_retest), names, out)):
            return

        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showwarning("缺少IP", "请先填写仪器 IP 地址")
            return
        self._save_ip()
        self._retest_subset = by_item
        self._retest_prev = list(self.pending_retest)   # 收尾时要把没跑完的并回去
        self.retesting = True
        self._pg_reset_subset(by_item)
        self._timer_start()
        self.stop_btn.configure(state="normal")
        self.retry_btn.configure(state="disabled")
        self.retest_btn.configure(state="disabled")
        self.status.set("重测中...")
        self.root.title("PROMAX 频道表批量测试 — 重测失败频道")
        self.q = queue.Queue()
        try:
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            port = 2222
        self.engine = TestEngine(self.q, port)
        self._log("")
        self._log("=" * 60)
        self._log("【重测失败频道】共 %d 个：%s" % (len(self.pending_retest), names))
        self._log("=" * 60)
        self.thread = threading.Thread(
            target=self.engine.run,
            args=(ip, items, self.cfg, 20.0, False),      # 重测不暂停（同一张表内跑完）
            kwargs={"subset_map": by_item},
            daemon=True)
        self.thread.start()
        self.root.after(120, self._poll)

    def _on_close(self):
        """关窗口：先停测试、断开连接，再销毁窗口。

        仪器只允许一个 telnet 会话。如果直接 destroy，后台线程会被连同
        socket 一起丢弃，socket 只发 FIN（不是 RST）→ 仪器可能卡在
        FIN_WAIT_2 不释放会话 → 下一次连接就"连得上但完全收不到回复"。
        """
        t = getattr(self, "thread", None)
        if self.engine:
            try:
                self.engine.stop()
            except Exception:
                pass
        if t is not None and t.is_alive():
            t.join(timeout=3.0)          # 让 run() 的 finally 自己收尾
        if self.engine:
            try:
                self.engine.close()      # 兜底：确保 SO_LINGER RST 关掉
            except Exception:
                pass
        try:
            self._close_log_file()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------------- 消息轮询 ----------------
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.thread and self.thread.is_alive():
            self.root.after(120, self._poll)
        else:
            # 线程已退出，把残余消息收干净
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    self._handle(kind, payload)
            except queue.Empty:
                pass
            self._finish_ui()

    def _store_row(self, item, row):
        """把一条结果收进 self.rows。

        重测模式下**按频道名覆盖**同行（同一个频道不该在表里出现两行），
        正常测试直接追加。行的先后顺序由收尾时按 idx 排序保证
        （重测的行是"补回来"的，追加在末尾会跑到表格最后）。
        """
        lst = self.rows.setdefault(item.name, [])
        if self.retesting:
            for i, old in enumerate(lst):
                if old.get("ch") == row.get("ch"):
                    lst[i] = row
                    return
        lst.append(row)

    def _collect_failures(self):
        """从引擎收集失败频道，按 (表, 频道名) 去重后返回。"""
        seen, out = set(), []
        for item, idx, pt, why in (getattr(self.engine, "failures", None) or []):
            k = (item.key, pt[2])
            if k in seen:
                continue
            seen.add(k)
            out.append((item, idx, pt, why))
        return out

    def _update_retest_btn(self):
        """按"有没有失败频道 / 有没有结果文件 / 是否正在跑"刷新重测按钮。"""
        n = len(self.pending_retest)
        busy = bool(self.thread and self.thread.is_alive())
        self.retest_btn.configure(
            text=("重测失败频道 (%d)" % n) if n else "重测失败频道",
            state=("normal" if (n and self.out_path and not busy)
                   else "disabled"))

    def _handle(self, kind, payload):
        if kind == TestEngine.MSG_LOG:
            self._log(payload)
        elif kind == TestEngine.MSG_ROW:
            item, row = payload
            self._store_row(item, row)
            parts = []
            # 注意：射频(TER)没有 OFFSET 键，必须用「键存在且非空」判断，
            # 不能用 != ""（键缺失时 get() 返回 None，None != "" 为 True 会误入分支）
            for k in ("LEVEL", "CN", "POWER", "MER"):
                v = row.get(k)
                if v not in (None, ""):
                    parts.append("%s=%s%s" % (k, v, row.get(k + "_unit", "")))
            ov = row.get("OFFSET")
            if ov not in (None, ""):
                try:
                    parts.append("OFFSET=%gkHz" % (round(float(ov) / 1000.0, 3)))
                except (TypeError, ValueError):
                    parts.append("OFFSET=%s" % ov)
            # 频点序号带总数：`[  3/20]`，一眼看出还剩多少
            # （总数未知时退化成 `[  3]`，不显示 `[3/0]` 这种误导性分母）
            tag = ("[%3d/%d]" % (row["idx"], self.pg_cur_total)
                   if self.pg_cur_total else "[%3d]" % row["idx"])
            self._log("  %s %-14s %9.3f MHz  LOCK=%-4s %s"
                      % (tag, row["ch"], float(row["freq_mhz"]),
                         row.get("LOCKED", "-"), "  ".join(parts)))
            # 一个频点测完了 → 两条都前进一格
            self.pg_cur_done += 1
            self.pg_all_done += 1
            self._update_progress()
        elif kind == TestEngine.MSG_PLAN_START:
            item = payload
            self.sel_active = item.key
            # 换表了：本表进度归零、总数换成这张表的频点数
            # （重测模式下是"本表要重测的频道数"，不是整表频道数）
            self.pg_cur_total = (len(self._retest_subset.get(item.key, ()))
                                 or len(item.points()))
            self.pg_cur_done = 0
            self._paint_sel()
            self._update_progress()
        elif kind == TestEngine.MSG_PLAN_DONE:
            item, rows = payload
            self.sel_done.add(item.key)
            if self.sel_active == item.key:
                self.sel_active = None      # 这张测完了，等下一张的 START
            self._paint_sel()
            # 补齐当前表：个别频点彻底失败（设置 NAK/超时）时不会发 MSG_ROW，
            # 不补的话进度条会永远差一格，看着像卡住了。
            # 每个频点最终**一定有结论**（成功写行 / 失败也在日志里落了记录），
            # 所以这里补满是对语义的正确表达；差值同步补进「全部」。
            missed = self.pg_cur_total - self.pg_cur_done
            if missed > 0:
                self.pg_cur_done = self.pg_cur_total
                self.pg_all_done += missed
            self._update_progress()
            self._log("【%s】完成，共 %d 个频道" % (item.name, len(rows)))
        elif kind == TestEngine.MSG_CONFIRM:
            # 换线暂停：等用户接好线点「继续」的这段时间**不算**在用时里
            self._timer_pause()
            self._confirm(payload)
        elif kind == TestEngine.MSG_NETLOST:
            # 断线：测试已暂停（后台在等重连）。亮出「重试连接」，状态栏说明情况。
            self._timer_pause()          # 等重连的时间也不计入用时
            self.retry_btn.configure(state="normal")
            self.status.set("网络中断，已暂停 — 等待重连")
            self.root.title("PROMAX 频道表批量测试 — 已暂停（网络中断）")
            self._log("")
            self._log("!! 与仪器的连接断开，测试已**暂停**（数据不丢）。")
            self._log("   点「重试连接」立即重试；默认也会定时自动重连；")
            self._log("   点「中止」则结束本次测试并保存已测得的结果。")
        elif kind == TestEngine.MSG_NETOK:
            self.retry_btn.configure(state="disabled")
            self._timer_resume()         # 重连成功，继续计时
            n = self.engine.reconnects if self.engine else 0
            self.status.set(("测试中...（已重连 %d 次）" % n) if n else "测试中...")
            self.root.title("PROMAX 频道表批量测试 — 测试中")
            self._log("   连接已恢复（累计重连 %d 次），继续测试。" % n)
        elif kind in (TestEngine.MSG_FINISH, TestEngine.MSG_STOPPED):
            pass    # 由 _poll 线程结束后统一收尾

    def _log(self, text):
        # 行首时戳（可在设置里关掉）。日志面板与文件同步加，方便对照。
        # 空行不加时戳，避免噪音。
        if text and self.cfg.get("log_timestamps", True):
            text = "[%s] %s" % (_ts(), text)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        # 同步落盘，程序意外中断也不丢日志
        # 两个句柄都写：log_fp=单一日志（追加）、sep_log_fp=独立日志（本次）
        for name in ("log_fp", "sep_log_fp"):
            fp = getattr(self, name, None)
            if fp:
                try:
                    fp.write(text + "\n")
                    fp.flush()
                except Exception:
                    pass

    def _confirm(self, text):
        """弹暂停窗，等用户确认"""
        win = tk.Toplevel(self.root)
        win.title("更换接口")
        win.transient(self.root)
        win.grab_set()
        win.geometry("460x200")
        ttk.Label(win, text=text, wraplength=420, justify="left"
                  ).pack(padx=16, pady=16, fill="both", expand=True)

        def go(v):
            self.engine.resolve(v)
            win.destroy()
            if v:
                self._timer_resume()     # 接好线继续 → 重新计时
            # 选中止则保持停表，收尾时 _timer_stop 会把最终用时定格

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=16, pady=12)
        ttk.Button(btns, text="继续", command=lambda: go(True)).pack(
            side="right", padx=6)
        ttk.Button(btns, text="中止测试", command=lambda: go(False)).pack(
            side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: go(False))

    # ---------------- 输出文件处理 ----------------
    def _downlink_map(self):
        """(表名, 频道名) → 下行 MHz。

        我们结果里卫星存的是**中频**（发仪器的就是 IF），而 Promax 格式写的是
        `downlink`（下行）。用当前加载的频道表把两者对上。
        """
        m = {}
        for it in getattr(self, "items", None) or []:
            for c in getattr(it, "channels", None) or []:
                f = c.get("freq_mhz")
                if f is not None:
                    m[(str(it.name).strip(), str(c.get("name")).strip())] = f
        return m

    def convert_promax(self):
        """把已有的 xlsx / csv 结果转成 Promax 格式 XML（不连仪器）。"""
        cur = self.out_var.get().strip()
        init_dir = os.path.dirname(cur) if cur else ""
        if not init_dir or not os.path.isdir(init_dir):
            init_dir = self.outdir_var.get().strip() or default_out_dir(self.cfg)
        src = filedialog.askopenfilename(
            title="选择要转换的结果文件（xlsx / csv）",
            initialdir=init_dir if os.path.isdir(init_dir) else None,
            filetypes=[("结果文件", "*.xlsx *.csv"), ("Excel 表格", "*.xlsx"),
                       ("CSV 文本", "*.csv"), ("所有文件", "*.*")])
        if not src:
            return
        dst = default_promax_path(src)
        if os.path.exists(dst) and not messagebox.askyesno(
                "文件已存在", "已存在：\n%s\n\n覆盖它？" % dst):
            return
        try:
            dst, n = convert_result_file(src, dst, self._downlink_map(),
                                         plans_dir=getattr(self, "source", None))
        except Exception as e:
            self._log("!! Promax 格式转换失败：%s" % e)
            messagebox.showerror("转换失败", "%s\n\n%s" % (src, e))
            return
        self.status.set("已生成 %s" % os.path.basename(dst))
        self._log("")
        self._log("已生成 Promax 格式文件: %s（%d 个频道）" % (dst, n))
        messagebox.showinfo("转换完成",
                            "已生成 Promax 格式文件：\n%s\n\n共 %d 个频道"
                            % (dst, n))

    def _resolve_output_path(self, out):
        """同名文件提示覆盖/改名。返回最终路径，取消则返回 None"""
        if not (out.lower().endswith(".xlsx") or out.lower().endswith(".csv")):
            out = _fix_suffix(out, self.cfg)
            self.out_var.set(out)
        if not os.path.exists(out):
            return out
        # 同名文件已存在 → 三选一
        win = tk.Toplevel(self.root)
        win.title("文件已存在")
        win.transient(self.root)
        win.grab_set()
        win.geometry("460x180")
        ttk.Label(win, wraplength=420, justify="left",
                  text="目标文件已存在：\n%s\n\n请选择处理方式：" % out
                  ).pack(padx=16, pady=16, anchor="w")
        res = {"v": None}

        def pick(v):
            res["v"] = v
            win.destroy()

        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=16, pady=8)
        ttk.Button(bf, text="覆盖", command=lambda: pick("overwrite")
                   ).pack(side="right", padx=4)
        ttk.Button(bf, text="修改文件名", command=lambda: pick("rename")
                   ).pack(side="right", padx=4)
        ttk.Button(bf, text="取消", command=lambda: pick(None)
                   ).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: pick(None))
        self.root.wait_window(win)

        if res["v"] == "overwrite":
            return out
        if res["v"] == "rename":
            newp = filedialog.asksaveasfilename(
                title="另存为", defaultextension=fmt_suffix(self.cfg),
                initialdir=os.path.dirname(out),
                initialfile=os.path.basename(out),
                filetypes=[("Excel 表格", "*.xlsx"), ("CSV 文本", "*.csv"),
                           ("所有文件", "*.*")])
            if not newp:
                return None
            self.out_var.set(newp)
            d = os.path.dirname(newp)
            if d:
                self._remember_outdir(d)
            # 换个名字后仍可能撞名，递归确认
            return self._resolve_output_path(newp)
        return None

    def _save_with_retry(self, path):
        """保存结果；文件被占用时提供「重试」，而不是报个错就退出

        返回 (成功?, 最终路径或 None)
        """
        sheet_title = None
        if self.active_preset:
            sheet_title = preset_filename(self.active_preset)
        while True:
            try:
                written = write_result(path, self.rows, sheet_title=sheet_title)
                return True, written
            except PermissionError:
                # 典型场景：xlsx 正被 Excel 打开
                win = tk.Toplevel(self.root)
                win.title("文件被占用")
                win.transient(self.root)
                win.grab_set()
                win.geometry("500x210")
                ttk.Label(win, wraplength=460, justify="left",
                          text=("无法写入，文件可能正被其他程序打开：\n%s\n\n"
                                "请关闭打开该文件的程序（如 Excel），"
                                "然后点「重试」。\n"
                                "也可以「另存为」换个文件名。" % path)
                          ).pack(padx=16, pady=16, anchor="w")
                res = {"v": None}

                def pick(v):
                    res["v"] = v
                    win.destroy()

                bf = ttk.Frame(win)
                bf.pack(fill="x", padx=16, pady=8)
                ttk.Button(bf, text="重试", command=lambda: pick("retry")
                           ).pack(side="right", padx=4)
                ttk.Button(bf, text="另存为...", command=lambda: pick("saveas")
                           ).pack(side="right", padx=4)
                ttk.Button(bf, text="取消", command=lambda: pick(None)
                           ).pack(side="right")
                win.protocol("WM_DELETE_WINDOW", lambda: pick(None))
                self.root.wait_window(win)

                if res["v"] == "retry":
                    continue                    # 再试一次
                if res["v"] == "saveas":
                    newp = filedialog.asksaveasfilename(
                        title="另存为", defaultextension=fmt_suffix(self.cfg),
                        initialdir=os.path.dirname(path),
                        initialfile=os.path.basename(path),
                        filetypes=[("Excel 表格", "*.xlsx"),
                                   ("CSV 文本", "*.csv"), ("所有文件", "*.*")])
                    if not newp:
                        return False, None
                    path = newp
                    self.out_var.set(path)
                    continue
                return False, None
            except Exception as e:
                messagebox.showerror("保存失败", str(e))
                return False, None

    # ---------------- 日志文件 ----------------
    def _log_header(self, out_path):
        """本次测试的日志表头（两种日志共用，保证内容一致）"""
        lines = []
        lines.append("=" * 78)
        lines.append("【测试开始】%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("仪器: %s:%s" % (self.ip_var.get().strip(),
                                    self.port_var.get()))
        lines.append("结果: %s" % out_path)
        if self.active_preset:
            lines.append("预设: %s" % self.active_preset.get("name", ""))
        try:
            settle, retry, step = band_settings(
                self.cfg_run or self.cfg, "SAT")
            lines.append("参数: 卫星 settle=%.1fs retry=%d step=%.1fs"
                         % (settle, retry, step))
        except Exception:
            pass
        lines.append("-" * 78)
        return "\n".join(lines) + "\n"

    def _open_log_file(self, out_path):
        """打开日志。

        ① **单一日志**（追加模式，默认 ./promax_test.log）：所有测试写在一起，
           供「读取日志」按钮查看、跨次做趋势分析。
        ② **独立日志**（可选，勾选「同时生成独立日志」时）：与结果文件
           同目录同主名（如 卫星全频道_主_20261014.log），**覆盖式**，
           只含本次测试 —— 便于调试时单独打开查看，与「读取日志」按钮无关。
        """
        self._close_log_file()
        header = self._log_header(out_path)      # 表头单独生成，失败不影响开文件
        # ---- ① 单一日志：追加 ----
        self.log_path = log_path(self.cfg)
        try:
            new = not os.path.exists(self.log_path) or \
                os.path.getsize(self.log_path) == 0
            self.log_fp = open(self.log_path, "a", encoding="utf-8")
            if new:
                self.log_fp.write("PROMAX 频道表批量测试 日志（追加式单文件）\n")
                self.log_fp.write("=" * 78 + "\n")
            self.log_fp.write("\n" + header)
            self.log_fp.flush()
        except Exception:
            self.log_fp = None
            self.log_path = None

        # ---- ② 独立日志：覆盖（与结果同目录同主名）----
        self.sep_log_path = None
        self.sep_log_fp = None
        if self.cfg.get("log_separate", False) and out_path:
            base, _ = os.path.splitext(out_path)
            self.sep_log_path = os.path.abspath(base + ".log")
            try:
                self.sep_log_fp = open(self.sep_log_path, "w",
                                       encoding="utf-8")
                self.sep_log_fp.write("PROMAX 频道表批量测试 日志（独立）\n")
                self.sep_log_fp.write(header)
                self.sep_log_fp.flush()
            except Exception:
                self.sep_log_fp = None
                self.sep_log_path = None

    def _close_log_file(self, tail=""):
        end = "【测试结束】%s\n" % time.strftime("%Y-%m-%d %H:%M:%S")
        for name in ("log_fp", "sep_log_fp"):
            fp = getattr(self, name, None)
            if fp:
                try:
                    if tail:
                        fp.write(tail)
                    fp.write(end)
                    fp.flush()
                    fp.close()
                except Exception:
                    pass
            setattr(self, name, None)

    def _finish_ui(self):
        self.stop_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")   # 测试结束
        # 停表：顶栏定格在最终用时（暂停期间不计数）
        used = self._timer_stop()
        # 本轮跑完（或中止）：撤掉"正在测试"的高亮，已完成的行保留绿色
        self.sel_active = None
        self._paint_sel()

        # 断线统计：让用户一眼看到本次受过几次网络影响
        try:
            n = self.engine.reconnects if self.engine else 0
        except Exception:
            n = 0
        if n:
            self._log("")
            self._log("【网络恢复】本次测试期间断线重连 %d 次（数据已保留）。" % n)
        self._log("")
        self._log("【用时】本轮净用时 %s（断线等待 / 换线暂停不计入）"
                  % _fmt_hms(used))
        # 失败频道：未锁定的 + 设置阶段就失败的（后者连行都没有）
        # 🔴 重测模式要**并上"还没补测的那些"**：这一轮只跑了 3 个频道，
        #    引擎的 failures 只反映这 3 个，剩下没跑的不能凭空消失。
        was_retest = self.retesting
        new_fail = self._collect_failures()
        if was_retest:
            done = {(k, pt[2]) for k, lst in self._retest_subset.items()
                    for _i, pt in lst}
            kept = [f for f in getattr(self, "_retest_prev", [])
                    if (f[0].key, f[2][2]) not in done]
            self.pending_retest = kept + new_fail
        else:
            self.pending_retest = new_fail
        if self.pending_retest:
            by = {}
            for item, idx, pt, why in self.pending_retest:
                by.setdefault(item.name, []).append(pt[2])
            self._log("")
            self._log("【未成功】%d 个频道：%s"
                      % (len(self.pending_retest),
                         "；".join("%s: %s" % (k, "、".join(v))
                                   for k, v in by.items())))
            self._log("   点工具栏「重测失败频道」只补测这些频道，"
                      "结果合并进同一个文件（已成功的不重测）。")
        stopped = self.engine and self.engine._stop.is_set()
        self.root.title("PROMAX 频道表批量测试")
        if self.rows:
            # 重测回来的行是"覆盖/追加"进去的 → 保存前按原始序号重排，
            # 保证结果表里的频道顺序与频道表一致（不会把补测的行堆到末尾）
            for _rows in self.rows.values():
                try:
                    _rows.sort(key=lambda r: int(r.get("idx") or 0))
                except (TypeError, ValueError):
                    pass
            ok, path = self._save_with_retry(self.out_path)
            if ok:
                self.out_path = path
                self.status.set("已保存 %s" % path)
                self._log("")
                self._log("结果已保存: %s" % path)
                self._log("共 %d 张表, %d 条记录"
                          % (len(self.rows),
                             sum(len(v) for v in self.rows.values())))
                # 设置里开了「同时生成 Promax 格式」→ 再落一份 .XML
                # （给只认仪器导出文件的第三方软件；失败不影响结果文件）
                if self.cfg.get("export_promax", False):
                    try:
                        px, nch = write_promax_for_results(
                            self.rows, default_promax_path(path),
                            self._downlink_map())
                        self._log("Promax 文件: %s（%d 个频道）" % (px, nch))
                    except Exception as e:
                        self._log("!! Promax 文件生成失败：%s" % e)
                if self.log_path:
                    # 收尾：写"测试结束"再关闭（追加式，不删历史）
                    summary = ("【本次汇总】%d 张表 / %d 条记录，净用时 %s\n"
                               % (len(self.rows),
                                  sum(len(v) for v in self.rows.values()),
                                  _fmt_hms(used)))
                    self._close_log_file(tail=summary)
                    self._log("日志: %s（追加）" % self.log_path)
                    if self.sep_log_path:
                        self._log("独立日志: %s（本次，覆盖）"
                                  % self.sep_log_path)
                else:
                    self._close_log_file()
                if self.sep_log_path:
                    self._log("独立日志: %s（本次，覆盖）" % self.sep_log_path)
                if not stopped and not was_retest and messagebox.askyesno(
                        "完成", "测试完成，结果已保存：\n%s\n\n现在打开文件吗？"
                                % path):
                    self._open(path)
            else:
                self.status.set("保存已取消")
                self._log("结果未保存（用户取消或保存失败）")
                self._close_log_file()
        else:
            self.status.set("中止" if stopped else "无数据")
            self._log("没有可保存的数据")
            self._close_log_file()

        # ---- 收尾：刷新「重测失败频道」按钮；重测则报告结果 ----
        tried = 0
        if was_retest:
            tried = sum(len(v) for v in self._retest_subset.values())
            self._log("")
            self._log("【重测完成】本次重测 %d 个频道，其中 %d 个仍未成功。"
                      % (tried, len(self.pending_retest)))
        self.retesting = False
        self._update_retest_btn()
        if was_retest and not stopped:
            if self.pending_retest:
                messagebox.showwarning(
                    "重测完成",
                    "重测结束：%d 个频道仍未成功（%s）。\n\n结果已更新到：\n%s"
                    % (len(self.pending_retest),
                       "、".join(sorted({pt[2] for _i, _x, pt, _w
                                         in self.pending_retest})),
                       self.out_path))
            else:
                messagebox.showinfo(
                    "重测完成",
                    "全部 %d 个频道已成功锁定，结果已更新：\n%s"
                    % (tried, self.out_path))

    # ---------------- 读取日志（设置窗按钮）----------------
    def _view_log(self, parent=None):
        """读取单一日志文件并在窗口里展示全部内容。

        窗口形态：**标准 Windows 窗口**——右上角有 最小化 / 最大化 / 关闭 三个
        原生按钮，可拖拽边框缩放，最小化后进任务栏（不再是 transient 工具对话框，
        那种窗口右上角只有关闭一个按钮）。

        ⚠️ 从设置窗调用时，设置窗正持 `grab_set()`（模态）。一个**非 transient**
        的普通窗口会被它的 grab 吃掉输入（表现为"日志窗点了没反应"）。
        所以这里不是把日志窗塞进设置窗的 grab 体系，而是：
          · 打开日志窗前，**临时释放设置窗的 grab**（`grab_release`）；
          · 日志窗正常交互、缩放、最大化；
          · 日志窗关闭时，再把 grab **还给**设置窗（若它还在）。
        这样既保住了标准窗口外观，又不会被模态设置窗挡住。

        （底部不再放"最大化"按钮 —— 用系统标题栏自带的即可。）
        """
        path = log_path(self.cfg)
        if not os.path.isfile(path):
            messagebox.showinfo("日志", "日志文件不存在：\n%s\n\n"
                                        "（首次测试后才会生成）" % path)
            return
        master = parent if parent is not None else self.root
        win = tk.Toplevel(master)
        win.title("日志 — %s" % path)
        # 标准窗口：不 transient（transient 会退化成只有关闭按钮的工具窗），
        # 保留系统标题栏的最小化/最大化/关闭三个按钮。
        win.geometry("1000x700")
        win.minsize(420, 240)
        win.resizable(True, True)
        try:
            win.attributes("-toolwindow", False)   # 确保不是工具窗
        except Exception:
            pass

        # 临时让出设置窗的模态 grab，日志窗才能正常操作。
        # ⚠️ `grab_current()` 的返回值不能可靠地与 str(widget) 比较（不同 Tk
        # 版本可能返回 widget、字符串或 None），所以这里直接尝试释放；释放前
        # 用 try 判断是否真的持有 grab（拿不到就说明没 grab，无需归还）。
        grabbed = None
        if parent is not None:
            try:
                held = parent.grab_current()
                # held 非空即表示当前有 grab；它通常是 parent 自身或其后代
                if held is not None:
                    parent.grab_release()
                    grabbed = parent
            except Exception:
                grabbed = None

        def _on_close():
            # 归还 grab 给设置窗（如果它还在），再销毁日志窗
            if grabbed is not None:
                try:
                    if grabbed.winfo_exists():
                        grabbed.grab_set()
                except Exception:
                    pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text=path, foreground="#555").pack(side="left")
        size = os.path.getsize(path)
        ttk.Label(top, text="  %.1f KB" % (size / 1024.0),
                  foreground="#999").pack(side="left")

        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        txt = tk.Text(box, wrap="none")
        vsb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        hsb = ttk.Scrollbar(box, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            content = "读取失败: %s" % e
        txt.insert("1.0", content)
        txt.see("end")          # 直接定位到最新一次测试

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="打开文件", command=lambda: self._open(path)
                   ).pack(side="left")
        ttk.Button(btns, text="复制全部",
                   command=lambda: (self.root.clipboard_clear(),
                                    self.root.clipboard_append(content),
                                    messagebox.showinfo("已复制", "日志已复制到剪贴板"))
                   ).pack(side="left", padx=6)
        ttk.Button(btns, text="最新", command=lambda: txt.see("end")
                   ).pack(side="left")
        # 关闭按钮（右上角已有系统"关闭"，这里保留一个便于习惯操作）
        ttk.Button(btns, text="关闭", command=_on_close).pack(side="right")

    @staticmethod
    def _open(path):
        try:
            os.startfile(path)      # Windows
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", path])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="CH",
                    help="频道表目录（含 TER/SAT 子目录）或 .tgz 备份")
    ap.add_argument("--ip", help="预填仪器 IP")
    args = ap.parse_args()

    src = args.source
    if not (os.path.isdir(src) or os.path.isfile(src)):
        here = os.path.dirname(os.path.abspath(__file__))
        alt = os.path.join(here, src)
        if os.path.isdir(alt) or os.path.isfile(alt):
            src = alt
        else:
            print("找不到频道表来源: %s" % src)
            return 2

    root = tk.Tk()
    app = App(root, src)
    if args.ip:
        app.ip_var.set(args.ip)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
