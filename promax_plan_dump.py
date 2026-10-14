# -*- coding: utf-8 -*-
"""
PROMAX 频道表分析工具（离线）
=============================
解析仪器的频道表备份（*.tgz），列出每张表的频道明细。

用途
----
仪器的远程控制协议**无法列出频道表内的频道明细**（36 条命令都没有这个功能），
但频道表 XML 备份里写着完整的频率/制式/频道名。本工具直接从备份解析，
得到"频道表 → 频道 → 频率/制式"的完整清单，用于：
  1. 离线规划测试点（不必守着仪器一个频道一个频道试）
  2. 给 promax_plan_scan.py 提供"按频率直接遍历"的依据
  3. 核对现场频点是否与表一致

用法
----
  python promax_plan_dump.py "D:\\工作文档\\Promax\\Promax数据备份\\CH_20231225_old.tgz"
  python promax_plan_dump.py <tgz> --plan TOPWAY            # 只看某张表
  python promax_plan_dump.py <tgz> --csv topway.csv          # 导出 CSV
  python promax_plan_dump.py <tgz> --all                    # 全部表都导出

XML 结构（实测）
----------------
<CHANNEL-SET band="TERRESTRIAL" name="TOPWAY" ...>
  <HEADER><TV system="PAL" standard="I"/><LNB>undefined</LNB></HEADER>
  <CHANNELS>
    <CHANNEL name="113.25-Z03" frequency="116.00" units="MHz">
      <SIGNAL><ANALOGTER><TV system="PAL" standard="I"/>
               <BANDWIDTH value="8" units="kHz"/></ANALOGTER></SIGNAL>
    </CHANNEL>
    <CHANNEL name="171-DS06" frequency="171.00" units="MHz">
      <SIGNAL><DVB-C /></SIGNAL>
    </CHANNEL>
  </CHANNELS>
</CHANNEL-SET>

⚠️ 实测坑：部分文件名带**尾随空格**（如 `DEMO-C-Band .XML`），
    下发 `*TUNE PLAN=` 时必须原样带上，去空格会找不到。
"""

import argparse
import csv
import os
import re
import sys
import tarfile


def _text(node):
    """取节点文本，空标签返回空串"""
    return (node.text or "").strip() if node is not None else ""


