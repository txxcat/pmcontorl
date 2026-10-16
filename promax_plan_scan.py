# -*- coding: utf-8 -*-
"""
PROMAX 频道表遍历测试程序
=========================
依据：《Remote control commands for HD RANGER & RANGER Neo family》v2.2 (2017-02)
      +《HD RANGER / RANGER Neo 用户手册》(ranger-neo-4.pdf)

背景与协议要点
--------------
手册 36 条远程命令中**没有任何一条能列出频道表内的频道明细**（频道号/频率/台名）。
能读到的只有：
  *?INSTALLATION              → 当前安装包的汇总，含 TER-CH=nn / SAT-CH=nn 数量
  *?INSTALLATION CURRENT TER-CH[=nn] → 取第 nn 张地面频道表的**名称**
所以遍历只能靠仪器自己"走"：
  *TUNE MODE=CH               → 切成按频道调谐
  *TUNE PLAN=plan_conf        → 切换频道表
  *TUNE CH NEXT               → 跳到下一个频道
  *?TUNE CH                   → 读当前频道（PLAN=xxx CH=xxx）
反复 NEXT 直到读到的频道号不再变化（回到起点）即完成一圈。

用法
----
  # 1) 先列出所有频道表
  python promax_plan_scan.py --host 192.168.1.50 --list

  # 2) 遍历第2 张地面频道表，测 LEVEL + C/N
  python promax_plan_scan.py --host 192.168.1.50 --scan ter 2

  # 3) 遍历卫星频道表第 1 张（自动开 LNB）
  python promax_plan_scan.py --host 192.168.1.50 --scan sat 1

  # 4) 指定测量项 / 保存 CSV
  python promax_plan_scan.py --host 192.168.1.50 --scan ter 2 --items LEVEL,C/N --csv out.csv

依赖：仅标准库，连接层复用 promax_telnet_test.PromaxTelnet
"""

import argparse
import csv
import os
import re
import sys
import time

from promax_telnet_test import (PromaxTelnet, Promax, PromaxError,
                                parse_measure, parse_measure_all,
                                parse_install_current, parse_tune_ch)


# --------------------------------------------------------------- 按频率遍历
def scan_by_frequency(conn, inst, band, points, analog=True, lnb_output=0,
                      settle=0.6, raw_print=False):
    """按给定频点列表逐个调谐并测量（不依赖 TUNE CH NEXT）

    points: [(freq_mhz, signal, ch_name), ...]  由 promax_plan_dump 解析得到

    相比"逐 CH NEXT"的优势：
      1. 频率与频道名直接来自 XML，可离线核对，不必靠仪器一步步走
      2. 每个频点可单独指定制式（模拟表/数字表混排也能正确设 SIGNAL TYPE）
      3. 仪器若不支持某表，也不会静默测错频段
    """
    render = conn.render_raw
    print("\n" + "=" * 72)
    print("按频率遍历：%d 个频点  频段=%s" % (len(points), band))
    print("=" * 72)

    for line in inst.apply_profile(band, analog=analog, lnb_output=lnb_output,
                                   raw_print=raw_print):
        print(line)
    inst.tune_mode("FREQ")

    results = []
    cur_sig = None
    for i, (mhz, sig, chname) in enumerate(points, 1):
        # 逐频点按其制式设置 SIGNAL TYPE（同一张表可能混排模拟/数字）
        want = "ANALOG" if str(sig).upper().startswith("ANALOG") else (
            "DVB-C" if band == "TER" else "DVB-S2")
        if want != cur_sig:
            inst.signal_type(want)
            if want == "ANALOG":
                inst.signal_standard("PAL")
            cur_sig = want
            time.sleep(0.3)      # 等制式切换生效

        khz = int(round(mhz * 1000))
        r = inst.set_tune(band, "%dK" % khz)
        if not r["ack"]:
            print("  [%3d/%d] %-12s %8.3f MHz  设置 NAK，跳过"
                  % (i, len(points), chname, mhz))
            continue
        if settle > 0:
            time.sleep(settle)

        rr = conn.command("?MEASURE")
        if raw_print:
            print("    TX %s" % render(rr["sent"]))
            print("    RX %s" % render(rr.get("raw", b"")))
        allv = parse_measure_all(" ".join(rr["lines"])) if rr["lines"] else {}

        band_now, plan_now, ch_now = parse_tune_ch(inst.tune_ch())
        lock = allv.get("LOCKED", (None, ""))
        lock = str(lock[1] or lock[0] or "")

        use_items = measure_items(band, want == "ANALOG")
        KEYMAP = {"C/N": "CN", "LEVEL": "POWER"}
        row = {"idx": i, "band": band_now or band, "plan": plan_now or "",
               "ch": chname, "freq_mhz": "%.3f" % mhz, "signal": sig,
               "LOCKED": lock}
        for item in use_items:
            key = KEYMAP.get(item, item)
            val, unit = allv.get(key, (None, ""))
            row[item] = "" if val is None else ("%.1f" % val)
            row[item + "_unit"] = unit or ""
        if band == "SAT":
            ov, ou = allv.get("OFFSET", (None, ""))
            row["OFFSET"] = "" if ov is None else ("%.0f" % ov)
            row["OFFSET_unit"] = ou or "Hz"
        results.append(row)

        print("  [%3d/%d] %-12s %8.3f MHz  LOCK=%-4s %s%s"
              % (i, len(points), chname, mhz, lock or "-",
                 "  ".join("%s=%s%s" % (k, row.get(k, "-"), row.get(k + "_unit", ""))
                           for k in use_items),
                 ("  OFFSET=%sHz" % row["OFFSET"]) if row.get("OFFSET") else ""))
    return results


