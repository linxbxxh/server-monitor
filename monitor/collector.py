"""通过 SSH 采集 Linux 服务器资源: CPU / 内存 / 磁盘 / 网络 / 负载 / NPU(昇腾) / GPU(NVIDIA)."""
import logging
import os
import re
import subprocess
import threading
import time

import paramiko

try:
    import psutil
except ImportError:  # 本机采集不可用时, LocalCollector 会报离线
    psutil = None

log = logging.getLogger("monitor.collector")

# 一条命令取齐所有指标; CPU 取间隔 1 秒的两次采样以计算利用率
REMOTE_CMD = (
    "echo __CPU_A__; grep '^cpu ' /proc/stat; "
    "echo __CPU_B__; sleep 1; grep '^cpu ' /proc/stat; "
    "echo __MEM__; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
    "echo __LOAD__; cat /proc/loadavg; "
    "echo __UP__; cat /proc/uptime; "
    "echo __CORES__; grep -c '^processor' /proc/cpuinfo; "
    "echo __DISK__; df -kP; "
    "echo __NET__; cat /proc/net/dev; "
    "echo __GPU__; nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,name "
    "--format=csv,noheader,nounits 2>/dev/null; "
    "echo __NPU__; (npu-smi info 2>/dev/null || /usr/local/sbin/npu-smi info 2>/dev/null) | head -200; "
    "echo __PROC__; ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | grep -vE '(^|[[:space:]])(grep|head)[[:space:]]' | head -11; "
    "echo __USERS__; who; "
    "echo __END__"
)

REMOTE_CMD_DETAIL = REMOTE_CMD.replace(
    'echo __PROC__; ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | head -11; ',
    'echo __PROC__; ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | head -11; '
)

NET_EXCLUDE = re.compile(
    r"^(lo|docker.*|br-.*|veth.*|virbr.*|cali.*|flannel.*|cni.*|tun.*|tap.*|wg.*|kube-ipvs.*|dummy.*|sit.*|vnic.*)$")

PSEUDO_FS = {"tmpfs", "devtmpfs", "udev", "none", "shm", "cgroup", "cgroup2",
             "squashfs", "nsfs", "ramfs", "proc", "sysfs", "tracefs", "fusectl", "configfs"}

_NPU_ROW_A = re.compile(r"^\s*([\d.]+|-)\s+(\d+)\s+(\d+)\s*/\s*(\d+)\s*$")
_NPU_ROW_B = re.compile(r"^\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s*$")
_GPU_LINE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+)\s*,(.*)$")


def resolve_ssh_alias(alias):
    """从 ~/.ssh/config 解析 Host 配置; 找不到返回 None。"""
    from paramiko.config import SSHConfig
    path = os.path.join(os.path.expanduser("~"), ".ssh", "config")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        config = SSHConfig.from_file(f)
    if alias not in config.get_hostnames():
        return None
    return config.lookup(alias)


def _sections(output):
    sections, cur = {}, None
    for line in output.splitlines():
        m = re.fullmatch(r"__(\w+)__", line.strip())
        if m:
            cur = m.group(1)
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"
    return sections


def _cpu_times(line):
    vals = [int(x) for x in line.split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def _mem_info(text):
    total = avail = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1])
    if not total:
        return None
    used = total - (avail or 0)
    return {"total_gb": round(total / 1048576, 2), "used_gb": round(used / 1048576, 2),
            "percent": round(used / total * 100, 1)}


def _disk_info(text, min_total_gb=1.0, max_rows=6):
    rows, seen = [], set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        fs, total_kb, used_kb, cap, mount = parts[0], int(parts[1]), int(parts[2]), parts[4], parts[5]
        if os.path.basename(fs) in PSEUDO_FS or fs.startswith("/dev/loop"):
            continue
        if mount.startswith("/snap") or mount == "/boot/efi":
            continue
        if mount in ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf"):  # 容器绑定挂载噪音
            continue
        if total_kb / 1048576 < min_total_gb or fs in seen:
            continue
        seen.add(fs)
        try:
            percent = float(cap.rstrip("%"))
        except ValueError:
            continue
        rows.append({"mount": mount, "total_gb": round(total_kb / 1048576, 1),
                     "used_gb": round(used_kb / 1048576, 1), "percent": percent})
    rows.sort(key=lambda r: -r["percent"])
    return rows[:max_rows], max(0, len(rows) - max_rows)


