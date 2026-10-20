# pmcontorl — PROMAX RANGERNeo 4 频道表批量测试工具

通过 **Telnet** 远程控制 PROMAX 场强仪，对仪器里的**频道表（Channel Plan）**做批量测量，
把结果导出成 Excel / CSV，用于有线电视网络与卫星链路的定期巡检。

> **目标仪器**：RANGERNeo 4 — High class field strength meter and spectrum analyzer with 4K decoder
> （固件 `*VER 31.17`，Telnet 端口 `2222`）

## 下载（Windows 免安装）

不想装 Python 的同学直接下打包版：**[Releases 页面](https://github.com/txxcat/pmcontorl/releases/latest)**
→ `pmcontrol-v1.0.0-win64.zip`（约 23 MB）→ 解压 → 进 `pmcontrol` 文件夹双击 `pmcontrol.exe`。

- 已内置演示预设与演示频道表，**无需仪器即可体验完整流程**
- 🔴 不要把 exe 单独拖出来运行，它依赖同目录的 `_internal`
- 首次运行弹「Windows 已保护你的电脑」是正常的（未购买代码签名证书）：
  点「更多信息」→「运行 anyway」
- 仅支持 Windows 10/11 64 位

**主界面**

![主界面](docs/pmcontorl_main.png)

**设置界面**

![设置界面](docs/screenshot-settings.png)

---

## 功能

- **频道表批量测试**：射频（TER）/ 卫星（SAT）分两组展示，可单选、可跨组多选
- **快捷测试组**：一键选中一组频道表并按规则生成结果文件名（配置驱动，可自定义）
- **自动锁定重试**：未锁定时保持同频继续等待，逐步加大稳定等待时间
- **断线自动重连**（仪器走 WiFi 时链路会抖）：连接断开时测试**暂停**而不是中断，
  自动重连（间隔可配，默认 10s）并从**当前频点**继续；已测完的频点不会重测。
  也可以点工具栏的「重试连接」按钮立即重试，或点「中止」结束并保存已测数据。
  **两个阶段都覆盖**：换表时的「表级准备」（切频段 / 下发 LNB / 校验频段）与
  逐频点测量 —— 表边界恰恰是断线高发点（正在动线、仪器也可能踢掉空闲会话），
  重连成功后表级准备会整表重做，不会让整场测试提前结束。
- **测试进度一目了然**：
  - 右侧「已选频道表」里，正在测的那张标成琥珀 + `▶`，已测完的转浅绿 + `✓`
    （表多时自动滚动到当前行），一眼看得出跑到第几张、还剩哪些
  - 界面下方两条进度条：**当前频道表**（本表已测频点 / 本表频点数）与
    **全部**（已测频点 / 所有所选表的频点合计，**按频道数加权**，不是按表的张数）
  - 日志里每个频点都带序号与总数，如 `[  3/20]`，不用数着行看还剩多少
  - 多表连测需要中途换接口时尤其省事。
- **计时器**：工具栏最右侧显示本轮**净用时**（`用时 00:12:34`）。断线挂起、
  换线暂停（等你接线的那个弹窗）期间**同步停表**，只有真正在测的时间才计入；
  收尾时定格，并写进日志的「【用时】」与汇总行。
- **重测失败频道**（卫星批量测试的最大痛点）：一轮跑完后，工具栏按钮会变成
  「重测失败频道 (n)」并亮起 —— 点它**只补测未锁定 / 设置失败的那几条**，
  结果按频道名覆盖回**同一个结果文件**（已成功的频道一条都不重测）。
  行的位置按原频道序号排好，不产生重复行；仍然失败的会继续留在待办里，可反复补测。
- **结果导出**：xlsx（带表头样式）/ csv（utf-8-sig，Excel 直开），设置里可切
- **Promax 格式文件（.XML）**：按仪器导出的 `POINT / CHANNEL-SET / CHANNEL`
  结构（version 1.3）生成，供只认仪器导出文件的第三方软件读取。两个入口：
  - 设置里勾「同时生成 Promax 格式文件」→ 每次保存结果时**顺带**落一份同名 `.XML`
  - 界面右侧「转换成Promax格式文件」按钮 → 把**已有的** xlsx / csv 转成 `.XML`
  - 命令行（不装 GUI 也能用）：`python promax_plan_write.py 结果.xlsx [-o out.XML]`

  | 结果里的数据 | 写进 XML 的哪里 |
  |---|---|
  | 频道名 / 频率 | `CHANNEL name` / `frequency`（射频）或 `downlink` + `sat_band`（卫星） |
  | 制式 | `<DVB-C>` / `<DVB-S>` / `<DVB-S2>`，模拟频道写 `<UNKNOWN>`（真机也这样） |
  | LOCK | `STATUS value="MPEG2 TS locked" locked="LOCKED"`，未锁定写 `No signal received` / `UNLOCKED` |
  | POWER / CN / MER / OFFSET | `<MEASURES>` 下同名元素（OFFSET 用 kHz，TE 表里是 Hz，会自动换算） |
  | 符号率 | `<PARAMETERS><SYMBOLRATE value=".." units="kSymb/s" />` |

  > 仪器还写 CONSTELLATION / CODERATE / CBER / VBER / LM / BANDWIDTH，
  > 这些**测不到就不写** —— 宁可缺字段，也不编数据。
  > 卫星结果里存的是**中频**，转 XML 时靠当前频道表回推真实下行（表不在时按
  > 「频道名即下行」或本振推算兜底）。
- **按频段裁剪结果列**（实测口径，避免无意义空列）：
  | 测试类型 | 列数 | 不输出 |
  |---|---|---|
  | 射频 TER | 15 | `OFFSET`、`SR`（射频无频偏/符号率概念） |
  | 卫星 SAT | 16 | `LEVEL`（卫星只报 `POWER`） |
  | 射频+卫星混合 | 17 | —（取并集，保留任一频段要用的列） |
- **两套日志**：单一日志（追加式，跨次累积）+ 独立日志（覆盖式，与结果文件同主名）
- **三层时间记录**：行首时戳、频点级耗时（调谐/等待/总计）、可选命令级收发时刻
- **模拟器**：无真机也能跑通全流程（含锁定/重试/NAK 行为复现）

### 测试进度

多表连测时不用猜跑到哪了。下面这张图就是右侧列表高亮 + 下方两条进度条同框的效果：

![进度反馈](docs/screenshot-progress.png)

**右侧「已选频道表」的三种状态：**

| 标记 | 含义 |
|---|---|
| 琥珀 + `▶` | **正在测试**（表多到列表装不下时会自动滚到这一行） |
| 浅绿 + `✓` | 已完成 |
| 白 | 排队中 |

**下方两条进度条：**

| 进度条 | 含义 |
|---|---|
| 当前频道表 | 本表已测频点 / 本表频点数。换表时归零，起点是「换线暂停」弹窗出现的那一刻 |
| 全部 | 已测频点 / 所有所选表的频点**合计**。按频道数加权 —— 一张 50 频点的表和一张 2 频点的表各占 50% 会严重失真，所以分母是频道数而不是表的张数 |

**顶栏计时器（工具栏最右）：** 显示本轮净用时，形如 `用时 00:03:12`。
暂停时同步停表并标注 `用时 00:03:12（已暂停）` —— 断线等重连、换线等接线
这些"没在测"的时间不计入，所以它反映的是**仪器的实际工作时长**。

**日志里的频点序号带总数**，形如 `[  3/20]`，一眼看出还剩多少。

> 某个频点彻底失败（设置被 NAK / 超时）时不会产生结果行，进度条在收尾时会把这一格补上 ——
> 每个频点最终都有结论，进度条不该永远差一格。

## 环境要求

- Python 3.8+（GUI 用 `tkinter`，写 xlsx 需要 `openpyxl`，缺失时自动退化为 CSV）
- 仪器与主机网络互通，仪器 Telnet 端口 `2222` 开放

## 快速上手

```bash
# 1. 克隆
git clone https://github.com/txxcat/pmcontorl.git
cd pmcontorl

# 2. （可选）装 openpyxl，不装则结果存为 CSV
pip install openpyxl

# 3. 启动
python promax_plan_gui.py
```

仓库自带 **DEMO-\* 演示频道表**，首次启动 IP 预填 `127.0.0.1`、快捷测试组指向演示表，
**开箱即可跑通界面与导出流程**，不需要真实仪器数据。

连真实仪器：设置里把「默认 IP」改成仪器的地址（如 `192.168.x.x`），端口默认 `2222`。

### 自己打包成 exe（PyInstaller）

```bash
pip install pyinstaller
pyinstaller -D -w -i "PM Ranger Neo4.png" -n pmcontrol promax_plan_gui.py
```

产物在 `dist/pmcontrol/`（约 33 MB）。发布时把**整个文件夹**打成一个 zip，
并一起放入 `CH/` 演示表与一份使用说明 —— 程序用 `_app_dir()` 定位配置与频道表，
解压后换任意目录都能跑。注意：`config.json` 含真实 IP 与预设，**不要**打进包，
首次运行会自动生成默认配置。

### 没有仪器？用模拟器

```bash
python mock_promax_server.py            # 另一个终端，监听 127.0.0.1:2222
python promax_plan_gui.py               # IP 保持 127.0.0.1 即可
```

模拟器复现了真机的关键行为，包括：

- `*TUNE PLAN=` 恒 NAK、`LNB OUTPUT=0` NAK、`SIGNAL STANDARD=DK` NAK
- 锁定只由**等待时间**决定；**重发 `*TUNE` 会重置捕获导致永远锁不上**
- **锁定时间按频段给**：射频 0.15s（客户端一次等待就锁）、卫星基线 2.0s（一次锁），
  **个别卫星频道**按 `SLOW_LOCK` 表要 8~16s → 触发客户端的锁定重试
  （`SLOW_LOCK` 的值 = **该频道需要的连续等待秒数**，键为「频段 + 中频 MHz」）。
  表里同时包含「DEMO 演示组」（对应仓库自带的 `DEMO-C-Band`）和「现场实测组」——
  后者是按作者仪器标定的，**用自己机器的频道表时请改成你实测到的中频**。
- 模拟制式永不报 `LOCKED=ON`，只返回 `LEVEL`
- **断链注入**：`--drop-on-band N` 收到第 N 条 `*TUNE BAND=` 时直接断链，
  用来复现"换表瞬间 / 表级准备阶段掉线"（表边界是断线高发点）

### 跑测试

```bash
python _selftest_mock.py          # 协议级自检（40 项）
python _e2e_mock.py DEMO-C-Band.XML   # 端到端跑指定频道表
python _test_reconnect.py         # 断线恢复：跑到一半杀掉模拟器，验证暂停→重连→续跑
python _test_reconnect.py --manual      # 同上，但关掉自动重连，只走「重试连接」按钮
python _test_reconnect.py --cold-start  # 开始时模拟器没起（验证首连失败也能挂起重试）
python _test_reconnect.py --boundary    # 断在**表级准备**阶段（换表瞬间掉线，验证不中断）
python _test_highlight.py         # 「已选频道表」进度高亮（含真跑模拟器的集成验证）
python _test_progress.py          # 频点日志序号 + 两条进度条（含真跑模拟器的集成验证）
python _test_timer.py             # 顶栏计时器：暂停停表 / 断线挂起不计时（含集成验证）
python _test_retest.py            # 重测失败频道：只补测未成功的几条并合并回原文件
python _test_promax.py            # Promax 格式导出 / 转换（结构、编码、口径、GUI 两个入口）
```

## 关于频道表数据

频道表是**从仪器直接导出**的，目录结构遵循仪器导出惯例：

```
CH/
  TER/*.XML      # 地面/射频频道表
  SAT/*.XML      # 卫星频道表
```

🔴 **仓库里只包含 `DEMO-*` 演示表**。真实频道表含实际运营数据，已在 `.gitignore` 中排除，
不会随仓库分发。把自己的表放进 `CH/TER`、`CH/SAT` 即可使用，多余的表直接删掉。

同理，**快捷测试组与配置写在本地 `config.json`**（含真实 IP、输出目录历史），
该文件同样被 `.gitignore` 排除。首次运行会用内置的演示默认值。
每次保存配置前会滚动备份最近 10 份到同级的 `bak/` 子目录（`config.json.bak … .bak10`），
主目录只留当前配置。

## 目录结构

| 文件 | 说明 |
|---|---|
| `promax_plan_gui.py` | GUI 主程序（tkinter） |
| `promax_plan_dump.py` | 频道表 XML 解析（离线） |
| `promax_telnet_test.py` | Telnet 连接层与命令封装 |
| `promax_gui_config.py` | 配置层（json + 滚动备份到 `bak/` + 原子写） |
| `promax_plan_write.py` | 写 Promax 仪器格式 XML / 读结果文件（也可单独当命令行用） |
| `promax_plan_scan.py` | 命令行版（无 GUI） |
| `mock_promax_server.py` | 仪器模拟器 |
| `_selftest_mock.py` | 协议级自检（对模拟器跑） |
| `_e2e_mock.py` | 端到端测试 |
| `_test_reconnect.py` | 断线恢复测试（暂停 / 重连 / 续跑） |
| `_test_highlight.py` | 「已选频道表」进度高亮测试（消息层 + 真跑模拟器集成） |
| `_test_progress.py` | 频点日志序号 + 两条进度条测试（消息层 + 真跑模拟器集成） |
| `_test_timer.py` | 顶栏计时器测试（暂停停表 + 真跑模拟器集成：暂停时长确实被扣掉） |
| `_test_retest.py` | 「重测失败频道」测试（覆盖不重复 / 只跑待办频道 / 结果文件合并） |
| `_test_promax.py` | Promax 格式导出测试（XML 结构 / 编码 / 数值口径 / xlsx·csv 转换） |
| `_shot_settings.py` | 生成 README 用的三张界面截图（用演示配置，不碰真实数据） |
| `CH/TER`, `CH/SAT` | 频道表目录（仅 DEMO-\* 入库） |

## ⚠️ 真机协议踩坑记录

以下都是在 RANGERNeo 4（固件 31.17）上实测得出的结论，**与 2017 版手册有出入，以实测为准**：

1. **卫星 `*TUNE BAND=SAT FREQ=` 收的是中频（IF），不是下行频率**
   `IF = |downlink − LNB 本振|`（C 波段本振 5150、Ku 波段 11300）。
   直接发 downlink 会被 NAK。Ku 波段方向相反：`IF = downlink − 本振`。

2. **锁定只由「等待时间」决定**
   调谐后需要连续等待若干秒才报 `LOCKED=ON`；**重发 `*TUNE` 会重置捕获，导致永远锁不上**。
   重试时必须保持同频、只延长等待。符号率设不设、对错都不影响锁定（仪器自动搜）。

3. **模拟频道要调「图像载频」，不是 XML 里的 `frequency`**
   XML 的 `frequency` 是频道**中心频率**（ PAL-DK 比图像载频高 2.75 MHz），落在中心频率上
   什么也测不到。频道名通常自带图像载频（如 `113.25-Z03`），优先用它。

4. **制式不同，上报的测量项不同**
   模拟制式永不报 `LOCKED=ON`，返回 `LEVEL` + `C/N`；
   数字制式返回 `POWER` / `CH_POWER` + `MER`，没有 `LEVEL`。取值要「原名优先、映射名兜底」。

5. **`LNB OUTPUT` 只认 `OFF` 或 `*?LNB` 列出的可用电压**
   传 `0` / `0V` / `13V` 一律 NAK。

6. **`*TUNE PLAN=` 在 31.17 上恒 NAK**（远程切不了表）
   所以本工具一律按频率调谐；`*?TUNE CH` 回显里的 `PLAN=` 与本程序无关，不能用来判断切表成败。

7. **关闭连接必须 `SO_LINGER(1,0)` 强制 RST**
   仪器只允许一个 Telnet 会话，正常四次挥手不一定能立即释放。

8. **判断响应结束要用「收到完整 CR+LF 行」，不能用「数据静默」**
   仪器每秒发心跳 `XON`，会不停刷新最后收包时间，用静默判停会让每条命令都跑满超时。

9. **超时要给足**：单条命令实测要数秒，默认 20s。

## 许可证

[MIT](LICENSE)