# --------------------------------------------------------------- 测量项口径
def measure_items(band, analog=True):
    """按频段/制式选择测量项（与 promax_telnet_test 保持一致）

    TER 模拟 ANALOG : LEVEL + C/N
    TER 数字 DVB-C  : POWER + MER
    SAT 卫星        : POWER + C/N + MER
    """
    if band == "TER":
        return ["LEVEL", "C/N"] if analog else ["POWER", "MER"]
    return ["POWER", "C/N", "MER"]


# --------------------------------------------------------------- 解析工具
def parse_installation(lines):
    """解析 *?INSTALLATION 的多行响应

    手册未给完整示例，实际返回形如多行 *INSTALLATION ... 或单行键值对。
    这里做宽松解析：把整个响应拼成文本后逐键提取。
    """
    text = " ".join(lines)
    out = {}
    for key in ("NUMBER", "NAME", "TER-CH", "SAT-CH", "DISEQC",
                "SCREEN", "DATALOGGER", "SP", "MER", "ECHOES",
                "CONSTELLATION"):
        m = re.search(r"%s\s*=\s*([^\s]+)" % re.escape(key), text, re.I)
        if m:
            out[key] = m.group(1)
    return text, out


def parse_plan_name(lines):
    """*INSTALLATION NAME=xxxx → 名称"""
    text = " ".join(lines)
    m = re.search(r"NAME\s*=\s*(.+)$", text, re.I)
    return m.group(1).strip() if m else None


def parse_tune_ch(lines):
    """*TUNE BAND=xx PLAN=xxx CH=xxx → (band, plan, ch)"""
    text = " ".join(lines)
    b = re.search(r"BAND\s*=\s*([A-Z]+)", text, re.I)
    p = re.search(r"PLAN\s*=\s*([A-Za-z0-9_]+)", text, re.I)
    c = re.search(r"CH\s*=\s*([A-Za-z0-9_\-]+)", text, re.I)
    return (b.group(1).upper() if b else None,
            p.group(1) if p else None,
            c.group(1) if c else None)