def _net_totals(text):
    rx = tx = 0
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo" or NET_EXCLUDE.match(iface):
            continue
        f = rest.split()
        if len(f) < 9:
            continue
        try:
            rx += int(f[0])
            tx += int(f[8])
        except ValueError:
            continue
    return rx, tx


def _parse_gpu(text):
    items = []
    for line in text.splitlines():
        m = _GPU_LINE.match(line)
        if not m:
            continue
        idx, util, mu, mt, temp, name = m.groups()
        items.append({"id": idx, "name": name.strip(), "util": int(util),
                      "mem_used_gb": round(int(mu) / 1024, 1), "mem_total_gb": round(int(mt) / 1024, 1),
                      "temp_c": int(temp), "power_w": None, "health": None, "proc": None})
    return items


def _parse_npu(text):
    """解析 npu-smi info 表格 (已按实机 25.2.1 / Ascend910 输出适配, 兼容常见版本差异)。
    输出中进程表位于卡片表之后, 因此先扫一遍进程表、再组装芯片信息。"""
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip("|").split("|")])

    procs = {}
    for cells in rows:  # 进程表行: | NPU Chip | PID | 进程名 | 显存(MB) |
        toks = (cells[0] if cells else "").split()
        if len(cells) == 4 and len(toks) == 2 and toks[1].isdigit() and cells[1].isdigit():
            try:
                procs[(int(toks[0]), int(toks[1]))] = {
                    "pid": int(cells[1]), "name": cells[2], "mem_mb": float(cells[3])}
            except ValueError:
                pass

    chips, pending = [], None
    for cells in rows:
        toks = (cells[0] if cells else "").split()
        if len(cells) != 3 or len(toks) != 2:
            continue

        if toks[1].isdigit():  # 芯片行: | Chip Phy-ID | Bus-Id | AICore% Memory-Usage HBM-Usage |
            if pending is None:
                continue
            m = _NPU_ROW_B.match(cells[2])
            card, pending = pending, None
            if not m:
                continue
            aicore, mu, mt, hu, ht = (int(x) for x in m.groups())
            if mt == 0:  # 910 系列 DDR 计数为 0, 显存以 HBM 为准
                mu, mt = hu, ht
            # toks[0] 是 Chip, toks[1] 是真实的 Phy-ID
            chip_idx, phy_id = int(toks[0]), toks[1]
            p = procs.get((card["npu"], chip_idx))
            chips.append({"id": phy_id, "chip": chip_idx, "npu": card["npu"], "name": card["name"],
                          "health": card["health"], "temp_c": card["temp_c"],
                          "power_w": card["power_w"], "util": aicore,
                          "mem_used_gb": round(mu / 1024, 1), "mem_total_gb": round(mt / 1024, 1),
                          "proc": f'{p["name"]}({p["pid"]})' if p else None})
        else:  # 卡片行: | NPU Name | Health | Power Temp Hugepages |
            m = _NPU_ROW_A.match(cells[2])
            if not m:
                continue
            power_s, temp_s = m.group(1), m.group(2)
            pending = {"npu": int(toks[0]), "name": toks[1], "health": cells[1],
                       "power_w": float(power_s) if power_s != "-" else None,
                       "temp_c": int(temp_s)}
    return chips


