"""悬浮卡片风格样稿渲染: 一次生成 6 种风格拼成对比图。运行后文件保留供参考。"""
import os
os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")
import sys

from PyQt5.QtCore import Qt, QPoint, QRect, QRectF
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen
from PyQt5.QtWidgets import QApplication

from monitor.store import MonitorState
import widget as W
from widget import BallWidget, LEVEL, BAR_COLORS, C_BG, C_LINE, C_TXT, C_MUT, C_DIM, C_OK, C_WARN, C_CRIT, C_DOWN


def online(name, cpu, disk_pct, chips):
    return {"name": name, "label": "ma-user@host:1234", "online": True, "error": None, "ts": 1.0,
            "cpu_percent": cpu, "mem": {"total_gb": 1006, "used_gb": 175, "percent": 17.4},
            "disks": [{"mount": "/home/ma-user/work", "total_gb": 403, "used_gb": 338, "percent": disk_pct},
                      {"mount": "/", "total_gb": 50, "used_gb": 33, "percent": 68}],
            "disks_hidden": 2, "load": [29.8, 30.1, 30.4], "cores": 32, "uptime_days": 229.1,
            "net_rx_kbps": 20.0, "net_tx_kbps": 1272.0,
            "accel": {"type": "NPU", "items": [
                {"id": c, "name": "Ascend910", "health": "OK", "temp_c": 45,
                 "power_w": 295.1 if i == 0 else None, "util": u,
                 "mem_used_gb": mu, "mem_total_gb": 64.0, "proc": "VLLMEngineCor(436739)"}
                for i, (c, u, mu) in enumerate(chips)]}}


def build_state():
    state = MonitorState(60, {})
    state.update(online("notebook-9dde", 5.7, 85, [("1.0", 21, 63.6), ("1.1", 78, 63.9), ("3.0", 25, 63.8), ("3.1", 28, 63.7)]))
    state.update(online("cloudbrain-2gpu", 96.4, 69, [("3.0", 36, 52.8), ("3.1", 84, 52.9)]))
    state.update(online("personal-2gpu", 42.0, 43, [("2.0", 62, 56.2), ("2.1", 85, 56.4)]))
    state.update({"name": "gpu-server-85", "label": "linxb@host:2333", "online": False,
                  "error": "TimeoutError: timed out", "ts": 1.0})
    return state


def servers_of(w):
    return w.snap.get("servers") or []