# --------------------------------------------------------------- 读取频道表列表
def list_plans(conn, inst, raw_print=False):
    """读当前安装包信息与频道表数量。

    真机实测（RANGERNeo 4 / 固件 31.6）关键差异：
      * `*?INSTALLATION` 只返回 `NUMBER=nn`（安装包数量），**不含 TER-CH/SAT-CH**
      * 必须用 `*?INSTALLATION CURRENT`（**不带参数**）才能拿到全部字段：
        `*INSTALLATION NAME=<安装名> TER-CH=5 SAT-CH=18 ...`
      * 手册写的 `*?INSTALLATION CURRENT TER-CH[=nn]` 在真机上返回 NAK

    因此：拿得到的是"有多少张表"，**拿不到每张表的名字**——
    真正的表名要从 `*?TUNE CH` 的 PLAN 字段（XML 文件名）逐个 NEXT 出来。
    """
    cur = inst.installation_current()
    if raw_print:
        for ln in cur:
            print("    %s" % ln)
    info = parse_install_current(cur)
    if not info:
        raise PromaxError("*?INSTALLATION CURRENT 解析失败")

    n_ter = int(info.get("TER-CH", "0") or 0)
    n_sat = int(info.get("SAT-CH", "0") or 0)

    print("安装包: %s" % info.get("NAME", "(未知)"))
    print("  地面频道表TER-CH = %d 张" % n_ter)
    print("  卫星频道表 SAT-CH = %d 张" % n_sat)
    print("  DISEQC=%s  PNG=%s  DATALOGGER=%s  SP=%s  MER=%s"
          % (info.get("DISEQC_PROGRAM", "-"), info.get("PNG", "-"),
             info.get("DATALOGGER", "-"), info.get("SP", "-"),
             info.get("MER", "-")))

    plans = []
    for band, n in (("TER", n_ter), ("SAT", n_sat)):
        for i in range(1, n + 1):
            plans.append({"band": band, "index": i, "name": None})

    if plans:
        print("\n共 %d 张频道表。序号即 *?INSTALLATION CURRENT 报告的排列顺序；"
              % len(plans))
        print("表名需遍历时才逐个读出（见 --scan），因为仪器不提供表名列表接口。")
    else:
        print("\n未解析到频道表数量。")
    return info, plans


# --------------------------------------------------------------- 连通性诊断
def diagnose(conn, inst):
    """逐条探测仪器是否响应远程命令。

    实测教训：仪器**只允许一个 telnet 会话**，若上一会话未干净断开
    （仪器不回 FIN，socket 卡在 FIN_WAIT_2），新连接会「连得上但收不到任何回复」——
    连 ACK 都没有。此时 ping 通、端口可连，但命令全部无响应。
    """
    print("逐条探测（每条最多 %.0fs）:" % conn.timeout)
    probes = [("*?VER", "版本"), ("*?NAM", "设备名"),
              ("*?INSTALLATION CURRENT", "安装包/频道表数量"),
              ("*?TUNE", "当前调谐"), ("*?TUNE CH", "当前频道"),
              ("*?MEASURE", "全部测量"), ("*?UNITS", "单位"),
              ("*?SIGNAL", "信号参数")]
    ok = bad = 0
    for cmd, desc in probes:
        t0 = time.time()
        try:
            lines = inst.c.query(cmd[1:], timeout=min(10.0, conn.timeout))
            dt = time.time() - t0
            if lines:
                ok += 1
                print("  [ OK ] %-26s %5.1fs  %s" % (cmd, dt, lines[0][:70]))
            else:
                bad += 1
                print("  [无响] %-26s %5.1fs" % (cmd, dt))
        except PromaxError as e:
            bad += 1
            print("  [异常] %-26s  %s" % (cmd, e))
    print("\n响应 %d 条 / 无响应 %d 条" % (ok, bad))
    if bad == len(probes):
        print("""
所有命令均无响应。TCP 端口可连但收不到 ACK，说明仪器当前不接受远程命令。
常见原因（按可能性排序）：
  1. 上一 telnet 会话未干净断开，仪器仍认为会话占用 → 等 1~2 分钟再试
  2. 仪器正处于测量/调谐执行中，忙状态不响应 → 等待当前操作结束
  3. 仪器前面板正处于某个需要确认的界面/菜单 → 回到主界面
  4. 仪器 telnet 服务被关闭 → 在 Preferences → Network 里重新开启
建议同时用浏览器打开 http://<IP>/ 看仪器是否活着、界面是否正常。""")