class ServerCollector:
    """单台服务器的 SSH 采集器(连接复用, 失败自动重连)。"""

    def __init__(self, entry, ssh_timeout=10, process_detail=False):
        self.name = entry["name"]
        self.group = entry.get("group") or ""
        self.process_detail = entry.get("process_detail", process_detail)
        self.timeout = ssh_timeout
        self._client = None
        self._prev_net = None
        # 可重入锁: 覆盖整个 sample 生命周期(连接/执行/解析/网络基准),
        # 防止手动刷新与周期轮询并发采集同一台服务器
        self._lock = threading.RLock()

        alias = entry.get("ssh_alias")
        if alias:
            info = resolve_ssh_alias(alias)
            if info is None:
                raise RuntimeError(f"~/.ssh/config 中找不到 Host: {alias}")
            self.host = info.get("hostname") or alias
            self.port = int(info.get("port") or 22)
            self.user = info.get("user") or entry.get("user")
            ids = info.get("identityfile") or []
            self.key_file = os.path.expanduser(ids[0]) if ids else entry.get("key_file")
            self.password = info.get("password") or entry.get("password")
            self.proxy_command = info.get("proxycommand")
        else:
            self.host = entry.get("host")
            self.port = int(entry.get("port", 22))
            self.user = entry.get("user")
            self.key_file = os.path.expanduser(entry["key_file"]) if entry.get("key_file") else None
            self.password = entry.get("password")
            self.proxy_command = None
        if not self.host:
            raise RuntimeError("配置缺少 host 或 ssh_alias")
        self.label = f"{self.user or '?'}@{self.host}:{self.port}"

    def sample(self):
        with self._lock:   # 整个采样串行: 旧样本不会晚于新样本写回
            try:
                data = self._collect()
                data.update(name=self.name, label=self.label, group=self.group, online=True, error=None)
                return data
            except Exception as exc:
                self._close()
                return {"name": self.name, "label": getattr(self, "label", self.name), "group": self.group,
                        "online": False, "error": f"{type(exc).__name__}: {exc}", "ts": time.time()}

    def _close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _make_proxy_sock(self):
        """根据 proxycommand 启动子进程, 返回可用的 socket-like 对象。"""
        import subprocess as _sp
        import socket as _socket

        proc = _sp.Popen(self.proxy_command, shell=True,
                         stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))

        # paramiko 的 connect(sock=...) 需要一个有 settimeout() 和 fileno() 的对象;
        # 用一个轻量适配器包装 Popen 的 stdin/stdout
        class _PipeSock:
            def __init__(self, p):
                self._p = p
                self._r = p.stdout
                self._w = p.stdin
                self._closed = False
            def settimeout(self, t):
                pass
            def send(self, data):
                try:
                    self._w.write(data); self._w.flush()
                    return len(data)
                except Exception:
                    return 0
            def recv(self, n):
                try:
                    return self._r.read(n)
                except Exception:
                    return b""
            def close(self):
                self._closed = True
                try:
                    self._p.terminate()
                except Exception:
                    pass
            def fileno(self):
                return self._r.fileno()
        return _PipeSock(proc)

    def _connect(self):
        # paramiko 会读取 HTTP(S)_PROXY/ALL_PROXY 环境变量并让 SSH 走代理,
        # 而本机常年开着 https_proxy=http://127.0.0.1:7890: 经代理连 SSH 会额外握手
        # 甚至被中间设备干扰, 表现为 "Error reading SSH protocol banner"。监控直连
        # 目标机即可, 这里临时清空代理变量(仅影响本次 connect 调用)。
        proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "all_proxy", "no_proxy")
        saved = {k: os.environ.pop(k, None) for k in proxy_vars}
        try:
            client = paramiko.SSHClient()
            # 先加载系统 known_hosts: 已录入指纹的主机会被严格校验,
            # 未录入的新主机才自动信任并录入(TOFU), 防止指纹已变的主机被静默接受
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            sock = None
            if self.proxy_command:
                sock = self._make_proxy_sock()
            # banner/auth 超时给足余量: 跨境链路单次握手可能 >10s(实测 paramiko 直连
            # 海外机需 ~10.4s), 沿用 ssh_timeout 会在网络抖动时抛 "Error reading SSH
            # protocol banner"。连接超时仍用较短值以便快速失败。
            handshake_timeout = max(self.timeout * 3, 30)
            client.connect(self.host, port=self.port, username=self.user,
                           key_filename=self.key_file, password=self.password,
                           timeout=self.timeout, banner_timeout=handshake_timeout,
                           auth_timeout=handshake_timeout, allow_agent=False,
                           look_for_keys=False, sock=sock)
            return client
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def _exec(self):
        if self._client is None:
            self._client = self._connect()
        try:
            _, stdout, _ = self._client.exec_command(REMOTE_CMD if self.process_detail else REMOTE_CMD.replace('echo __PROC__; ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | head -11; ', 'echo __PROC__;'), timeout=self.timeout * 3)
        except (paramiko.SSHException, paramiko.ssh_exception.SSHException, EOFError, ConnectionResetError, OSError):
            # 会话失效, 强制关闭并重建
            self._close()
            self._client = self._connect()
            _, stdout, _ = self._client.exec_command(REMOTE_CMD if self.process_detail else REMOTE_CMD.replace('echo __PROC__; ps -eo pid,user,pcpu,pmem,comm --sort=-pcpu | head -11; ', 'echo __PROC__;'), timeout=self.timeout * 3)
        out = stdout.read().decode("utf-8", "replace")
        stdout.channel.recv_exit_status()
        return out

    def _collect(self):
        out = self._exec()   # 锁由 sample() 持有, 这里不再重复加锁
        s = _sections(out)
        if "END" not in s:
            raise RuntimeError("远程输出不完整(目标机需为 Linux)")
        now = time.time()

        cpu = None
        a = (s.get("CPU_A") or "").strip().splitlines()
        b = (s.get("CPU_B") or "").strip().splitlines()
        if a and b:
            idle1, total1 = _cpu_times(a[0])
            idle2, total2 = _cpu_times(b[0])
            dt = total2 - total1
            if dt > 0:
                cpu = round(max(0.0, min(100.0, (1 - (idle2 - idle1) / dt) * 100)), 1)

        try:
            load = [round(float(x), 2) for x in (s.get("LOAD") or "").split()[:3]]
        except ValueError:
            load = []
        try:
            cores = int((s.get("CORES") or "").strip())
        except ValueError:
            cores = None
        try:
            uptime_days = round(float((s.get("UP") or "").split()[0]) / 86400, 2)
        except (ValueError, IndexError):
            uptime_days = None

        rx, tx = _net_totals(s.get("NET") or "")
        if self._prev_net and now > self._prev_net[0]:
            dt = now - self._prev_net[0]
            rx_rate = max(0, rx - self._prev_net[1]) / dt / 1024
            tx_rate = max(0, tx - self._prev_net[2]) / dt / 1024
        else:
            rx_rate = tx_rate = None
        self._prev_net = (now, rx, tx)

        processes = []
        if self.process_detail:
            for line in (s.get("PROC") or "").splitlines()[1:]:
                p = line.split(None, 4)
                if len(p) == 5:
                    try:
                        proc_name = p[4]
                        if proc_name == "grep" or proc_name == "head" or proc_name.startswith("grep "):
                            continue
                        processes.append({"pid": int(p[0]), "user": p[1], "cpu_percent": float(p[2]), "mem_percent": float(p[3]), "name": proc_name})
                    except ValueError: pass
        users = sorted({line.split()[0] for line in (s.get("USERS") or "").splitlines() if line.split()}) if self.process_detail else []
        gpus = _parse_gpu(s.get("GPU") or "")
        npus = _parse_npu(s.get("NPU") or "")
        if npus:
            accel = {"type": "NPU", "items": npus}
        elif gpus:
            accel = {"type": "GPU", "items": gpus}
        else:
            accel = {"type": None, "items": []}

        disks, disks_hidden = _disk_info(s.get("DISK") or "")
        return {"ts": now, "cpu_percent": cpu, "mem": _mem_info(s.get("MEM") or ""),
                "disks": disks, "disks_hidden": disks_hidden, "load": load, "cores": cores,
                "uptime_days": uptime_days, "usage": {"cpu_percent": cpu, "mem_percent": ( _mem_info(s.get("MEM") or "") or {}).get("percent")}, "processes": processes, "users": users,
                "net_rx_kbps": round(rx_rate, 1) if rx_rate is not None else None,
                "net_tx_kbps": round(tx_rate, 1) if tx_rate is not None else None, "accel": accel}


