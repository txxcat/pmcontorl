# -*- coding: utf-8 -*-
"""Promax 格式导出 / 转换测试。

覆盖：
  1. 写出的 XML 结构（POINT / CHANNEL-SET / CHANNEL / MEASUREMENTS / MEASURES / STATUS）
  2. 文件本身：utf-8 无 BOM、CRLF、Tab 缩进、双引号属性（与真机导出一致）
  3. 数值口径：频率 2 位小数、POWER/CN/MER/OFFSET 1 位、OFFSET 由 Hz 转 kHz
  4. 卫星：写 downlink（不是 frequency）+ sat_band，中频→下行（频道表优先、本振兜底）
  5. 制式块：模拟 → UNKNOWN；DVB-C / DVB-S / DVB-S2 各自独立
  6. 特殊字符转义、空数据报错
  7. 读回 xlsx / csv 再转换（模拟"事后转换"），列裁剪后也能转
  8. GUI：设置开关打开后跑一轮，结果旁边自动出现 .XML；
     「转换成Promax格式文件」按钮（文件对话框用桩代替）

用法：python _test_promax.py     （集成段需要 2222 空闲，自行起模拟器）
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                    # noqa: E402
import promax_plan_gui as gui                            # noqa: E402
import promax_plan_write as W                            # noqa: E402

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
    "settle_ter": 0.1, "settle_sat": 0.5, "retry_ter": 1, "retry_sat": 1,
    "retry_step_ter": 0.1, "retry_step_sat": 0.5,
    "pause_between": False, "auto_reconnect": True, "reconnect_interval": 10.0,
    "export_format": "xlsx", "export_promax": True,
    "log_dir": "", "log_name": "promax_test.log",
    "log_timestamps": True, "log_timing": True, "log_cmd_timing": False,
    "log_separate": False, "presets": [],
}
gui.load_config = lambda *a, **k: dict(DEMO)


class Dialogs(object):
    """messagebox / filedialog 桩（模态框会卡住测试）"""

    def __init__(self):
        self.calls = []
        self.answer = True
        self.pick_file = ""

    def _rec(self, kind, title, msg=""):
        self.calls.append((kind, title, msg))

    def askyesno(self, title, msg="", **k):
        self._rec("askyesno", title, msg)
        return self.answer

    def showinfo(self, title, msg="", **k):
        self._rec("showinfo", title, msg)

    def showwarning(self, title, msg="", **k):
        self._rec("showwarning", title, msg)

    showerror = showwarning

    def askopenfilename(self, **k):
        self._rec("askopenfilename", k.get("title", ""), "")
        return self.pick_file


DLG = Dialogs()
gui.messagebox.askyesno = DLG.askyesno
gui.messagebox.showinfo = DLG.showinfo
gui.messagebox.showwarning = DLG.showwarning
gui.messagebox.showerror = DLG.showerror
gui.filedialog.askopenfilename = DLG.askopenfilename


def demo_rows():
    """合成的结果行（数值取自演示表量级，不是真实测试数据）"""
    return {
        "DEMO-Digital": [
            {"idx": 1, "band": "TER", "plan": "DEMO-Digital", "ch": "171-DS06",
             "freq_mhz": "171.000", "signal": "DVB-C", "LOCKED": "ON",
             "POWER": "82.5", "CN": "38.9", "MER": "42.2",
             "OFFSET": "-2900", "symb_rate": 6875, "attempts": 1},
            {"idx": 2, "band": "TER", "plan": "DEMO-Digital", "ch": "179-DS07",
             "freq_mhz": "179.000", "signal": "DVB-C", "LOCKED": "OFF",
             "POWER": "71.0", "CN": "", "MER": "", "OFFSET": "",
             "symb_rate": 6875, "attempts": 4},
        ],
        "DEMO-Analog": [
            {"idx": 1, "band": "TER", "plan": "DEMO-Analog", "ch": "144.25-Z05",
             "freq_mhz": "144.250", "signal": "ANALOGTER", "LOCKED": "OFF",
             "LEVEL": "90.6", "CN": "52.6", "MER": "", "OFFSET": "",
             "symb_rate": "", "attempts": 1},
        ],
        "DEMO-C-Band": [
            {"idx": 1, "band": "SAT", "plan": "DEMO-C-Band", "ch": "3780",
             "freq_mhz": "1370.000", "signal": "DVB-S", "LOCKED": "ON",
             "POWER": "68.1", "CN": "20.4", "MER": "19.8",
             "OFFSET": "13000", "symb_rate": 10490, "attempts": 2},
        ],
        "DEMO-Ku-Band": [
            {"idx": 1, "band": "SAT", "plan": "DEMO-Ku-Band", "ch": "12395",
             "freq_mhz": "1095.000", "signal": "DVB-S2", "LOCKED": "ON",
             "POWER": "77.2", "CN": "18.9", "MER": "20.0",
             "OFFSET": "14000", "symb_rate": 27500, "attempts": 1},
        ],
    }


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


def demo_only_dir():
    tmp = tempfile.mkdtemp(prefix="pmcontorl_px_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def wait_finished(root, app, timeout=120.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        root.update()
        if app.thread is not None and not app.thread.is_alive():
            t1 = time.time()
            while time.time() - t1 < 1.5:
                root.update()
                time.sleep(0.02)
            return True
        time.sleep(0.02)
    return False


def main():
    demo_dir = demo_only_dir()
    outdir = tempfile.mkdtemp(prefix="pmcontorl_pxout_")
    root = tk.Tk()
    mock = None
    try:
        app = gui.App(root, demo_dir)
        root.update()
        p = os.path.join(outdir, "demo.XML")

        print("\n--- 1) 结构：SET / CHANNEL / 制式块")
        sets = W.build_sets(demo_rows())
        W.write_promax(p, sets, name="demo")
        r = ET.parse(p).getroot()
        check("根是 POINT 且 name/date/time 齐全",
              r.tag == "POINT" and r.get("name") == "demo"
              and r.get("date") and r.get("time"), r.attrib)
        check("GPS 元素存在且 locked=false",
              r.find("GPS") is not None
              and r.find("GPS").get("locked") == "false")
        css = r.findall("CHANNEL-SET")
        check("4 张表 → 4 个 CHANNEL-SET", len(css) == 4, str(len(css)))
        bands = [c.get("band") for c in css]
        check("频段标注 TERRESTRIAL / SATELLITE",
              bands == ["TERRESTRIAL", "TERRESTRIAL", "SATELLITE", "SATELLITE"],
              str(bands))
        check("equipment / version 与真机一致",
              all(c.get("equipment") == "RANGERNeo 4" and c.get("version") == "1.3"
                  for c in css))
        ch0 = css[0].find("CHANNEL")
        check("射频频道用 frequency（2 位小数）+ units=MHz",
              ch0.get("frequency") == "171.00" and ch0.get("units") == "MHz"
              and ch0.get("downlink") is None, ch0.attrib)
        sat = css[2].find("CHANNEL")
        check("卫星频道用 downlink + sat_band，且没有 frequency",
              sat.get("downlink") == "3780.00" and sat.get("sat_band") == "LOW"
              and sat.get("frequency") is None, sat.attrib)
        check("C 波段中频 1370 → 下行 3780（本振兜底算法）",
              sat.get("downlink") == "3780.00")
        check("Ku 波段中频 1095 → 下行 12395",
              css[3].find("CHANNEL").get("downlink") == "12395.00",
              css[3].find("CHANNEL").get("downlink"))

        print("\n--- 2) 制式块与 STATUS")
        def std_of(cs, i=0):
            m = cs.findall("CHANNEL")[i].find("MEASUREMENTS")
            kids = list(m)
            return kids[0].tag if kids else None, kids[0] if kids else None

        t0, b0 = std_of(css[0])
        check("DVB-C 频道 → <DVB-C>", t0 == "DVB-C", t0)
        check("锁定 → STATUS value=MPEG2 TS locked / LOCKED",
              b0.find("STATUS").get("locked") == "LOCKED"
              and b0.find("STATUS").get("value") == "MPEG2 TS locked",
              b0.find("STATUS").attrib)
        t1, b1 = std_of(css[0], 1)
        check("未锁定 → STATUS value=No signal received / UNLOCKED",
              b1.find("STATUS").get("locked") == "UNLOCKED"
              and b1.find("STATUS").get("value") == "No signal received")
        t2, b2 = std_of(css[1])
        check("模拟频道 → <UNKNOWN>（真机也不定标准）", t2 == "UNKNOWN", t2)
        check("模拟频道带 POWER 且 range=UNKNOWN",
              b2.find("MEASURES/POWER").get("range") == "UNKNOWN")
        check("模拟频道没有 PARAMETERS", b2.find("PARAMETERS") is None)
        t3, b3 = std_of(css[3])
        check("DVB-S2 频道 → <DVB-S2>", t3 == "DVB-S2", t3)

        print("\n--- 3) 数值口径")
        ms = b0.find("MEASURES")
        check("POWER 82.5 / units=dBuV",
              ms.find("POWER").get("value") == "82.5"
              and ms.find("POWER").get("units") == "dBuV")
        check("CN 38.9 / MER 42.2（各 1 位小数）",
              ms.find("CN").get("value") == "38.9"
              and ms.find("MER").get("value") == "42.2")
        check("OFFSET -2900Hz → -2.9 kHz",
              ms.find("OFFSET").get("value") == "-2.9"
              and ms.find("OFFSET").get("units") == "kHz",
              ms.find("OFFSET").attrib)
        check("SYMBOLRATE 6875（整数不带小数点）+ kSymb/s",
              b0.find("PARAMETERS/SYMBOLRATE").get("value") == "6875"
              and b0.find("PARAMETERS/SYMBOLRATE").get("units") == "kSymb/s")
        check("空值字段不写（没 CN/MER/OFFSET 的频道只剩 POWER）",
              [x.tag for x in b1.find("MEASURES")] == ["POWER"],
              [x.tag for x in b1.find("MEASURES")])

        print("\n--- 4) 文件本身：编码 / 行尾 / 缩进")
        raw = open(p, "rb").read()
        check("utf-8 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
        check("CRLF 行尾", raw.count(b"\r\n") > 10
              and raw.count(b"\n") == raw.count(b"\r\n"))
        txt = raw.decode("utf-8")
        check("声明行用双引号", txt.startswith('<?xml version="1.0" '
                                            'encoding="utf-8"?>'), txt[:40])
        check("Tab 缩进（CHANNEL-SET 前是 1 个 Tab）",
              "\n\t<CHANNEL-SET" in txt)
        check("结尾有换行", txt.endswith("</POINT>\r\n"))

        print("\n--- 5) 特殊字符转义 / 空数据")
        esc = {"T&\"表": [{"idx": 1, "band": "TER", "plan": "T&\"表",
                           "ch": "A<B>&\"C", "freq_mhz": "100", "signal": "DVB-C",
                           "LOCKED": "ON", "POWER": "60", "symb_rate": 6875}]}
        pe = os.path.join(outdir, "esc.XML")
        W.write_promax(pe, W.build_sets(esc), name="esc")
        rr = ET.parse(pe).getroot()          # 能解析回来就说明转义没坏
        check("表名/频道名里的 & < > \" 都被正确转义",
              rr.find("CHANNEL-SET").get("name") == 'T&"表'
              and rr.find("CHANNEL-SET/CHANNEL").get("name") == 'A<B>&"C',
              rr.find("CHANNEL-SET").attrib)
        try:
            W.write_promax(os.path.join(outdir, "empty.XML"), [])
            check("空数据应当报错", False)
        except ValueError:
            check("空数据报错，不生成空文件", True)
        check("空数据没留下文件",
              not os.path.exists(os.path.join(outdir, "empty.XML")))

        print("\n--- 6) 事后转换：xlsx / csv → XML")
        x = os.path.join(outdir, "demo.xlsx")
        gui.write_result(x, demo_rows())
        out1, n1 = W.convert_result_file(x)
        check("xlsx 转换生成同名 .XML", out1.lower() == x.lower()[:-5] + ".xml",
              out1)
        check("频道数 = 5", n1 == 5, str(n1))
        r1 = ET.parse(out1).getroot()
        check("转换后仍是 4 个 CHANNEL-SET", len(r1.findall("CHANNEL-SET")) == 4)
        check("转换链路里 OFFSET 仍是 -2.9 kHz（没被二次除 1000）",
              r1.find("CHANNEL-SET/CHANNEL/MEASUREMENTS/DVB-C/MEASURES/OFFSET"
                      ).get("value") == "-2.9",
              r1.find("CHANNEL-SET/CHANNEL/MEASUREMENTS/DVB-C/MEASURES/"
                      "OFFSET").attrib)

        c = os.path.join(outdir, "demo.csv")
        gui.write_result(c, demo_rows())
        out2, n2 = W.convert_result_file(c)
        check("csv 转换也成功", n2 == 5 and os.path.isfile(out2), str(n2))

        # 列裁剪后的文件（纯射频：没有 OFFSET / SR 列）也要能转
        ter_only = {k: v for k, v in demo_rows().items() if k.startswith(
            "DEMO-D") or k == "DEMO-Analog"}
        x2 = os.path.join(outdir, "ter_only.xlsx")
        gui.write_result(x2, ter_only)
        out3, n3 = W.convert_result_file(x2)
        check("列裁剪过的文件（纯射频）能转", n3 == 3, str(n3))
        r3 = ET.parse(out3).getroot()
        check("裁剪后没有 OFFSET 元素也不报错",
              r3.find(".//DVB-C/MEASURES/OFFSET") is None)

        # 用频道表回推下行（有 downlink_map 时以它为准）
        dmap = {("DEMO-C-Band", "3780"): 3780.0,
                ("DEMO-Ku-Band", "12395"): 12395.0}
        sets2 = W.build_sets(demo_rows(), dmap)
        check("downlink_map 命中时用表里的下行",
              sets2[2][2][0]["downlink"] == 3780.0
              and sets2[3][2][0]["downlink"] == 12395.0)

        # ---------------- GUI 集成 ----------------
        print("\n--- 7) GUI：设置开关 → 跑完自动生成 .XML")
        if port_busy():
            print("  [SKIP] 2222 被占用（可能有残留模拟器进程），跳过集成测试")
        else:
            mock = subprocess.Popen([PY, MOCK, "--quiet"], cwd=HERE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            time.sleep(1.2)
            it = [i for i in app.items if i.band == "TER"
                  and len(i.points()) > 2][0]
            for k, v in app.check_vars.items():
                if k != it.key and v.get():
                    app._toggle(k)
            if not app.check_vars[it.key].get():
                app._toggle(it.key)
            out = os.path.join(outdir, "auto_test.xlsx")
            app.out_var.set(out)
            app.ip_var.set("127.0.0.1")
            app.port_var.set("2222")
            DLG.answer = False            # 不打开结果文件
            root.update()
            app.start()
            check("测试跑完", wait_finished(root, app))
            px = W.default_promax_path(app.out_path or out)
            check("结果旁边自动生成了 .XML", os.path.isfile(px), px)
            if os.path.isfile(px):
                rr = ET.parse(px).getroot()
                n = sum(len(cs.findall("CHANNEL"))
                        for cs in rr.findall("CHANNEL-SET"))
                check("XML 里频道数 = 本次结果行数（%d）" % len(
                    app.rows.get(it.name, [])), n == len(app.rows.get(it.name, [])),
                    "%d vs %d" % (n, len(app.rows.get(it.name, []))))
                check("XML 里能查到锁定状态",
                      rr.find(".//STATUS") is not None
                      and rr.find(".//STATUS").get("locked") in
                      ("LOCKED", "UNLOCKED"))
            check("日志里提到 Promax 文件",
                  "Promax 文件:" in app.log.get("1.0", "end"))

            print("\n--- 8) GUI：「转换成Promax格式文件」按钮")
            src = os.path.join(outdir, "manual.xlsx")
            gui.write_result(src, demo_rows())
            DLG.pick_file = src
            DLG.calls.clear()
            app.convert_promax()
            dst = W.default_promax_path(src)
            check("按钮生成了同名 .XML", os.path.isfile(dst), dst)
            check("转换完成弹窗",
                  any(c[0] == "showinfo" for c in DLG.calls),
                  [c[:2] for c in DLG.calls])
            check("日志里记了转换结果",
                  "已生成 Promax 格式文件" in app.log.get("1.0", "end"))

        print("\n=====================================")
        print("通过 %d   失败 %d" % (OK, BAD))
        return 1 if BAD else 0
    finally:
        if mock is not None:
            try:
                mock.kill()
            except Exception:
                pass
        try:
            root.destroy()
        except Exception:
            pass
        shutil.rmtree(demo_dir, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
