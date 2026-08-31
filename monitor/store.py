"""内存态数据存储: 最新采样 / 历史趋势 / 告警记录。"""
import logging
import threading
import time
from collections import deque

log = logging.getLogger("monitor.store")


class MonitorState:
    def __init__(self, history_points=180, thresholds=None):
        th = thresholds or {}
        self.cpu_warn, self.cpu_crit = th.get("cpu_warn", 80), th.get("cpu_crit", 95)
        self.mem_warn, self.mem_crit = th.get("mem_warn", 85), th.get("mem_crit", 95)
        self.disk_warn, self.disk_crit = th.get("disk_warn", 80), th.get("disk_crit", 90)

        self.lock = threading.Lock()
        self.points = history_points
        self.history = {}   # name -> deque[{t, cpu, mem, rx, tx}]
        self.current = {}   # name -> 最新采样
        self.status = {}    # name -> ok / warn / crit / down
        self.alerts = deque(maxlen=100)

    def update(self, s):
        name = s["name"]
        level, reasons = self._evaluate(s)
        with self.lock:
            prev = self.status.get(name)
            self.status[name] = level
            self.current[name] = s
            h = self.history.setdefault(name, deque(maxlen=self.points))
            h.append({"t": s.get("ts", time.time()), "cpu": s.get("cpu_percent"),
                      "mem": (s.get("mem") or {}).get("percent"),
                      "rx": s.get("net_rx_kbps"), "tx": s.get("net_tx_kbps")})
            if level != prev:
                if level in ("warn", "crit", "down"):
                    self._alert(name, level, reasons or s.get("error"))
                elif prev in ("warn", "crit", "down") and level == "ok":
                    self._alert(name, "ok", None)

    def _evaluate(self, s):
        if not s.get("online"):
            return "down", None
        level = "ok"
        reasons = []
        cpu, mem = s.get("cpu_percent"), (s.get("mem") or {}).get("percent")

        def check(kind, label, v, warn, crit):
            nonlocal level
            if v is None:
                return
            if v >= crit:
                level = "crit"
                reasons.append(f"{label} {v}% ≥ {crit}%")
            elif v >= warn and level != "crit":
                level = "warn"
                reasons.append(f"{label} {v}% ≥ {warn}%")

        check("cpu", "CPU", cpu, self.cpu_warn, self.cpu_crit)
        check("mem", "内存", mem, self.mem_warn, self.mem_crit)
        for d in s.get("disks") or []:
            check("disk", f"磁盘{d['mount']}", d.get("percent"), self.disk_warn, self.disk_crit)
        return level, reasons

    def _alert(self, name, level, reasons):
        if level == "down":
            msg = reasons or "SSH 连接失败"
        elif level == "ok":
            msg = "指标恢复正常"
        else:
            msg = "、".join(reasons) if reasons else "指标超阈值"
        self.alerts.appendleft({"t": time.time(), "name": name, "level": level, "msg": msg})
        log_fn = log.info if level == "ok" else log.warning
        log_fn("[%s] %s: %s", level.upper(), name, msg)

    def snapshot(self):
        with self.lock:
            servers = []
            for name, s in self.current.items():
                item = dict(s)
                item["history"] = list(self.history.get(name) or [])
                item["status"] = self.status.get(name, "down" if not s.get("online") else "ok")
                servers.append(item)
            counts = {"ok": 0, "warn": 0, "crit": 0, "down": 0}
            for st in self.status.values():
                counts[st] = counts.get(st, 0) + 1
            alerts = list(self.alerts)[:30]
        return {"ts": time.time(), "servers": servers, "alerts": alerts, "counts": counts}