class LocalCollector:
    """本机采集器(config 中 local: true 时使用): 不经过 SSH, 直接读本机指标。
    CPU/内存/磁盘用 psutil, GPU 优先 nvidia-smi(NVIDIA), 无 NVIDIA 时退化为仅 CPU/内存。"""

    def __init__(self, entry, ssh_timeout=10):
        self.name = entry["name"]
        self.group = entry.get("group") or ""
        self.label = "localhost(本机)"
        if psutil is None:
            raise RuntimeError("未安装 psutil, 无法采集本机指标")
        self._has_nvsmi = self._probe("nvidia-smi")
        psutil.cpu_percent(None)  # 预热, 首次返回 0.0

    @staticmethod
    def _probe(cmd):
        import shutil
        return shutil.which(cmd) is not None

    def _gpu_items(self):
        if not self._has_nvsmi:
            return []
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except Exception:
            return []
        return _parse_gpu(out)

    def sample(self):
        try:
            cpu = round(psutil.cpu_percent(None), 1)  # 与上次调用的时间间隔内的利用率
            vm = psutil.virtual_memory()
            mem = {"total_gb": round(vm.total / 1073741824, 2),
                   "used_gb": round(vm.used / 1073741824, 2),
                   "percent": round(vm.percent, 1)}
            disks = []
            for part in psutil.disk_partitions():
                if "cdrom" in part.opts or part.fstype == "":
                    continue
                try:
                    u = psutil.disk_usage(part.mountpoint)
                except OSError:
                    continue
                if u.total / 1073741824 < 1:
                    continue
                disks.append({"mount": part.mountpoint, "total_gb": round(u.total / 1073741824, 1),
                              "used_gb": round(u.used / 1073741824, 1), "percent": round(u.percent, 1)})
            disks.sort(key=lambda r: -r["percent"])
            gpus = self._gpu_items()
            accel = {"type": "GPU", "items": gpus} if gpus else {"type": None, "items": []}
            return {"name": self.name, "label": self.label, "group": self.group, "online": True, "error": None,
                    "ts": time.time(), "cpu_percent": cpu, "mem": mem,
                    "disks": disks[:6], "disks_hidden": max(0, len(disks) - 6),
                    "load": [], "cores": psutil.cpu_count(), "uptime_days": None,
                    "net_rx_kbps": None, "net_tx_kbps": None, "usage": {"cpu_percent": cpu, "mem_percent": mem["percent"]}, "processes": [], "users": [], "accel": accel}
        except Exception as exc:
            return {"name": self.name, "label": self.label, "group": self.group, "online": False,
                    "error": f"{type(exc).__name__}: {exc}", "ts": time.time()}


