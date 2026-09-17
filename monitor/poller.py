"""采集调度: 构建采集器并周期性并行采样, 供 Web 面板与桌面悬浮球两个入口复用。"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from monitor.collector import DemoCollector, LocalCollector, ServerCollector, TunnelCollector

log = logging.getLogger("monitor.poller")


class BrokenCollector:
    """配置有误的服务器: 永远显示为离线并带出错误信息, 避免程序崩溃。"""

    def __init__(self, entry, error):
        self.name = entry.get("name", "?")
        self.group = entry.get("group") or ""
        self.label = entry.get("ssh_alias") or entry.get("host") or ""
        self.error = error

    def sample(self):
        return {"name": self.name, "label": self.label, "group": self.group, "online": False,
                "error": self.error, "ts": time.time()}


def build_collectors(servers, ssh_timeout, process_detail=False):
    collectors = []
    for s in servers:
        try:
            if s.get("local"):
                collectors.append(LocalCollector(s))
            elif s.get("tunnel"):
                collectors.append(TunnelCollector(s))
            elif s.get("demo"):
                collectors.append(DemoCollector(s))
            else:
                collectors.append(ServerCollector(s, ssh_timeout, process_detail))
        except Exception as exc:
            log.error("服务器 %s 配置有误: %s", s.get("name"), exc)
            collectors.append(BrokenCollector(s, str(exc)))
    return collectors


def poll_loop(settings, collectors, state, stop_event):
    interval = settings["interval_seconds"]
    seen_online = set()
    with ThreadPoolExecutor(max_workers=max(4, len(collectors))) as pool:
        while not stop_event.is_set():
            started = time.time()
            for sample in pool.map(lambda c: c.sample(), collectors):
                state.update(sample)
                if sample.get("online") and sample["name"] not in seen_online:
                    seen_online.add(sample["name"])
                    log.info("%s 采集成功 (%s)", sample["name"], sample.get("label"))
            stop_event.wait(max(1, interval - (time.time() - started)))
