"""服务器监控 - 入口: 后台采集线程 + Web 面板。

用法:  python app.py [--config 路径] [--port 端口]
"""
import os

# 打包成 exe 后 conda 版 cryptography 加载 OpenSSL legacy provider 会失败;
# paramiko 只用现代算法, 直接禁用(必须在导入 paramiko 之前设置)
os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

import argparse
import logging
import threading

from monitor.config import load_config
from monitor.poller import build_collectors, poll_loop
from monitor.store import MonitorState
from monitor.web import create_app

log = logging.getLogger("monitor")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="多服务器资源使用监控面板")
    ap.add_argument("--config", default=None, help="配置文件路径(默认为项目目录下 config.yaml)")
    ap.add_argument("--port", type=int, default=None, help="覆盖面板端口")
    args = ap.parse_args()

    settings, servers = load_config(args.config)
    if args.port:
        settings["listen_port"] = args.port
    if not servers:
        log.warning("config.yaml 中没有 enabled: true 的服务器")

    state = MonitorState(settings["history_points"], settings["thresholds"])
    collectors = build_collectors(servers, settings["ssh_timeout"], settings.get("process_detail", False))

    stop_event = threading.Event()
    threading.Thread(target=poll_loop, args=(settings, collectors, state, stop_event),
                     daemon=True).start()

    host, port = settings["listen_host"], settings["listen_port"]
    show_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    log.info("监控面板已启动: http://%s:%s  (Ctrl+C 退出)", show_host, port)
    app = create_app(state, settings)
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