class DemoCollector:
    """模拟数据采集器(config 中 demo: true 时使用), 用于离线预览面板。"""

    def __init__(self, entry, ssh_timeout=10):
        import random
        self.name = entry["name"]
        self.group = entry.get("group") or ""
        self.label = "demo@localhost(模拟数据)"
        self.rnd = random.Random(entry["name"])
        self.cpu = self.rnd.uniform(15, 55)
        self.mem = self.rnd.uniform(40, 70)
        self.rx = self.rnd.uniform(100, 900)
        self.util = self.rnd.uniform(0, 60)
        self.disks = [{"mount": "/", "percent": self.rnd.uniform(35, 70),
                       "used_gb": 40.0, "total_gb": 100.0}]

    def _walk(self, v, lo, hi, step):
        return round(max(lo, min(hi, v + self.rnd.uniform(-step, step))), 1)

    def sample(self):
        self.cpu = self._walk(self.cpu, 3, 96, 14)
        self.mem = self._walk(self.mem, 25, 90, 4)
        self.rx = max(0, self._walk(self.rx, 0, 2000, 300))
        self.util = self._walk(self.util, 0, 99, 25)
        accel = {"type": "NPU", "items": []}
        if "db" in self.name:
            accel["items"] = [{"id": "0.0", "name": "Ascend910", "health": "OK",
                               "temp_c": 46, "power_w": round(150 + self.util * 5, 1),
                               "util": round(self.util), "mem_used_gb": round(64 * self.util / 130 + 20, 1),
                               "mem_total_gb": 64.0, "proc": "VLLMEngineCor(436739)"}]
        return {"name": self.name, "label": self.label, "group": self.group, "online": True, "error": None,
                "ts": time.time(), "cpu_percent": self.cpu,
                "mem": {"total_gb": 32.0, "used_gb": round(32 * self.mem / 100, 1), "percent": self.mem},
                "disks": self.disks, "disks_hidden": 0, "load": [round(self.cpu / 100 * 8, 2)] * 3,
                "cores": 8, "uptime_days": 42.5,
                "net_rx_kbps": self.rx, "net_tx_kbps": round(self.rx / 4, 1), "accel": accel}


