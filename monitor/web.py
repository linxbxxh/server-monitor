"""Flask Web 服务: 面板页面 + JSON 接口。"""
import os
import sys
import time

from flask import Flask, jsonify, send_from_directory


def _resource_dir():
    if getattr(sys, "frozen", False):  # PyInstaller 打包: 静态文件解包在 _MEIPASS
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(state, settings):
    static_dir = os.path.join(_resource_dir(), "monitor", "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/status")
    def status():
        data = state.snapshot()
        data["interval"] = settings["interval_seconds"]
        return jsonify(data)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "ts": time.time()})

    return app
