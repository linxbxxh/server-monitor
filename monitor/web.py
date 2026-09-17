"""Flask Web 服务: 面板页面 + JSON 接口。"""
import os
import sys
import time

from flask import Flask, jsonify, request, send_from_directory


def _resource_dir():
    if getattr(sys, "frozen", False):  # PyInstaller 打包: 静态文件解包在 _MEIPASS
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(state, settings):
    static_dir = os.path.join(_resource_dir(), "monitor", "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")

    # 可选的 URL 前缀 (Caddy 反代 /server-monitor/ 时不 strip 路径, 后端需同时
    # 支持 /api/* 与 /server-monitor/api/* 两种前缀)
    prefix = str(settings.get("url_prefix") or "").rstrip("/")

    def _route(rule, **opts):
        def deco(fn):
            app.add_url_rule(prefix + rule, view_func=fn, **opts)
            if prefix:  # 同时注册无前缀版本, 本机访问不受影响
                app.add_url_rule(rule, view_func=fn, **opts)
            return fn
        return deco

    # 手机/远程访问令牌: settings.mobile_token 配置后, /api/* 需携带
    #   Authorization: Bearer <token>   或   ?token=<token>
    # 未配置 token 时不启用鉴权(保持原有本机用法不变)。
    api_token = str(settings.get("mobile_token") or "").strip()

    def _authorized():
        if not api_token:
            return True
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == api_token:
            return True
        if request.args.get("token", "") == api_token:
            return True
        return False

    def _need_auth():
        return jsonify({"error": "unauthorized", "hint": "missing/invalid token"}), 401

    @_route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # 带前缀的静态资源: /server-monitor/static/* -> 静态目录里的 /static/*
    if prefix:
        @app.get(prefix + "/static/<path:fname>")
        def prefixed_static(fname):
            return send_from_directory(app.static_folder, fname)

    @_route("/api/status")
    def status():
        if not _authorized():
            return _need_auth()
        data = state.snapshot()
        data["interval"] = settings["interval_seconds"]
        return jsonify(data)

    @_route("/api/summary")
    def summary():
        """轻量摘要: 手机悬浮窗/App 轮询用(无 history/磁盘/进程明细)。"""
        if not _authorized():
            return _need_auth()
        data = state.summary()
        data["interval"] = settings["interval_seconds"]
        return jsonify(data)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "ts": time.time()})

    return app