class TunnelCollector:
    """本机通道/反向隧道健康检查(config 中 tunnel: true 时使用)。

    通过 HTTP 健康检查验证通道可达性, 可选探测 SSH 反向隧道进程是否存活。
    不需要 SSH 连接, 只做轻量探测, 反映"这条对外通道现在通不通"。
    """

    def __init__(self, entry, ssh_timeout=10):
        self.name = entry["name"]
        self.group = entry.get("group") or ""
        self.label = entry.get("label") or entry.get("name")
        self.check_url = entry.get("check_url")          # 本地或公网健康检查地址
        self.expect_code = entry.get("expect_code")      # 期望的 HTTP 状态码(默认 200; agentdock 用 401)
        self.ssh_marker = entry.get("ssh_marker")        # SSH 反向隧道命令行特征串(如 lin_key.pem + 18317)
        self.timeout = 8

    def _http_ok(self):
        if not self.check_url:
            return None, None
        import urllib.request
        try:
            req = urllib.request.Request(self.check_url, headers={"User-Agent": "tunnel-monitor"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                code = r.getcode()
        except urllib.error.HTTPError as e:   # 401/403 也代表通道通了
            code = e.code
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        want = self.expect_code if self.expect_code is not None else 200
        if isinstance(want, int):
            want = [want]
        ok = code in want
        return ok, f"HTTP {code}"

    def _ssh_alive(self):
        if not self.ssh_marker:
            return None
        try:
            import subprocess as sp
            # marker 用不带扩展名的密钥名特征, 如 lin_key / tx_lin
            marker = self.ssh_marker.split(".")[0]
            # wmic 查询 ssh.exe 且命令行含 marker 的进程数, 比 CimInstance 在子进程里更稳
            cmd = f'wmic process where "name=\'ssh.exe\' and CommandLine like \'%{marker}%\'" get ProcessId /format:csv'
            out = sp.run(cmd, capture_output=True, text=True, timeout=self.timeout,
                         errors="replace",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            # wmic csv 输出含表头, 数有内容的行
            lines = [l for l in out.splitlines() if l.strip() and "," in l and "ProcessId" not in l]
            return len(lines) > 0
        except Exception:
            return None   # 探测失败时不影响主判定

    def sample(self):
        http_ok, http_msg = self._http_ok()
        ssh_ok = self._ssh_alive()

        # 判定: 有 check_url 以它为准; 有 ssh_marker 要求进程也存活
        if http_ok is None:      # 只配了 ssh_marker
            online = bool(ssh_ok)
            msg = "SSH 隧道进程存活" if online else "SSH 隧道进程丢失"
        else:
            online = bool(http_ok)
            msg = http_msg
            if online and ssh_ok is False:
                online = False
                msg = f"{http_msg} 但 SSH 隧道进程丢失"
            elif online:
                msg = http_msg + (" + 隧道进程存活" if ssh_ok else "")

        # 通道类不设 cpu/mem 百分比, 避免 _evaluate 把"在线"误判成资源超限
        return {"name": self.name, "label": self.label, "group": self.group,
                "online": online, "error": None if online else msg, "is_tunnel": True,
                "ts": time.time(), "cpu_percent": None,
                "mem": {"total_gb": None, "used_gb": None, "percent": None},
                "disks": [], "disks_hidden": 0, "load": [], "cores": None,
                "uptime_days": None, "net_rx_kbps": None, "net_tx_kbps": None,
                "accel": {"type": None, "items": []},
                "tunnel_msg": msg}
