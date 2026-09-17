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
        with self.lock:
            # 并发场景下旧样本可能晚到, 不允许覆盖更新的数据
            prev_s = self.current.get(name)
            if prev_s is not None and (s.get("ts") or 0) < (prev_s.get("ts") or 0):
                return
            level, reasons = self._evaluate(s)
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

        def check(label, v, warn, crit):
            nonlocal level
            if v is None:
                return
            if v >= crit:
                level = "crit"
                reasons.append(f"{label} {v}% ≥ {crit}%")
            elif v >= warn and level != "crit":
                level = "warn"
                reasons.append(f"{label} {v}% ≥ {warn}%")

        check("CPU", cpu, self.cpu_warn, self.cpu_crit)
        check("内存", mem, self.mem_warn, self.mem_crit)
        for d in s.get("disks") or []:
            check(f"磁盘{d['mount']}", d.get("percent"), self.disk_warn, self.disk_crit)
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

    def summary(self):
        """轻量摘要: 手机悬浮窗/移动端轮询用, 不含 history/磁盘/进程明细, 控制流量。"""
        with self.lock:
            servers = []
            for name, s in self.current.items():
                accel = s.get("accel") or {}
                chips = accel.get("items") or []
                utils = [c.get("util") for c in chips if c.get("util") is not None]
                mem = s.get("mem") or {}
                st = self.status.get(name, "down" if not s.get("online") else "ok")
                # 主要程序: 进程明细里 CPU 最高的前 1 个
                procs = s.get("processes") or []
                top = procs[0] if procs else None
                servers.append({
                    "name": s.get("name"),
                    "group": s.get("group") or "其他",
                    "online": s.get("online"),
                    "status": st,
                    "error": s.get("error"),
                    "cpu_percent": s.get("cpu_percent"),
                    "mem_percent": mem.get("percent"),
                    "accel_type": accel.get("type"),
                    "accel_count": len(chips),
                    "accel_util": round(sum(utils) / len(utils)) if utils else None,
                    "hbm_used": round(sum(c.get("mem_used_gb") or 0 for c in chips), 1) if chips else None,
                    "hbm_total": round(sum(c.get("mem_total_gb") or 0 for c in chips), 1) if chips else None,
                    "top_process": ({"name": top.get("name"), "cpu_percent": top.get("cpu_percent")}
                                    if top else None),
                    "is_tunnel": s.get("is_tunnel", False),
                })
            counts = {"ok": 0, "warn": 0, "crit": 0, "down": 0}
            for st in self.status.values():
                counts[st] = counts.get(st, 0) + 1
            # 摘要只带最近 5 条告警
            alerts = [{"t": a.get("t"), "name": a.get("name"),
                       "level": a.get("level"), "msg": a.get("msg")}
                      for a in list(self.alerts)[:5]]
        return {"ts": time.time(), "servers": servers, "alerts": alerts, "counts": counts}