# --------------------------------------------------------------- 遍历测试
def scan_plan(conn, inst, band, index, items=None, analog=True,
              lnb_output=0, settle=0.6, max_channels=400,
              wrap_guard=2, raw_print=False, csv_path=None, plan_name=None,
              args_allow_mismatch=False):
    """遍历指定频道表的全部频道并逐个测量。

    band : 'TER' | 'SAT'
    index: 第几张频道表（从 1 开始）
    items : 测量项列表，None 则按频段自动
    plan_name: 该表的名称。*TUNE PLAN 手册定义为"频道表的字母数字代码"，
               实践上就是表名（如 CHINA），因此优先用名称切；名称不可得时回退序号。
    """
    render = conn.render_raw
    tag = "%s-CH=%d" % (band, index)

    # 1) 切频道表 + 切按频道调谐
    print("\n" + "=" * 72)
    print("遍历频道表 %s%s" % (tag, (" (%s)" % plan_name) if plan_name else ""))
    print("=" * 72)

    # 切频道表。
    # 真机实测：PLAN 是**频道表 XML 文件名**（如 DEMO-C-Band.XML），
    # 手册说的"简短代码"和序号都无效。
    # 策略：优先用 --plan 传入的完整文件名；若只给了前缀（如 DEMO-C），
    #      先读当前 PLAN 确认完整名，再尝试按前缀匹配。
    full_plan = plan_name
    if plan_name:
        r_plan = conn.command("TUNE PLAN=%s" % plan_name, want_response=False)
        if raw_print:
            print("TX %s" % render(r_plan["sent"]))
            print("RX %s" % render(r_plan.get("raw", b"")))
        if not r_plan["ack"] and "." not in plan_name:
            # 只给了前缀，尝试补全：当前 PLAN 若同前缀则直接用
            cur = parse_tune_ch(inst.tune_ch())
            if cur[1] and cur[1].upper().startswith(plan_name.upper()):
                full_plan = cur[1]
                print("  [提示] PLAN 需完整XML 文件名，改用当前 PLAN=%s"
                      % full_plan)
                r_plan = conn.command("TUNE PLAN=%s" % full_plan,
                                      want_response=False)
                if raw_print:
                    print("TX %s" % render(r_plan["sent"]))
                    print("RX %s" % render(r_plan.get("raw", b"")))
        if not r_plan["ack"]:
            print("  [警告] *TUNE PLAN=%s 未获 ACK，将沿用仪器当前频道表"
                  % plan_name)
    else:
        print("  [提示] 仪器不提供表名列表接口，沿用当前 PLAN 逐 NEXT 遍历")
        r_plan = {"ack": True, "sent": b"", "raw": b""}

    r_mode = conn.command("TUNE MODE=CH", want_response=False)
    if raw_print:
        print("TX %s" % render(r_mode["sent"]))
        print("RX %s" % render(r_mode.get("raw", b"")))

    # 2) 频段配置（与主程序一致：射频 ANALOG+PAL / 卫星 DVB-S2+LNB）
    for line in inst.apply_profile(band, analog=analog, lnb_output=lnb_output,
                                   raw_print=raw_print):
        print(line)

    # 3) 读第一个频道
    first = inst.tune_ch()
    b0, plan0, ch0 = parse_tune_ch(first)
    print("起始频道: BAND=%s PLAN=%s CH=%s" % (b0, plan0, ch0))
    if not ch0:
        print("  [失败] *?TUNE CH 无有效频道号，无法遍历")
        return None, []
    # 校验实际生效的频道表与频段，避免"要测地面却测了卫星"这种静默错误
    if full_plan and plan0 and plan0.upper() != full_plan.upper():
        print("  [警告] 请求切换到 %s，但仪器报告当前是 %s"
              % (full_plan, plan0))
    if b0 and b0 != band:
        print("  [严重] 目标频段是 %s，但仪器当前 BAND=%s —— "
              "结果不是你要的频段！\n         请用 --plan 指定该频段的频道表"
              "（地面表/卫星表是不同的 XML 文件）。" % (band, b0))
        if not args_allow_mismatch:
            print("         已中止遍历，避免产出错误数据。")
            return None, []

    use_items = items or measure_items(band, analog)
    # 显示名 → 真机响应里的实际键名
    KEYMAP = {"C/N": "CN", "LEVEL": "POWER"}
    print("测量项: %s   （真机为 *?MEASURE 一次全量返回后取键）" % ", ".join(use_items))

    results = []
    seen = []
    ch_now = ch0
    no_progress = 0
    i = 0

    while i < max_channels:
        # 读当前频道
        b_cur, plan_cur, ch_cur = parse_tune_ch(inst.tune_ch())
        ch_cur = ch_cur or ch_now

        # ---- 终止判定放在测量之前，避免把"回到起点"那一项重复计入结果 ----
        wrapped = len(seen) > 0 and ch_cur == seen[0]
        if wrapped:
            print("\n已回到起始频道 %s，遍历结束（%d 个频道）"
                  % (seen[0], len(seen)))
            break

        # 测量：一次全量读取，再按需取键
        row = {"band": b_cur or band, "plan": plan_cur or str(index),
               "ch": ch_cur, "idx": i + 1}
        if settle > 0:
            time.sleep(settle)
        rr = conn.command("?MEASURE")
        text = " ".join(rr["lines"])
        if raw_print:
            print("    TX %s" % render(rr["sent"]))
            print("    RX %s" % render(rr.get("raw", b"")))
        allv = parse_measure_all(text) if text else {}
        row["LOCKED"] = str(allv.get("LOCKED", ("", ""))[1] or
                            allv.get("LOCKED", ("", ""))[0] or "")
        for item in use_items:
            key = KEYMAP.get(item, item)
            val, unit = allv.get(key, (None, ""))
            row[item] = "" if val is None else ("%.1f" % val)
            row[item + "_unit"] = unit or ""
        if band == "SAT":
            ov, ou = allv.get("OFFSET", (None, ""))
            row["OFFSET"] = "" if ov is None else ("%.0f" % ov)
            row["OFFSET_unit"] = ou or "Hz"

        results.append(row)
        seen.append(ch_cur)
        i += 1
        extra = ""
        if band == "SAT" and row["OFFSET"]:
            extra = "  OFFSET=%s%s" % (row["OFFSET"], row["OFFSET_unit"])
        print("  [%3d] %-10s %s%s" % (
            i, ch_cur,
            "  ".join("%s=%s%s" % (k, row.get(k, "-"), row.get(k + "_unit", ""))
                      for k in use_items),
            extra))

        # 频道号连续不变且不是第一个 → 判定无法前进
        if len(seen) >= 2 and seen[-1] == seen[-2]:
            no_progress += 1
            if no_progress >= wrap_guard:
                print("\n频道号连续 %d 次不变，判定无法继续前进，停止" % no_progress)
                break
        else:
            no_progress = 0

        # NEXT
        #注意：真机上切换 SIGNAL TYPE 可能重置调谐，NEXT 之后频道号可能不按预期前进。
        # 因此这里做双重防护：NAK 立即停止；频道号反复不前进也停止。
        rn = conn.command("TUNE CH NEXT", want_response=False)
        if raw_print:
            print("    TX %s" % render(rn["sent"]))
            print("    RX %s" % render(rn.get("raw", b"")))
        if not rn["ack"]:
            print("\n[TUNE CH NEXT 返回 NAK，提前结束]")
            break
        ch_now = ch_cur
    else:
        print("\n达到上限 %d 个频道，停止遍历" % max_channels)

    return (b0, plan0, ch0), results


