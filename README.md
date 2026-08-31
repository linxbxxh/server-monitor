# 服务器监控(桌面悬浮卡片 + Web 面板)

一个轻量的多服务器资源监控工具：本机通过 SSH 定时采集服务器指标。
默认以**桌面悬浮卡片**常驻：小方块贴在屏幕边缘，一屏显示全部服务器概览，鼠标悬停展开逐芯片详情，移开自动收起；
也可以随时在浏览器打开完整网页面板。专为昇腾 NPU（Ascend）服务器设计，同时兼容 NVIDIA GPU、纯 CPU 机器。

## 功能

- **桌面悬浮卡片**（`ServerMonitor.exe` 默认形态）
  - 置顶小方块(约 300×160)：一屏显示全部服务器——状态点、名字、CPU 占比条与百分比、
    NPU 平均算力条、显存合计；磁盘超阈值出现 ⚠ 圆点，右上角角标 = 告警数量
  - 贴近屏幕边缘自动吸附并略微变淡；鼠标悬停展开详情面板：
    每台服务器的内存 / NPU 逐芯片算力与 HBM 显存 / 磁盘 / 最近告警
  - 可拖拽换位置，松手自动吸附最近的屏幕边缘；右键菜单：锁定面板、立即刷新、
    在浏览器打开完整面板、隐藏悬浮窗、退出（隐藏后可从托盘图标找回）
- **采集指标**（每台服务器，默认 15 秒一轮）：
  - CPU 利用率（含历史趋势线）、内存用量
  - **NPU**：`npu-smi` 逐芯片 AICore 利用率、HBM 显存、温度、功耗、占用进程
  - **GPU**：`nvidia-smi` 逐卡利用率、显存、温度（无 NPU 时自动切换）
  - 磁盘各分区使用率、系统负载、运行时长、网卡收发速率
- **Web 完整面板**（深色主题，5 秒自动刷新）：每台服务器一张大卡片（曲线图、进度条、
  阈值变色），页头汇总 + 最近告警列表；悬浮球菜单或 `python app.py` 均可打开
- **阈值告警**：CPU / 内存 / 磁盘超阈值自动记录（面板展示 + 控制台日志）
- 服务器列表直接复用本机 `~/.ssh/config` 的 Host 配置，无需重复填写密钥

## 快速开始

### 方式一: 直接运行 exe(推荐, 无需装 Python)

双击 **ServerMonitor.exe**，桌面边缘出现悬浮卡片；要看大面板时右键 → "在浏览器打开完整面板"。

> exe 必须和 `config.yaml` 放在同一目录; 改完配置重启 exe 生效。
> 当前 exe 已打包好(约 60 MB, 内含 Qt 运行库), 放在项目根目录。
> 托盘图标可能被 Windows 收进任务栏角落的"^"折叠区。

### 方式二: 源码运行

```bash
cd server-monitor
pip install -r requirements.txt   # flask / paramiko / pyyaml / PyQt5
python widget.py                  # 桌面悬浮卡片(或双击 run.bat)
python app.py                     # 网页版面板 http://127.0.0.1:8787
```

### 重新打包 exe

```bash
build.bat      # 用 PyInstaller 重新生成 ServerMonitor.exe(悬浮球版)
```

> 当前 `config.yaml` 已启用你的 3 台 ModelArts（ZCode）服务器，
> 内网 62.85 / 62.86（4×RTX 2080 Ti）与 172.22.11.55 以 `enabled: false` 备着，改一行即可启用。

## 配置（config.yaml）

```yaml
settings:
  interval_seconds: 15      # 采集间隔
  listen_port: 8787         # 网页版面板端口
  thresholds:               # 告警阈值(%)
    cpu_warn: 80
    cpu_crit: 95
    ...

servers:
  - name: notebook-9dde
    ssh_alias: ZCode-notebook-9dde   # 方式一: 复用 ~/.ssh/config
    enabled: true

  - name: my-server                 # 方式二: 显式填写
    host: 1.2.3.4
    port: 22
    user: root
    key_file: C:/Users/linxb/.ssh/id_rsa   # 或 password: "xxx"
```

- `enabled: false` 的服务器不监控；`demo: true` 的条目用模拟数据预览面板（不连真实服务器）
- 修改配置后重启 exe / `python widget.py` / `python app.py` 生效

## 目录结构

```
server-monitor/
├── ServerMonitor.exe       # 打包好的悬浮球(双击即用, 需与 config.yaml 同目录)
├── widget.py               # 源码入口: 桌面悬浮卡片(PyQt5)
├── app.py                  # 源码入口: 网页版面板(Flask)
├── config.yaml             # 服务器列表与阈值
├── build.bat               # 重新打包 exe
├── run.bat                 # 源码方式一键启动
├── requirements.txt
├── monitor/
│   ├── collector.py        # SSH 采集 + npu-smi/nvidia-smi 解析
│   ├── poller.py           # 采集调度(悬浮球/网页共用)
│   ├── store.py            # 历史/告警(内存态)
│   ├── web.py              # Flask 路由
│   └── static/index.html   # 网页面板页面(无外部依赖)
└── samples/                # 实机 npu-smi 输出样例(解析器测试用)
```

## 说明与常见问题

- **目标机要求**：Linux（指标取自 `/proc`、`df`、`npu-smi`、`nvidia-smi`，全部只读命令）。
  容器环境（如 ModelArts）直接可用；`nvidia-smi`/`npu-smi` 不存在时对应板块自动隐藏。
- **密钥带口令**：`~/.ssh/config` 方式依赖密钥可免口令加载；有口令请改用显式 `key_file`+`password` 写法。
- **exe 运行**：单文件无需安装 Python; 内含 PyQt5 + Flask + paramiko。打包时已剔除 conda 环境误带的
  MKL/PySide6/numpy 等大依赖, 并禁用 OpenSSL legacy provider
  (conda 版 cryptography 在打包后加载 legacy provider 会报错, paramiko 只用现代算法不受影响)。
- **悬浮窗找不到了**：看任务栏右下角托盘(可能在"^"里), 双击托盘图标或右键 → "显示悬浮球"。
- **局域网访问**：把 `listen_host` 改为 `0.0.0.0`，注意面板无登录认证，不要暴露到公网。
- **数据保存在内存**：重启后历史趋势从头累计（保留最近 180 个点）。需要长期历史可后续接 SQLite/InfluxDB。
- 172.22.11.55 当前连接超时（可能关机），启用后会显示为"离线"并给出错误原因。

## 可扩展方向

告警推送（邮件/钉钉/企微 webhook）、指标入库长期存储、多用户访问认证、进程级 NPU 占用排行。