def _dedup_name(name):
    """把"整串重复两遍"的表名折回去。

    真机上有些表的 name 属性坏了（原字符串拼了两遍，例如
    `xxx-KuV` 写成 `xxx-KuVxxx-KuV`）。这种名字进报告很难看，
    这里检测并只保留一份。
    """
    s = str(name or "")
    n = len(s)
    if n >= 4 and n % 2 == 0:
        half = s[:n // 2]
        if half == s[n // 2:]:
            return half
    return s


def parse_plan_xml(data):
    """解析一张频道表 XML → dict

    返回 {'name','band','tv_system','tv_standard','lnb','channels':[...],'raw_len'}
    channel = {'name','freq','freq_mhz','signal','detail','bandwidth'}
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(data)
    raw_name = root.get("name", "")
    out = {
        "name": _dedup_name(raw_name),
        "name_raw": raw_name,
        "band": root.get("band", ""),
        "date": root.get("date", ""),
        "version": root.get("version", ""),
        "tv_system": "",
        "tv_standard": "",
        "lnb": "",
        "channels": [],
        "raw_len": len(data),
    }
    header = root.find("HEADER")
    if header is not None:
        tv = header.find("TV")
        if tv is not None:
            out["tv_system"] = tv.get("system", "")
            out["tv_standard"] = tv.get("standard", "")
        out["lnb"] = _text(header.find("LNB"))

    # 卫星表头的 LNB 本振。**两种标签都要认**：
    #   C 波段：<BAND_C value="5150">
    #   Ku 波段：<BAND_KUKA high="11300" low="11300">
    # 只认 BAND_C 会让 Ku 表的本振取不到 → 频率算错 → 全部 NAK（踩过）。
    # ⚠️ 必须**在频道循环之前**解析：卫星频道要用它把 downlink 换算成中频
    loc = root.find(".//LNB-LOC-OSC")
    lo_val = None
    if loc is not None:
        for el in loc:
            tag = el.tag.upper()
            if not tag.startswith("BAND"):
                continue
            out["lnb_band"] = tag
            out["lnb_high"] = el.get("high")
            out["lnb_low"] = el.get("low")
            v = el.get("value") or el.get("low") or el.get("high") or ""
            u = el.get("units", "")
            if v:
                out["lnb_loc_osc"] = "%s%s" % (v, u)
                lo_val = _num(v)
            break
    lnb_lo = lo_val

    channels = root.find("CHANNELS")
    if channels is None:
        return out

    for ch in channels.findall("CHANNEL"):
        name = ch.get("name", "")
        # ⚠️ 地面表用 frequency，卫星表用 downlink —— 两种属性名都要认
        freq = ch.get("frequency") or ch.get("downlink") or ""
        attr_name = "frequency" if ch.get("frequency") else (
            "downlink" if ch.get("downlink") else "")
        units = ch.get("units", "MHz")
        try:
            mhz = float(freq)
        except (TypeError, ValueError):
            mhz = None
        sig = ch.find("SIGNAL")
        sig_name = ""
        detail = ""
        bw = ""
        symb = None      # 符号率 kSymb/s（DVB-S/S2 必须设对，否则锁不上）
        if sig is not None and len(sig):
            tag = sig[0].tag# ANALOGTER / DVB-C / DVB-S / DVB-S2 ...
            sig_name = tag
            tv = sig[0].find("TV")
            if tv is not None:
                detail = "%s-%s" % (tv.get("system", ""), tv.get("standard", ""))
            bwel = sig[0].find("BANDWIDTH")
            if bwel is not None:
                bw = "%s%s" % (bwel.get("value", ""), bwel.get("units", ""))
            # 卫星表常见 SYMBOLRATE；地面表可能带 SR/CR
            for k in ("SYMBOLRATE", "SR", "CR"):
                e = sig[0].find(k)
                if e is not None:
                    v = e.get("value") or _text(e)
                    u = e.get("units", "")
                    if v:
                        detail = (detail + " %s=%s%s" % (k, v, u)).strip()
                        # 符号率要结构化出来：DVB-S/S2 不设对符号率锁不上
                        if k in ("SYMBOLRATE", "SR") and symb is None:
                            try:
                                symb = float(v)
                            except (TypeError, ValueError):
                                pass
        # 🔴 卫星频道：表里的 downlink 是**下行频率**，但 `*TUNE BAND=SAT FREQ=`
        #    要的是**中频(IF)**，换算关系：IF = LNB本振 − downlink（C 波段）。
        #    真机实测（RANGERNeo 4，本振 5150MHz）：
        #      FREQ=1370000K -> ACK，*?TUNE 回 FREQ=1370000K DL=3780MHz
        #      FREQ=3780000K -> NAK（直接发 downlink 会被拒）
        #    各频点全部吻合：5150-3780=1370 / 5150-3900=1250
        #                     5150-4040=1110 / 5150-4120=1030
        tune_mhz = mhz
        tune_src = "plan"
        if mhz is not None and "SAT" in (out.get("band") or "").upper():
            # 卫星解调参数
            sband = ""
            sat_el = ch.find("SAT")
            if sat_el is not None:
                sband = (sat_el.get("band") or "").upper()
            # 本振选择：Ku 表的 BAND_KUKA 分高低本振，按频道 <SAT band="LOW|HIGH"> 取；
            # 只有单一本振（C 波段）时直接用。
            lo = lnb_lo
            try:
                lo_h, lo_l = _num(out.get("lnb_high")), _num(out.get("lnb_low"))
            except Exception:
                lo_h = lo_l = None
            if lo_l is not None and lo_h is not None and lo_l != lo_h:
                lo = lo_l if sband == "LOW" else lo_h
            if lo:
                # 🔴 中频方向**随波段相反**：
                #   C 波段：IF = 本振 − 下行（5150 − 3780 = 1370）
                #   Ku 波段：IF = 下行 − 本振（12395 − 11300 = 1095）
                # 统一用 abs() 两种情况都对；Ku 表若按 C 的公式算会得到**负中频**，
                # 仪器直接 NAK（实测：某张 Ku 表 5 个频点全 NAK，就是这个原因）。
                tune_mhz, tune_src = abs(mhz - lo), "lnb"
        # 🔴 模拟频道（ANALOGTER）：表里的 frequency 是**频道中心频率**，
        #    但测量必须落在**图像载频**上（PAL-DK 中心比载频高 2.75MHz）。
        #    频道名通常自带图像载频（如 113.25-Z03 / 787.25-DS52），优先用它。
        #    真机实测（RANGERNeo 4）：
        #      Z-3  表值 116.00 → LEVEL 31.1 / CN 0.0   （死点，什么都测不到）
        #           名值 113.25 → LEVEL 90.7 / CN 53.3   （正确）
        #      DS-52 表值 790.00 → LEVEL 27.2 / CN 0.0
        #            名值 787.25 → LEVEL 91.4 / CN 55.4 （正确）
        #    数字频道（DVB）名值与表值一致，不需要校正。
        elif mhz is not None and sig_name.upper().startswith("ANALOG"):
            nm = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", name or "")
            if nm:
                nf = float(nm.group(1))
                if 40.0 <= nf <= 1000.0 and abs(nf - mhz) > 0.01:
                    tune_mhz, tune_src = nf, "name"
        out["channels"].append({
            "name": name, "freq": freq, "freq_mhz": mhz,
            "tune_mhz": tune_mhz, "tune_src": tune_src,
            "attr": attr_name, "symb_rate": symb,
            "signal": sig_name, "detail": detail, "bandwidth": bw,
        })
    return out


def iter_plans(source):
    """遍历来源里的所有频道表 XML。source 可以是 .tgz 或解压后的目录。

    目录结构按仪器导出惯例：<源>/CH/TER/*.XML 与 <源>/CH/SAT/*.XML。
    也接受直接把 TER / SAT 放在源目录下的情况。
    """
    if os.path.isdir(source):
        found = []
        # 先找 CH/TER、CH/SAT 这类标准结构
        for sub in ("TER", "SAT"):
            for base in (os.path.join(source, "CH"), source,
                         os.path.join(source, "CH", "CH")):
                d = os.path.join(base, sub)
                if os.path.isdir(d):
                    found.extend((os.path.join(d, f), f)
                                 for f in sorted(os.listdir(d))
                                 if f.lower().endswith(".xml"))
        if not found:                      # 兜底：递归扫整个目录
            for root, _, files in os.walk(source):
                for f in sorted(files):
                    if f.lower().endswith(".xml"):
                        found.append((os.path.join(root, f), f))
        for path, name in found:
            with open(path, "rb") as f:
                data = f.read()
            yield path, data
        return

    with tarfile.open(source, "r:gz") as t:
        names = [n for n in t.getnames() if n.lower().endswith(".xml")]
        for n in names:
            f = t.extractfile(n)
            if f is None:
                continue
            yield n, f.read()


def load_plans(source):
    """加载并解析全部表，按 TER/SAT 分组"""
    ter, sat = [], []
    for member, data in iter_plans(source):
        try:
            p = parse_plan_xml(data)
        except Exception as e:
            print("  [跳过] %s 解析失败: %s" % (member, e), file=sys.stderr)
            continue
        p["file"] = os.path.basename(member)
        p["member"] = member
        # ⚠️ 保留尾部空格标记，下发命令时要用原名
        p["exact_file"] = p["file"]
        p["has_space"] = p["file"] != p["file"].strip()
        (sat if p["band"].upper().startswith("SAT") else ter).append(p)
    ter.sort(key=lambda x: x["file"])
    sat.sort(key=lambda x: x["file"])
    return ter, sat


def show_plan(p, max_ch=None):
    print("\n" + "=" * 78)
    tag = "地面(TER)" if p["band"].upper().startswith("TER") else "卫星(SAT)"
    print("文件: %s" % p["exact_file"], end="")
    print("   ← 文件名含尾随空格，切表时必须原样带上！" if p["has_space"] else "")
    print("表名: %s   频段: %s (%s)   频道数: %d"
          % (p["name"], p["band"], tag, len(p["channels"])))
    if p["tv_system"]:
        print("电视制式: %s-%s   LNB: %s" % (p["tv_system"], p["tv_standard"],
                                          p["lnb"] or "undefined"))
    if p.get("lnb_loc_osc"):
        print("LNB 本振: %s（Ku 波段标记；XML 里的 downlink 已是可直接使用的"
              "调谐频率，**不要**再减本振）" % p["lnb_loc_osc"])
    chans = p["channels"]
    if max_ch:
        chans = chans[:max_ch]
    if not chans:
        print("  (空表)")
        return
    w = max(len(c["name"]) for c in chans)
    w = max(w, 10)
    print("-" * (w + 40))
    print("%-*s %10s %10s  %-10s %s" % (w, "频道名", "XML频率", "名中频率",
                                       "制式", "备注"))
    print("-" * (w + 40))
    for c in chans:
        fr = "%.3f" % c["freq_mhz"] if c["freq_mhz"] is not None else c["freq"]
        note = c["detail"]
        if c["bandwidth"]:
            note = (note + " BW=" + c["bandwidth"]).strip()
        # 频道名里常带图像载频（如 787.25-DS52），与 XML frequency 属性（声载频）
        # 相差一个载波差（PAL-I/DK 为 2.75MHz）。两者含义不同，别混用。
        named = ""
        m = re.match(r"^\s*([0-9]+\.[0-9]+)", c["name"])
        if m and c["freq_mhz"] is not None:
            nv = float(m.group(1))
            named = "%.3f" % nv
            if abs(nv - c["freq_mhz"]) > 0.01:
                named += "*"
        print("%-*s %10s %10s  %-10s %s"
              % (w, c["name"], fr, named or "-", c["signal"], note))
    print("-" * (w + 40))
    if max_ch and len(p["channels"]) > max_ch:
        print("... 共 %d 个频道，仅显示前 %d 个" % (len(p["channels"]), max_ch))
    # 如存在名中频率与属性不一致，明确提示差异含义
    diffs = []
    for c in p["channels"]:
        m = re.match(r"^\s*([0-9]+\.[0-9]+)", c["name"])
        if m and c["freq_mhz"] is not None:
            d = c["freq_mhz"] - float(m.group(1))
            if abs(d) > 0.01:
                diffs.append(d)
    if diffs:
        print("注：带 * 的行表示频道名里的频率与 XML frequency 属性不一致。")
        print("    模拟频道：名中通常是**图像载频**（如 787.25），")
        print("    XML 属性是**声载频/实际调谐频率**（相差载波差，PAL-I/DK 为 2.75MHz）。")
        print("    实测本表共有 %d 个频点存在此差异，差值范围 %.2f~%.2f MHz。"
              % (len(diffs), min(diffs), max(diffs)))


def export_csv(path, plans):
    rows = []
    for p in plans:
        loc = _lnb_loc(p)
        for c in p["channels"]:
            rows.append({
                "file": p["exact_file"], "plan": p["name"], "band": p["band"],
                "ch": c["name"], "dl_mhz": c["freq_mhz"],
                "lnb_loc_mhz": loc if loc else "",
                "tune_mhz": c.get("tune_mhz",
                                  ("%.3f" % (loc - c["freq_mhz"]))
                                  if (loc and c["freq_mhz"] is not None) else ""),
                "signal": c["signal"], "detail": c["detail"],
                "bandwidth": c["bandwidth"],
            })
    cols = ["file", "plan", "band", "ch", "dl_mhz", "lnb_loc_mhz",
            "tune_mhz", "signal", "detail", "bandwidth"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("已导出 %d 个频道 -> %s" % (len(rows), path))


def summarize(ter, sat):
    print("=" * 78)
    print("频道表总览")
    print("=" * 78)
    for tag, group in (("地面 TER（射频测试用）", ter), ("卫星 SAT（卫星天线测试用）", sat)):
        total = sum(len(p["channels"]) for p in group)
        print("\n%s: %d 张表，合计 %d 个频道"
              % (tag, len(group), total))
        print("  " + "  ".join("%s(%d)" % (p["file"], len(p["channels"]))
                                for p in group))
        # 制式分布
        sig = {}
        for p in group:
            for c in p["channels"]:
                sig[c["signal"]] = sig.get(c["signal"], 0) + 1
        if sig:
            print("  制式分布: " + "  ".join("%s=%d" % (k, v)
                                             for k, v in sorted(sig.items())))


def _num(v):
    """从 "11300MHz" / "5150" 这类字符串里取出数值"""
    if v is None:
        return None
    try:
        return float("".join(c for c in str(v)
                             if c.isdigit() or c in ".-"))
    except ValueError:
        return None


def _lnb_loc(p):
    """取LNB 本振（MHz），卫星表有、地面表没有"""
    v = p.get("lnb_loc_osc") or ""
    if not v:
        return None
    return _num(v)


def to_freq_list(plan, apply_lnb=False):
    """把一张表转成 [(调谐频率MHz, 制式, 频道名)]，供 promax_plan_scan 按频率遍历

    ⚠️ 卫星表**不需要减 LNB 本振**（apply_lnb 默认 False）。
    实测验证：某张 C 波段表的 downlink 是 3780 / 4040，
    与真机 `*?TUNE CH` 返回的 `PLAN=<该表> CH=4040` 完全一致，
    且真机 `*?TUNE` 同时给出 `FREQ=1370000K DL=3780MHz` ——
    说明 XML 里的 downlink 已经是**仪器可直接使用的调谐频率（中频/IF）**，
    本振换算由仪器内部完成，PC 侧不要再减，否则会设错频率。
    `apply_lnb=True` 仅供导出参考值时使用。
    """
    loc = _lnb_loc(plan) if apply_lnb else None
    out = []
    for c in plan["channels"]:
        if c["freq_mhz"] is None:
            continue
        f = c["freq_mhz"] - loc if loc else c["freq_mhz"]
        out.append((f, c["signal"], c["name"]))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="解析 PROMAX 频道表备份（离线分析）",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n"
               "  python promax_plan_dump.py backup.tgz\n"
               "  python promax_plan_dump.py backup.tgz --plan TOPWAY --csv out.csv\n"
               "  python promax_plan_dump.py backup.tgz --all\n")
    ap.add_argument("source", help="频道表备份 .tgz 或解压后的目录（含 CH/TER、CH/SAT）")
    ap.add_argument("--plan", help="只显示指定表（按表名或文件名子串匹配）")
    ap.add_argument("--band", choices=["ter", "sat"], help="只显示某频段的表")
    ap.add_argument("--max", type=int, default=0, help="每张表最多显示多少频道（0=全部）")
    ap.add_argument("--csv", help="导出 CSV")
    ap.add_argument("--all", action="store_true", help="CSV 导出全部表")
    ap.add_argument("--list-only", action="store_true", help="只列文件清单，不解析")
    args = ap.parse_args()

    if not (os.path.isfile(args.source) or os.path.isdir(args.source)):
        print("找不到: %s" % args.source)
        return 2

    print("读取: %s" % args.source)
    if args.list_only:
        with tarfile.open(args.source, "r:gz") as t:
            names = t.getnames()
            xmls = [n for n in names if n.lower().endswith(".xml")]
            print("共 %d 项，其中 XML %d 个：" % (len(names), len(xmls)))
            for n in xmls:
                print("  %s" % n)
        return 0

    try:
        ter, sat = load_plans(args.source)
    except Exception as e:
        print("解析失败: %s" % e)
        return 3
    if not ter and not sat:
        print("未找到任何频道表 XML")
        return 3

    summarize(ter, sat)

    if args.band:
        groups = [ter] if args.band == "ter" else [sat]
    else:
        groups = [ter, sat]

    sel = []
    for g in groups:
        for p in g:
            if args.plan:
                key = args.plan.lower()
                if key not in p["name"].lower() and key not in p["file"].lower():
                    continue
            sel.append(p)
            show_plan(p, args.max or None)

    if args.csv:
        export_csv(args.csv, sel if (args.plan or args.band) else ter + sat)
    return 0


if __name__ == "__main__":
    sys.exit(main())