# --------------------------------------------------------------- 打印 / 保存
def print_table(results, items, freq_col=False):
    if not results:
        print("\n无结果")
        return
    has_lock = any(r.get("LOCKED") for r in results)
    has_off = any(r.get("OFFSET") for r in results)
    w = max(len(str(r["ch"])) for r in results)
    w = max(w, 10)
    head = ""
    if freq_col:
        head += "%10s  " % "MHz"
    head += "%-*s" % (w, "频道")
    if has_lock:
        head += "  %-6s" % "LOCK"
    for i in items:
        head += "  %-12s" % i
    if has_off:
        head += "  %-14s" % "OFFSET"
    print("\n" + "-" * len(head))
    print(head)
    print("-" * len(head))
    for r in results:
        line = ""
        if freq_col:
            line += "%10s  " % r.get("freq_mhz", "-")
        line += "%-*s" % (w, r["ch"])
        if has_lock:
            line += "  %-6s" % (r.get("LOCKED") or "-")
        for k in items:
            u = r.get(k + "_unit", "")
            line += "  %-12s" % ("%s%s" % (r.get(k, "-"), u) if r.get(k) else "-")
        if has_off:
            line += "  %-14s" % ("%s%s" % (r.get("OFFSET"), r.get("OFFSET_unit", "Hz"))
                                  if r.get("OFFSET") else "-")
        print(line)
    print("-" * len(head))
    print("合计 %d 个频点" % len(results))