def _side_tabs(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_BG))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
    # 左侧整体状态色条
    p.setBrush(self._worst_color())
    p.drawRect(QRectF(0.5, 0.5, 4.5, h - 1))
    p.setPen(C_TXT)
    p.setFont(self.f_small)
    p.drawText(QRect(12, 4, 100, 17), Qt.AlignLeft | Qt.AlignVCenter, "服务器监控")
    issues = self.snap.get("counts", {})
    nb = (issues.get("warn") or 0) + (issues.get("crit") or 0)
    if nb:
        p.setPen(C_CRIT if issues.get("crit") else C_WARN)
        p.drawText(QRect(w - 36, 4, 28, 17), Qt.AlignRight | Qt.AlignVCenter, f"!{nb}")
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        st = s.get("status") or "down"
        cpu = s.get("cpu_percent")
        p.setBrush(LEVEL.get(st, C_DOWN))
        p.drawRect(QRectF(12, y + 1, 3.5, 30))   # 每服务器左侧状态条
        p.setPen(C_TXT)
        fm = QFontMetrics(self.f_small)
        p.drawText(QRect(20, y, 122, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(s.get("name", ""), Qt.ElideRight, 122))
        p.setPen(C_CRIT if (cpu is not None and cpu >= 95) else C_WARN if (cpu is not None and cpu >= 80) else C_TXT)
        p.drawText(QRect(w - 42, y, 34, 15), Qt.AlignRight | Qt.AlignVCenter,
                   "离线" if not s.get("online") else ("…" if cpu is None else f"{cpu:.0f}%"))
        if s.get("online"):
            accel = s.get("accel") or {}
            chips = accel.get("items") or []
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = round(sum(utils) / len(utils)) if utils else None
            self._draw_meter(p, 20, y + 18, 92, cpu, "cpu", 80, 95)
            if chips:
                self._draw_meter(p, 120, y + 18, 46, avg, "npu", None, None)
        else:
            p.setPen(C_CRIT)
            p.drawText(QRect(20, y + 16, w - 28, 13), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(f"✕ {s.get('error')}", Qt.ElideMiddle, w - 32))


def _pills(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(20, 28, 47, 250))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 14, 14)
    p.setPen(C_TXT)
    p.setFont(self.f_small)
    p.drawText(QRect(12, 4, 100, 17), Qt.AlignLeft | Qt.AlignVCenter, "服务器监控")
    issues = self.snap.get("counts", {})
    nb = (issues.get("warn") or 0) + (issues.get("crit") or 0)
    if nb:
        bcol = C_CRIT if issues.get("crit") else C_WARN
        pill = QRectF(w - 36, 4, 28, 15)
        fill = QColor(bcol); fill.setAlpha(52)
        p.setBrush(fill); p.setPen(QPen(bcol, 1))
        p.drawRoundedRect(pill, 7, 7)
        p.setPen(bcol); p.drawText(pill, Qt.AlignCenter, f"!{nb}")
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        st = s.get("status") or "down"
        # 整行圆角胶囊, 背景色随状态
        row = QColor(LEVEL.get(st, C_DOWN)); row.setAlpha(22)
        p.setBrush(row)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(6, y, w - 12, 30), 9, 9)
        p.setPen(LEVEL.get(st, C_DOWN))
        p.drawRoundedRect(QRectF(6.5, y + 0.5, w - 13, 29), 9, 9)
        cpu = s.get("cpu_percent")
        fm = QFontMetrics(self.f_small)
        p.setPen(C_TXT)
        p.drawText(QRect(14, y + 2, 120, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(s.get("name", ""), Qt.ElideRight, 120))
        p.setPen(C_CRIT if (cpu is not None and cpu >= 95) else C_WARN if (cpu is not None and cpu >= 80) else C_TXT)
        p.drawText(QRect(w - 48, y + 2, 40, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "离线" if not s.get("online") else ("…" if cpu is None else f"{cpu:.0f}%"))
        if s.get("online"):
            accel = s.get("accel") or {}
            chips = accel.get("items") or []
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = round(sum(utils) / len(utils)) if utils else None
            self._draw_meter(p, 14, y + 19, 100, cpu, "cpu", 80, 95)
            if chips:
                self._draw_meter(p, 124, y + 19, 48, avg, "npu", None, None)
        else:
            p.setPen(C_CRIT)
            p.drawText(QRect(14, y + 16, w - 24, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(f"✕ {s.get('error')}", Qt.ElideMiddle, w - 28))


def _vivid(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_BG))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
    p.setPen(C_TXT)
    p.setFont(self.f_small)
    p.drawText(QRect(10, 4, 100, 17), Qt.AlignLeft | Qt.AlignVCenter, "服务器监控")
    issues = self.snap.get("counts", {})
    nb = (issues.get("warn") or 0) + (issues.get("crit") or 0)
    if nb:
        p.setPen(C_CRIT if issues.get("crit") else C_WARN)
        p.drawText(QRect(w - 36, 4, 28, 17), Qt.AlignRight | Qt.AlignVCenter, f"!{nb}")
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        st = s.get("status") or "down"
        cpu = s.get("cpu_percent")
        fm = QFontMetrics(self.f_small)
        # 名字用状态色, 占比条高饱和
        p.setPen(LEVEL.get(st, C_DOWN))
        p.drawText(QRect(12, y, 124, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(s.get("name", ""), Qt.ElideRight, 124))
        p.setPen(C_CRIT if (cpu is not None and cpu >= 95) else C_WARN if (cpu is not None and cpu >= 80) else C_TXT)
        p.drawText(QRect(w - 42, y, 34, 15), Qt.AlignRight | Qt.AlignVCenter,
                   "离线" if not s.get("online") else ("…" if cpu is None else f"{cpu:.0f}%"))
        if s.get("online"):
            accel = s.get("accel") or {}
            chips = accel.get("items") or []
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = round(sum(utils) / len(utils)) if utils else None
            self._draw_meter(p, 12, y + 18, 96, cpu, "cpu", 80, 95)
            if chips:
                self._draw_meter(p, 118, y + 18, 54, avg, "npu", None, None)
        else:
            p.setPen(C_CRIT)
            p.drawText(QRect(12, y + 16, w - 20, 13), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(f"✕ {s.get('error')}", Qt.ElideMiddle, w - 24))


def _minimal(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_BG))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)
    p.setPen(C_DIM)
    p.setFont(self.f_small)
    p.drawText(QRect(10, 4, 80, 15), Qt.AlignLeft | Qt.AlignVCenter, "监控")
    issues = self.snap.get("counts", {})
    nb = (issues.get("warn") or 0) + (issues.get("crit") or 0)
    if nb:
        p.setPen(C_CRIT if issues.get("crit") else C_WARN)
        p.drawText(QRect(w - 36, 4, 28, 15), Qt.AlignRight | Qt.AlignVCenter, f"!{nb}")
    for i, s in enumerate(servers_of(self)):
        y = 22 + i * 30
        st = s.get("status") or "down"
        cpu = s.get("cpu_percent")
        fm = QFontMetrics(self.f_small)
        # 一整行: 小圆点 + 名字 + CPU + NPU 数字, 单行无占比条
        p.setBrush(LEVEL.get(st, C_DOWN))
        p.drawEllipse(QRectF(10, y + 4, 7, 7))
        p.setPen(C_TXT)
        p.drawText(QRect(22, y, 118, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(s.get("name", ""), Qt.ElideRight, 118))
        if s.get("online"):
            accel = s.get("accel") or {}
            chips = accel.get("items") or []
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = round(sum(utils) / len(utils)) if utils else None
            cpu_txt = "…" if cpu is None else f"{cpu:.0f}%"
            npu_txt = "" if avg is None else f"N{avg}%"
            p.setPen(C_MUT)
            p.drawText(QRect(w - 84, y, 76, 15), Qt.AlignRight | Qt.AlignVCenter,
                       f"{cpu_txt}  {npu_txt}")
        else:
            p.setPen(C_CRIT)
            p.drawText(QRect(w - 84, y, 76, 15), Qt.AlignRight | Qt.AlignVCenter, "离线")
        if i < len(servers_of(self)) - 1:
            p.setPen(QPen(QColor(C_LINE.red(), C_LINE.green(), C_LINE.blue(), 80), 1))
            p.drawLine(10, y + 19, w - 10, y + 19)


def _modern(self, p):
    w, h = self.width(), self.height()
    # 浅色毛玻璃风格
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(238, 242, 250, 242))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)
    p.setPen(QPen(QColor(200, 208, 224), 1))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)
    p.setPen(QColor(30, 38, 58))
    p.setFont(self.f_small)
    p.drawText(QRect(12, 4, 100, 17), Qt.AlignLeft | Qt.AlignVCenter, "服务器监控")
    issues = self.snap.get("counts", {})
    nb = (issues.get("warn") or 0) + (issues.get("crit") or 0)
    if nb:
        p.setPen(QColor(200, 40, 40) if issues.get("crit") else QColor(200, 130, 10))
        p.drawText(QRect(w - 36, 4, 28, 17), Qt.AlignRight | Qt.AlignVCenter, f"!{nb}")
    dark = QColor(30, 38, 58)
    dim = QColor(110, 120, 142)
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        st = s.get("status") or "down"
        cpu = s.get("cpu_percent")
        fm = QFontMetrics(self.f_small)
        col = { "ok": QColor(22, 150, 95), "warn": QColor(200, 130, 10),
                "crit": QColor(200, 40, 40), "down": QColor(120, 130, 150)}[st]
        p.setBrush(col)
        p.drawEllipse(QRectF(12, y + 3.5, 7, 7))
        p.setPen(dark)
        p.drawText(QRect(24, y, 120, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(s.get("name", ""), Qt.ElideRight, 120))
        p.setPen(col)
        p.drawText(QRect(w - 42, y, 34, 15), Qt.AlignRight | Qt.AlignVCenter,
                   "离线" if not s.get("online") else ("…" if cpu is None else f"{cpu:.0f}%"))
        if s.get("online"):
            accel = s.get("accel") or {}
            chips = accel.get("items") or []
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = round(sum(utils) / len(utils)) if utils else None
            # 浅色底上的轨道
            p.setBrush(QColor(222, 228, 240))
            p.drawRoundedRect(QRectF(12, y + 18, 100, 5), 2.5, 2.5)
            if cpu is not None:
                c1, c2 = (QColor(59, 130, 246), QColor(37, 99, 235))
                if cpu >= 95: c1, c2 = (QColor(220, 38, 38), QColor(185, 28, 28))
                elif cpu >= 80: c1, c2 = (QColor(245, 158, 11), QColor(217, 119, 6))
                p.setBrush(c1)
                p.drawRoundedRect(QRectF(12, y + 18, max(4, 100 * cpu / 100), 5), 2.5, 2.5)
            if chips:
                p.setBrush(QColor(222, 228, 240))
                p.drawRoundedRect(QRectF(124, y + 18, 50, 5), 2.5, 2.5)
                if avg is not None:
                    p.setBrush(QColor(16, 150, 110))
                    p.drawRoundedRect(QRectF(124, y + 18, max(4, 50 * avg / 100), 5), 2.5, 2.5)
        else:
            p.setPen(QColor(200, 40, 40))
            p.drawText(QRect(12, y + 16, w - 20, 13), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(f"✕ {s.get('error')}", Qt.ElideMiddle, w - 24))


STYLES = [
    ("A 当前(优化版)", None),                       # 用现有 _paint_card
    ("B 左侧状态条", _side_tabs),
    ("C 胶囊行", _pills),
    ("D 高对比", _vivid),
    ("E 极简单行", _minimal),
    ("F 浅色毛玻璃", _modern),
]


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    state = build_state()

    from PyQt5.QtGui import QImage, QPixmap
    tiles = []
    for label, fn in STYLES:
        w = BallWidget(state, {"interval_seconds": 15, "listen_host": "127.0.0.1", "listen_port": 8899}, [])
        w._tick()
        w._anchor = QPoint(1200, 700)
        if fn is not None:
            w._paint_card = fn.__get__(w, BallWidget)
        w._collapse()
        app.processEvents()
        # 直接画到 QPixmap, 不走 grab()
        pm = QPixmap(w.geometry().width(), w.geometry().height())
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        w._paint_card(p)
        p.end()
        tiles.append((label, pm))

    cols, pad, label_h = 3, 16, 26
    tw = max(t[1].width() for t in tiles)
    th = max(t[1].height() for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    canvas = QImage(cols * (tw + pad) + pad, rows * (th + pad + label_h) + pad,
                    QImage.Format_ARGB32)
    canvas.fill(QColor(240, 240, 244))
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing)
    title_f = QFont("Microsoft YaHei", 11, QFont.Bold)
    for idx, (label, pm) in enumerate(tiles):
        r, c = divmod(idx, cols)
        x = pad + c * (tw + pad)
        y = pad + r * (th + pad + label_h)
        p.setPen(QColor(40, 40, 48))
        p.setFont(title_f)
        p.drawText(x, y, tw, label_h - 6, Qt.AlignLeft | Qt.AlignVCenter, label)
        p.drawPixmap(QPoint(x, y + label_h), pm)
    p.end()
    canvas.save("samples/style-mock.png")
    print("saved samples/style-mock.png")


if __name__ == "__main__":
    main()
