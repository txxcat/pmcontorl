# -*- coding: utf-8 -*-
"""配置层：json 配置 + 下载目录（注册表）+ 历史记录。

配置文件 plan_scan_gui.json 结构（**该文件不随仓库分发**，见 .gitignore）：
{
  "window": {"width": 1200, "height": 760},
  "ip_history": ["127.0.0.1"],
  "last_ip": "127.0.0.1",
  "port": 2222,
  "out_dir": "",                     # 空 = 跟随系统下载文件夹
  "out_dir_history": [],
  "settle": 0.15,
  "pause_between": true,
  "export_format": "xlsx",           # 结果文件格式：xlsx / csv
  "export_promax": false,            # 同时生成 Promax 格式 .XML（默认关）
  "presets": [...]                   # 本地预设组（真实数据，不上传）
}

首次运行（没有 json）时用 DEFAULT_CONFIG：IP 127.0.0.1、预设组指向
CH/ 下的 DEMO-* 演示频道表 —— 仓库开箱即可演示，不需要真实仪器数据。
"""

import json
import os
import sys
import time


def _app_dir():
    """程序所在目录（打包成 exe 时是 exe 目录，否则是脚本目录）。

    🔴 配置必须锚定到**程序所在目录**，绝不能用相对路径（相对 CWD）。
    否则从不同目录启动（双击 / 命令行 / 快捷方式）会读写不同的文件，
    表现为"配置莫名其妙被重置"。这是踩过的坑（2026-10-14）。
    """
    if getattr(sys, "frozen", False):        # PyInstaller 打包后
        return os.path.dirname(os.path.abspath(sys.executable))
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


CONFIG_NAME = os.path.join(_app_dir(), "plan_scan_gui.json")
OLD_CONFIG_NAME = "plan_scan_gui.json"      # 旧版遗留的相对路径写法，用于迁移

DEFAULT_CONFIG = {
    "window": {"width": 1240, "height": 780},
    "ip_history": ["127.0.0.1"],
    "last_ip": "127.0.0.1",
    "port": 2222,
    "out_dir": "",                 # 空 = 用系统下载文件夹
    "out_dir_history": [],
    "settle": 0.15,
    # 稳定等待：卫星锁定比射频慢，必须分开设
    # 默认值来自真机实测（2026-10-15 收敛），是各频段的最佳值：
    #   · 射频 settle=0.3s → 一次锁
    #   · 卫星 settle=3.0s → 基线一次锁；个别慢锁频道需触发自动重试
    "settle_ter": 0.3,
    "settle_sat": 3.0,
    # 锁定失败重试（保证卫星数据完整性，这是本程序的核心诉求）：
    #   · 射频 retry=3, step=0.3 → 累计等待 0.3 / 0.6 / 0.9 / 1.2s，远超基线
    #     （射频 LOCK_TIME=0.15s），所以**永远一次锁**，重试是兜底
    #   · 卫星 retry=5, step=6 → 累计等待 3 / 9 / 15 / 21 / 27s，能覆盖
    #     SLOW_LOCK 里 8~16s 的个别慢锁频道（验过：8s→第2次锁、16s→第3次锁），
    #     极端 80s+ 的也允许 5 次后判失败。
    "retry_ter": 3,             # 射频重试次数
    "retry_sat": 5,             # 卫星重试次数
    "retry_step_ter": 0.3,      # 射频每次重试增加的稳定等待（秒）
    "retry_step_sat": 6.0,      # 卫星每次重试增加的稳定等待（秒）
    "pause_between": True,      # 手动多选时，表间是否暂停（默认暂停）
    # ---- 断线自动重连 ----
    # 仪器走 WiFi 时链路会抖（掉线 / AP 重连 / DHCP 续约）。断线后**不中断测试**：
    # 暂停 → 等重连 → 从当前频点继续（已测完的不重测）。
    # auto_reconnect=True 时定时自动重试，网络恢复后无需人工干预即可续跑；
    # 关掉后仍可点界面上的「重试连接」手动重试。
    "auto_reconnect": True,
    "reconnect_interval": 10.0,   # 自动重连间隔（秒），下限 2
    # 结果文件格式：xlsx（带表头样式）/ csv（通用，Excel 可直接打开）
    "export_format": "xlsx",
    # 保存结果时**同时**再生成一份 Promax 仪器格式的 .XML（版本 1.3 结构），
    # 供只认仪器导出文件的第三方软件读取。默认关：多数人只要 xlsx/csv。
    "export_promax": False,
    # ---- 日志 ----
    "log_dir": "",              # 空 = 当前工作目录
    "log_name": "promax_test.log",   # 单一日志文件名（追加写入）
    "log_timestamps": True,     # 每行日志前加 [HH:MM:SS.mmm] 时戳
    "log_timing": True,         # 记录频点级耗时（调谐/锁定/总计）
    "log_cmd_timing": False,    # 记录命令级收发时刻与耗时（日志量大，默认关）
    # 额外再生成一份"独立日志"（与结果文件同目录同主名，覆盖式），
    # 便于调试时单独查看某一次测试，不影响上面那个追加式单一日志。
    "log_separate": False,
    "presets": [],
}


