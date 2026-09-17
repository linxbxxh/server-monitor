"""加载并校验 config.yaml。"""
import os
import sys

import yaml


def _base_dir():
    if getattr(sys, "frozen", False):  # PyInstaller 打包: config.yaml 放在 exe 旁边
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "interval_seconds": 15,
    "history_points": 180,
    "listen_host": "127.0.0.1",
    "listen_port": 8787,
    "ssh_timeout": 10,
    # 进程明细会增加远程命令开销，默认关闭；可在 settings 或单台服务器中开启
    "process_detail": False,
}

DEFAULT_THRESHOLDS = {
    "cpu_warn": 80, "cpu_crit": 95,
    "mem_warn": 85, "mem_crit": 95,
    "disk_warn": 80, "disk_crit": 90,
}


def load_config(path=None):
    path = path or os.path.join(_base_dir(), "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in (raw.get("settings") or {}).items() if v is not None})
    settings["thresholds"] = {**DEFAULT_THRESHOLDS, **(settings.get("thresholds") or {})}

    servers = [dict(s) for s in (raw.get("servers") or [])
               if isinstance(s, dict) and s.get("enabled", True)]
    return settings, servers