def save_csv(path, results, items):
    cols = ["idx", "band", "plan", "ch", "freq_mhz", "signal", "LOCKED"] + \
           list(items) + [i + "_unit" for i in items]
    if any(r.get("OFFSET") for r in results):
        cols += ["OFFSET", "OFFSET_unit"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)
    print("\n已保存: %s" % path)


# --------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(
        description="PROMAX 频道表遍历测试程序",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n"
               "  python promax_plan_scan.py --host 192.168.1.50 --list\n"
               "  python promax_plan_scan.py --host 192.168.1.50 --scan ter 2\n"
               "  python promax_plan_scan.py --host 192.168.1.50 --scan sat 1 --csv out.csv\n")
    ap.add_argument("--host", "-H", required=True, help="仪器 IP")
    ap.add_argument("--port", "-p", type=int, default=2222, help="Telnet 端口（默认 2222）")
    ap.add_argument("--timeout", "-t", type=float, default=20.0,
                    help="应答超时秒（默认 20；真机响应慢，不宜过小）")
    ap.add_argument("--list", "-l", action="store_true", help="列出所有频道表后退出")
    ap.add_argument("--diag", "-D", action="store_true",
                    help="连通性诊断：逐条探测仪器是否响应远程命令")
    ap.add_argument("--scan", "-s", choices=["ter", "sat"],
                    help="遍历指定频段的频道表，配合位置参数给出第几张")
    ap.add_argument("index", nargs="?", type=int, help="频道表序号（从 1 开始）")
    ap.add_argument("--items", help="自定义测量项，逗号分隔，如 LEVEL,C/N")
    ap.add_argument("--plan", help="频道表 XML 文件名，如 DEMO-C-Band.XML"
                                "（也可只给前缀 DEMO-C）")
    ap.add_argument("--from-xml", help="频道表备份 .tgz：直接按表内频点遍历，"
                                       "不依赖仪器 CH NEXT")
    ap.add_argument("--xml-plan", help="配合 --from-xml：表名或文件名子串，如 TOPWAY")
    ap.add_argument("--xml-max", type=int, default=0, help="--from-xml 时最多取多少频点")
    ap.add_argument("--allow-mismatch", action="store_true",
                    help="频段与目标不符时仍继续（默认中止，避免产出错误数据）")
    ap.add_argument("--dvb-c", dest="analog", action="store_false", default=True,
                    help="射频按数字制式（默认 ANALOG）")
    ap.add_argument("--lnb-output", default="0", help="卫星 LNB OUTPUT（默认 0）")
    ap.add_argument("--settle", type=float, default=0.6, help="每频道稳定等待秒")
    ap.add_argument("--max", type=int, default=400, help="最大频道数保护（默认 400）")
    ap.add_argument("--csv", help="把结果保存为 CSV")
    ap.add_argument("--raw-print", action="store_true", help="打印原始收发字节")
    args = ap.parse_args()

    if not (args.list or args.scan or args.diag):
        ap.error("需要 --list / --scan ter|sat <序号> / --diag / --scan + --from-xml")
    if args.scan and not args.index and not args.from_xml:
        ap.error("--scan 需要给出频道表序号（从 1 开始）；"
                 "或用 --scan ter --from-xml <备份.tgz> 直接按离线频点遍历")

    items = None
    if args.items:
        items = [s.strip() for s in args.items.split(",") if s.strip()]

    print("连接 %s:%d ..." % (args.host, args.port))
    conn = PromaxTelnet(args.host, args.port, timeout=args.timeout,
                        verbose=False, fast=False)
    # 不用 fast 模式：真机单条命令要数秒、*?MEASURE 响应 200+ 字节，
    # fast 的窄窗口会读不全响应导致遍历卡死。提速靠"一次全量测量取多键"。

    # 仪器多半只允许一个 telnet 会话，被占用时会"连得上但收不到任何回复"。
    # 故带重试：首条命令无响应则重连一次。
    for attempt in (1, 2):
        try:
            conn.connect()
            break
        except PromaxError as e:
            if attempt == 2:
                print("连接失败: %s" % e)
                return 2
            time.sleep(2)

    inst = Promax(conn)

    try:
        v = inst.ver()
        if not v:
            print("  [重试] 首条命令无响应，重连一次（仪器可能仍被上一会话占用）")
            conn.close()
            time.sleep(3)
            try:
                conn.connect()
            except PromaxError as e:
                print("重连失败: %s" % e)
                return 2
            inst = Promax(conn)
            v = inst.ver()
        print("仪器: %s   版本: %s" % (v[0] if v else "(无响应)", v[0] if v else "?"))

        if args.diag:
            diagnose(conn, inst)
            return 0

        # ---- 离线频点模式：不等仪器，直接按 XML 里的频点遍历 ----
        if args.from_xml:
            from promax_plan_dump import load_plans, to_freq_list
            if not os.path.isfile(args.from_xml):
                print("找不到备份文件: %s" % args.from_xml)
                return 2
            ter, sat = load_plans(args.from_xml)
            band = (args.scan or "ter").upper()
            group = ter if band == "TER" else sat
            if not group:
                print("备份里没有 %s 表" % band)
                return 3
            if args.xml_plan:
                key = args.xml_plan.lower()
                group = [p for p in group
                         if key in p["name"].lower() or key in p["file"].lower()]
                if not group:
                    print("没有匹配的表: %s" % args.xml_plan)
                    return 3
            print("\n使用离线频点：%s，共 %d 张表"
                  % (", ".join(p["exact_file"] for p in group), len(group)))
            points = []
            for p in group:
                for f, sig, name in to_freq_list(p):
                    points.append((f, sig, "%s/%s" % (p["name"], name)))
            if args.xml_max:
                points = points[:args.xml_max]
            results = scan_by_frequency(conn, inst, band, points,
                                        analog=args.analog,
                                        lnb_output=args.lnb_output,
                                        settle=args.settle,
                                        raw_print=args.raw_print)
            # 表格列必须跟实际读到的项一致：模拟读 LEVEL/CN，数字读 POWER/MER。
            # （--analog 只对"逐CH 模式"生效；离线模式下制式由每个频点自带的
            #   signal 决定，故按该表实际制式推断显示列）
            sigs = {str(s).upper() for _, s, _ in points}
            all_analog = bool(sigs) and all(s.startswith("ANALOG") for s in sigs)
            cols = (["LEVEL", "C/N"] if all_analog
                    else ["POWER", "MER"] if band == "TER"
                    else ["POWER", "C/N", "MER"])
            print_table(results, cols, freq_col=True)
            if args.csv and results:
                save_csv(args.csv, results, cols)
            return 0

        info, plans = list_plans(conn, inst, args.raw_print)

        if args.list:
            print("\n共 %d 张频道表。遍历用 --scan ter|sat <序号>，"
                  "必要时用 --plan <XML文件名> 指定具体表。" % len(plans))
            return 0

        band = args.scan.upper()
        # 表名只能从 *?TUNE CH 的 PLAN 字段读到；仪器不提供表名列表接口，
        # 因此默认沿用仪器当前选中的表，用 --plan 显式切换。
        plan_name = args.plan
        if not plan_name:
            cur = parse_tune_ch(inst.tune_ch())
            plan_name = cur[1]
            print("\n[提示] 未指定 --plan，沿用仪器当前 PLAN=%s" % plan_name)
        elif not any(p["band"] == band and p["index"] == args.index
                     for p in plans):
            print("\n注意: 列表里没有 %s 第 %d 张，仍按 --plan 指定尝试遍历。"
                  % (band, args.index))

        _, results = scan_plan(conn, inst, band, args.index,
                               items=items, analog=args.analog,
                               lnb_output=args.lnb_output, settle=args.settle,
                               max_channels=args.max, raw_print=args.raw_print,
                               plan_name=plan_name,
                               args_allow_mismatch=args.allow_mismatch)
        use_items = items or measure_items(band, args.analog)
        print_table(results, use_items)
        if args.csv and results:
            save_csv(args.csv, results, use_items)
        return 0
    except PromaxError as e:
        print("\n通讯异常: %s" % e)
        return 3
    except KeyboardInterrupt:
        print("\n已被用户中断。")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())