def log_path(cfg):
    """日志文件的完整路径：log_dir 优先，空则当前工作目录"""
    d = (cfg.get("log_dir") or "").strip()
    if not d or not os.path.isdir(d):
        d = os.getcwd()
    d = os.path.abspath(d)          # 统一分隔符，避免 D:/x\y 混用
    name = (cfg.get("log_name") or "promax_test.log").strip()
    if not name:
        name = "promax_test.log"
    if not name.lower().endswith(".log"):
        name += ".log"
    return os.path.join(d, name)


def load_config(path=None):
    """读 json 配置；不存在或损坏则用默认值"""
    if path is None:
        path = CONFIG_NAME
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # 深拷贝
    _migrate_old_path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except Exception:
        pass
    # 兼容旧版 ini 迁移
    if not cfg.get("presets"):
        old = _migrate_ini()
        if old:
            cfg.update(old)
            save_config(cfg, path)
    return cfg


def _migrate_old_path(path):
    """旧版把配置存在 CWD 下的相对路径；若新位置还没有配置，
    而 CWD 下那份存在，就搬过来（只搬一次，避免丢用户数据）。"""
    try:
        if os.path.abspath(path) == os.path.abspath(OLD_CONFIG_NAME):
            return                       # 本来就是同一份（脚本目录 == CWD）
        if os.path.isfile(path):
            return                       # 新位置已有配置，不覆盖
        cwd_old = os.path.join(os.getcwd(), OLD_CONFIG_NAME)
        if os.path.isfile(cwd_old):
            import shutil
            shutil.copy2(cwd_old, path)
    except Exception:
        pass


def _migrate_ini():
    """旧 plan_scan_gui.ini → 新 json 的字段迁移"""
    if not os.path.isfile("plan_scan_gui.ini"):
        return None
    try:
        with open("plan_scan_gui.ini", "r", encoding="utf-8") as f:
            old = json.load(f)      # 旧 ini 本身也是 json 内容
    except Exception:
        return None
    out = {}
    if old.get("ip_history"):
        out["ip_history"] = old["ip_history"]
    if old.get("last_ip"):
        out["last_ip"] = old["last_ip"]
    if old.get("port"):
        out["port"] = old["port"]
    return out


def save_config(cfg, path=None):
    if path is None:
        path = CONFIG_NAME
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # 写前先滚动备份（保留最近若干份），万一被误写还能找回
        _backup_config(path)
        # 原子写：先写临时文件再替换，避免写一半被中断导致配置损坏
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


BACKUP_KEEP = 10                # 滚动备份保留份数
BACKUP_DIRNAME = "bak"          # 备份子目录名（与配置文件同级）


def _backup_config(path):
    """把现有配置滚动备份到 <配置同级目录>/bak/<名>.bak / .bak2 / …（保留最近 N 份）。

    备份集中放在 bak/ 子目录里，主目录只留当前配置，不被 .bak 系列文件堆满。
    只在现有文件「看起来有效」时备份 —— 避免把损坏内容固化成备份。
    """
    try:
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        if not data.strip():
            return
        json.loads(data)                    # 不是合法 json 就不备份
        basedir = os.path.dirname(os.path.abspath(path))
        bakdir = os.path.join(basedir, BACKUP_DIRNAME)
        os.makedirs(bakdir, exist_ok=True)
        base = os.path.basename(path)       # 如 plan_scan_gui.json
        def bname(i):
            nm = (base + ".bak") if i == 1 else ("%s.bak%d" % (base, i))
            return os.path.join(bakdir, nm)
        # 删最老的，然后从老到新依次后移
        last = bname(BACKUP_KEEP)
        if os.path.isfile(last):
            os.remove(last)
        for i in range(BACKUP_KEEP - 1, 0, -1):
            src = bname(i)
            if os.path.isfile(src):
                os.replace(src, bname(i + 1))
        import shutil
        shutil.copy2(path, bname(1))
    except Exception:
        pass


# --------------------------------------------------------------- 下载目录
def get_download_path():
    """返回系统下载文件夹（读注册表，用户改过路径也能拿到）"""
    if os.name == "nt":
        try:
            import winreg
            sub_key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                       r"\Explorer\Shell Folders")
            guid = "{374DE290-123F-4565-9164-39C4925E467B}"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                return winreg.QueryValueEx(key, guid)[0]
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "downloads")


