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
    "echo __END__"
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

        if toks[1].isdigit():  # 芯片行: | chip phy-id | bus-id | AICore% mem hbm |
            if pending is None:
                continue
            m = _NPU_ROW_B.match(cells[2])
            card, pending = pending, None
            if not m:
                continue
            aicore, mu, mt, hu, ht = (int(x) for x in m.groups())
            if mt == 0:  # 910 系列 DDR 计数为 0, 显存以 HBM 为准
                mu, mt = hu, ht
            p = procs.get((card["npu"], int(toks[0])))
            chips.append({"id": f'{card["npu"]}.{toks[0]}', "name": card["name"],
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

    def __init__(self, entry, ssh_timeout=10):
        self.name = entry["name"]
        self.group = entry.get("group") or ""
        self.timeout = ssh_timeout
        self._client = None
        self._prev_net = None
        self._lock = threading.Lock()

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
        else:
            self.host = entry.get("host")
            self.port = int(entry.get("port", 22))
            self.user = entry.get("user")
            self.key_file = os.path.expanduser(entry["key_file"]) if entry.get("key_file") else None
            self.password = entry.get("password")
        if not self.host:
            raise RuntimeError("配置缺少 host 或 ssh_alias")
        self.label = f"{self.user or '?'}@{self.host}:{self.port}"

    def sample(self):
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

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.host, port=self.port, username=self.user,
                       key_filename=self.key_file, password=self.password,
                       timeout=self.timeout, banner_timeout=self.timeout,
                       auth_timeout=self.timeout, allow_agent=False, look_for_keys=False)
        return client

    def _exec(self):
        if self._client is None:
            self._client = self._connect()
        _, stdout, _ = self._client.exec_command(REMOTE_CMD, timeout=self.timeout * 3)
        out = stdout.read().decode("utf-8", "replace")
        stdout.channel.recv_exit_status()
        return out

    def _collect(self):
        with self._lock:
            out = self._exec()
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
                "uptime_days": uptime_days, "net_rx_kbps": round(rx_rate, 1) if rx_rate is not None else None,
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
                    "net_rx_kbps": None, "net_tx_kbps": None, "accel": accel}
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