def date6():
    return time.strftime("%y%m%d")


def date8():
    return time.strftime("%Y%m%d")


def default_out_dir(cfg):
    """输出目录：配置优先，否则系统下载文件夹"""
    d = (cfg.get("out_dir") or "").strip()
    if d and os.path.isdir(d):
        return d
    p = get_download_path()
    return p if os.path.isdir(p) else os.getcwd()


# --------------------------------------------------------------- 预设组
# 🔴 仓库里只放**演示用**预设（指向 CH/ 下的 DEMO-* 演示频道表）。
#    真实频道表从仪器导出、含实际运营数据，**不随仓库分发**（见 .gitignore）；
#    用户自己的预设组写在本地 plan_scan_gui.json 里，同样不会上传。
DEFAULT_PRESETS = [
    {"name": "射频数字_DEMO", "files": ["DEMO-Digital"],
     "filename": "射频数字_DEMO_{date8}", "pause": False},
    {"name": "射频全频道_DEMO", "files": ["DEMO-Digital", "DEMO-Analog"],
     "filename": "射频全频道_DEMO_{date8}", "pause": False},
    {"name": "卫星C波段_DEMO", "files": ["DEMO-C-Band"],
     "filename": "卫星C波段_DEMO_{date8}", "pause": True},
    {"name": "卫星Ku波段_DEMO", "files": ["DEMO-Ku-Band"],
     "filename": "卫星Ku波段_DEMO_{date8}", "pause": True},
    # 跨频段混合：结果文件会同时含射频与卫星行（列取并集，17 列）
    {"name": "混合演示_DEMO", "files": ["DEMO-Digital", "DEMO-C-Band"],
     "filename": "混合演示_DEMO_{date8}", "pause": True},
]


def preset_pause(preset, cfg=None):
    """该预设运行时，表间是否暂停。

    预设里没写 pause 时回退到全局 pause_between（默认 True）。
    兼容字符串 "true"/"false"/"yes"/"no"/"1"/"0"。
    """
    v = preset.get("pause")
    if v is None:
        if cfg is not None:
            v = cfg.get("pause_between", True)
        else:
            v = True
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on", "是")
    return bool(v)


def presets(cfg):
    """取预设列表；配置文件没有则写入默认值"""
    ps = cfg.get("presets")
    if not ps:
        cfg["presets"] = json.loads(json.dumps(DEFAULT_PRESETS))
        save_config(cfg)
        ps = cfg["presets"]
    return ps


def match_preset(items, files):
    """按 files 里的前缀匹配 PlanItem，返回 (命中, 未匹配前缀)

    前缀匹配：'DEMO-' 命中 'DEMO-Digital.XML'；'DEMO-C' 命中 'DEMO-C-Band.XML'。
    """
    hit, miss = [], []
    for f in files:
        f = str(f).strip()
        if not f:
            continue
        found = [it for it in items if it.file.strip().startswith(f)]
        if found:
            hit.extend(found)
        else:
            miss.append(f)
    # 去重且保持频道表原始顺序
    seen, out = set(), []
    for it in items:
        if it in hit and it.key not in seen:
            seen.add(it.key)
            out.append(it)
    return out, miss


def preset_filename(preset):
    """按预设规则生成文件名（支持 {date8} {date6}）"""
    tpl = preset.get("filename") or (preset.get("name", "结果") + "_{date8}")
    return (tpl.replace("{date8}", date8())
               .replace("{date6}", date6()))


def band_settings(cfg, band):
    """取某频段的稳定等待/重试参数

    band: 'TER' | 'SAT'
    返回 (settle, retry, retry_step)
    兼容旧配置：没有 settle_ter/settle_sat 时回退到 settle。
    默认值与 DEFAULT_CONFIG 保持一致（实测最佳）：
      · 射频  0.3 / 3 / 0.3
      · 卫星  3.0 / 5 / 6.0
    """
    sfx = "ter" if band == "TER" else "sat"
    if band == "TER":
        fb_settle, fb_retry, fb_step = 0.3, 3, 0.3
    else:
        fb_settle, fb_retry, fb_step = 3.0, 5, 6.0
    try:
        settle = float(cfg.get("settle_" + sfx, cfg.get("settle", fb_settle)))
    except (TypeError, ValueError):
        settle = fb_settle
    try:
        retry = int(cfg.get("retry_" + sfx, fb_retry))
    except (TypeError, ValueError):
        retry = fb_retry
    try:
        step = float(cfg.get("retry_step_" + sfx, fb_step))
    except (TypeError, ValueError):
        step = fb_step
    return settle, max(0, retry), max(0.0, step)
