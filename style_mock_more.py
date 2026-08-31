"""第二组悬浮卡片视觉样稿: G-N 八种方案。只生成图片, 不修改正式界面。"""
import os
os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")
import sys

from PyQt5.QtCore import Qt, QPoint, QRect, QRectF
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QApplication

from style_mock import build_state, servers_of
from widget import BallWidget, LEVEL, C_TXT, C_MUT, C_DIM, C_OK, C_WARN, C_CRIT, C_DOWN


def metrics(s):
    cpu = s.get("cpu_percent")
    chips = (s.get("accel") or {}).get("items") or []
    vals = [c.get("util") for c in chips if c.get("util") is not None]
    npu = round(sum(vals) / len(vals)) if vals else None
    mem_used = sum(c.get("mem_used_gb") or 0 for c in chips)
    mem_total = sum(c.get("mem_total_gb") or 0 for c in chips)
    return cpu, npu, mem_used, mem_total


def fit(font, text, width):
    return QFontMetrics(font).elidedText(text, Qt.ElideRight, width)


def bar(p, x, y, w, pct, color, track, height=5):
    p.setPen(Qt.NoPen)
    p.setBrush(track)
    p.drawRoundedRect(QRectF(x, y, w, height), height / 2, height / 2)
    if pct is not None:
        p.setBrush(color)
        p.drawRoundedRect(QRectF(x, y, max(3, w * max(0, min(100, pct)) / 100), height),
                          height / 2, height / 2)


def header(p, w, title, color, bg=None):
    if bg is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRect(QRectF(0, 0, w, 22))
    p.setPen(color)
    p.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
    p.drawText(QRect(10, 3, 120, 16), Qt.AlignLeft | Qt.AlignVCenter, title)


def _terminal(self, p):
    w, h = self.width(), self.height()
    bg = QColor(4, 14, 10)
    green = QColor(90, 255, 160)
    dim = QColor(50, 130, 88)
    p.setPen(Qt.NoPen); p.setBrush(bg)
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 5, 5)
    # 轻微扫描线
    p.setPen(QPen(QColor(40, 110, 72, 28), 1))
    for yy in range(22, h, 4): p.drawLine(4, yy, w - 4, yy)
    mono = QFont("Consolas", 8)
    p.setFont(mono); p.setPen(green)
    p.drawText(QRect(8, 3, 150, 16), Qt.AlignLeft | Qt.AlignVCenter, "> SYS_MONITOR")
    p.setPen(dim); p.drawText(QRect(w - 48, 3, 40, 16), Qt.AlignRight | Qt.AlignVCenter, "LIVE")
    for i, s in enumerate(servers_of(self)):
        y = 23 + i * 34
        cpu, npu, _, _ = metrics(s)
        online = s.get("online")
        st = s.get("status") or "down"
        token = {"ok":"OK", "warn":"WRN", "crit":"ERR", "down":"OFF"}.get(st, "---")
        col = green if st == "ok" else QColor(255, 210, 70) if st == "warn" else QColor(255, 90, 90)
        p.setPen(col); p.setFont(mono)
        p.drawText(QRect(8, y, 30, 14), Qt.AlignLeft | Qt.AlignVCenter, f"[{token}]")
        p.setPen(QColor(205, 255, 225))
        p.drawText(QRect(43, y, 100, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(mono, s.get("name", ""), 100))
        p.setPen(col)
        p.drawText(QRect(w - 60, y, 52, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "OFFLINE" if not online else f"C{cpu:.0f}%")
        if online:
            p.setPen(dim)
            ntext = "--" if npu is None else f"NPU {npu:02d}%"
            p.drawText(QRect(43, y + 15, 62, 13), Qt.AlignLeft | Qt.AlignVCenter, ntext)
            bar(p, 110, y + 19, w - 118, npu, green, QColor(20, 48, 34), 4)
        else:
            p.setPen(QColor(255, 90, 90))
            p.drawText(QRect(43, y + 15, w - 51, 13), Qt.AlignLeft | Qt.AlignVCenter, "connection timeout")


def _neon(self, p):
    w, h = self.width(), self.height()
    bg = QColor(7, 8, 26)
    cyan, magenta = QColor(0, 230, 255), QColor(255, 60, 190)
    p.setPen(Qt.NoPen); p.setBrush(bg)
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 12, 12)
    p.setPen(QPen(cyan, 1)); p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 12, 12)
    header(p, w, "NPU // WATCH", cyan)
    p.setPen(QPen(magenta, 1)); p.drawLine(8, 21, w - 8, 21)
    f = QFont("Microsoft YaHei", 8)
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        cpu, npu, _, _ = metrics(s)
        st = s.get("status") or "down"
        col = LEVEL.get(st, C_DOWN)
        glow = QColor(col); glow.setAlpha(55)
        p.setPen(Qt.NoPen); p.setBrush(glow)
        p.drawRoundedRect(QRectF(6, y, w - 12, 29), 6, 6)
        p.setPen(col); p.setFont(f)
        p.drawText(QRect(12, y + 1, 125, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(f, s.get("name", ""), 125))
        p.setPen(cyan if s.get("online") else C_DOWN)
        p.drawText(QRect(w - 48, y + 1, 40, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "OFF" if not s.get("online") else f"{cpu:.0f}%")
        if s.get("online"):
            bar(p, 12, y + 19, 88, cpu, magenta, QColor(34, 20, 55), 5)
            bar(p, 108, y + 19, 64, npu, cyan, QColor(16, 35, 52), 5)
        else:
            p.setPen(C_CRIT); p.drawText(QRect(12, y + 16, w - 20, 12), Qt.AlignLeft, "LINK LOST")


def _industrial(self, p):
    w, h = self.width(), self.height()
    bg, panel = QColor(26, 29, 33), QColor(39, 43, 48)
    amber = QColor(255, 176, 45)
    p.setPen(Qt.NoPen); p.setBrush(bg)
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 4, 4)
    header(p, w, "RESOURCE CONTROL", QColor(235, 238, 240), QColor(18, 20, 23))
    p.setPen(amber); p.drawText(QRect(w - 38, 3, 30, 16), Qt.AlignRight | Qt.AlignVCenter, "AUTO")
    f = QFont("Arial", 8, QFont.Bold)
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        cpu, npu, _, _ = metrics(s)
        st = s.get("status") or "down"
        p.setBrush(panel); p.setPen(QPen(QColor(65, 70, 76), 1))
        p.drawRect(QRectF(7, y, w - 14, 29))
        p.setBrush(LEVEL.get(st, C_DOWN)); p.setPen(Qt.NoPen)
        p.drawRect(QRectF(7, y, 4, 29))
        p.setPen(QColor(225, 228, 232)); p.setFont(f)
        p.drawText(QRect(16, y + 2, 112, 13), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(f, s.get("name", ""), 112))
        p.setPen(amber if s.get("online") else C_DOWN)
        p.drawText(QRect(w - 48, y + 2, 40, 13), Qt.AlignRight | Qt.AlignVCenter,
                   "OFF" if not s.get("online") else f"{cpu:.0f}%")
        if s.get("online"):
            bar(p, 16, y + 19, 86, cpu, amber, QColor(72, 61, 43), 4)
            bar(p, 112, y + 19, 58, npu, QColor(65, 210, 150), QColor(35, 67, 55), 4)
        else:
            p.setPen(C_CRIT); p.drawText(QRect(16, y + 16, w - 24, 12), Qt.AlignLeft, "COMM FAILURE")


def _graphite(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen); p.setBrush(QColor(22, 23, 26))
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 9, 9)
    p.setPen(QPen(QColor(60, 62, 68), 1)); p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 9, 9)
    header(p, w, "SERVERS", QColor(215, 217, 222))
    f = QFont("Microsoft YaHei", 8)
    for i, s in enumerate(servers_of(self)):
        y = 23 + i * 34
        cpu, npu, _, _ = metrics(s)
        online = s.get("online")
        p.setPen(QColor(195, 198, 204) if online else QColor(105, 108, 114)); p.setFont(f)
        p.drawText(QRect(12, y, 126, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(f, s.get("name", ""), 126))
        p.setPen(QColor(240, 242, 245) if online else QColor(120, 123, 130))
        p.drawText(QRect(w - 46, y, 38, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "OFF" if not online else f"{cpu:.0f}%")
        if online:
            # 单色条, 仅严重值用红色提示
            ccol = C_CRIT if cpu is not None and cpu >= 95 else QColor(185, 188, 194)
            bar(p, 12, y + 19, 100, cpu, ccol, QColor(50, 52, 57), 4)
            bar(p, 122, y + 19, 50, npu, QColor(125, 128, 135), QColor(50, 52, 57), 4)
        if i < len(servers_of(self)) - 1:
            p.setPen(QPen(QColor(48, 50, 55), 1)); p.drawLine(10, y + 30, w - 10, y + 30)


def _status_blocks(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen); p.setBrush(QColor(14, 20, 33))
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 10, 10)
    header(p, w, "STATUS BOARD", C_TXT)
    f = QFont("Microsoft YaHei", 8)
    for i, s in enumerate(servers_of(self)):
        y = 23 + i * 34
        cpu, npu, _, _ = metrics(s)
        st = s.get("status") or "down"
        col = LEVEL.get(st, C_DOWN)
        fill = QColor(col); fill.setAlpha(38)
        p.setPen(Qt.NoPen); p.setBrush(fill)
        p.drawRect(QRectF(6, y, w - 12, 29))
        p.setBrush(col); p.drawRect(QRectF(6, y, 5, 29))
        p.setPen(C_TXT); p.setFont(f)
        p.drawText(QRect(17, y + 2, 118, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(f, s.get("name", ""), 118))
        p.setPen(col)
        p.drawText(QRect(w - 46, y + 2, 38, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "OFF" if not s.get("online") else f"{cpu:.0f}%")
        if s.get("online"):
            p.setPen(C_MUT)
            p.drawText(QRect(17, y + 16, 70, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       f"CPU {cpu:.0f}")
            p.drawText(QRect(95, y + 16, 70, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       "NPU --" if npu is None else f"NPU {npu}")
        else:
            p.setPen(C_CRIT); p.drawText(QRect(17, y + 16, w - 25, 12), Qt.AlignLeft, "NO CONNECTION")


def _matrix(self, p):
    w, h = self.width(), self.height()
    p.setPen(Qt.NoPen); p.setBrush(QColor(16, 24, 39))
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 8, 8)
    header(p, w, "SERVER MATRIX", C_TXT)
    f = QFont("Microsoft YaHei", 8)
    cells = servers_of(self)
    cw, ch = (w - 18) / 2, 66
    for i, s in enumerate(cells[:4]):
        r, c = divmod(i, 2)
        x, y = 6 + c * (cw + 6), 24 + r * (ch + 5)
        cpu, npu, _, _ = metrics(s)
        st = s.get("status") or "down"
        col = LEVEL.get(st, C_DOWN)
        bg = QColor(col); bg.setAlpha(24)
        p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 105), 1)); p.setBrush(bg)
        p.drawRoundedRect(QRectF(x, y, cw, ch), 6, 6)
        p.setPen(C_TXT); p.setFont(f)
        p.drawText(QRect(int(x + 6), int(y + 4), int(cw - 12), 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(f, s.get("name", ""), int(cw - 12)))
        p.setPen(col); p.setFont(QFont("Arial", 13, QFont.Bold))
        p.drawText(QRect(int(x + 6), int(y + 20), int(cw - 12), 20), Qt.AlignLeft | Qt.AlignVCenter,
                   "OFF" if not s.get("online") else f"{cpu:.0f}%")
        p.setFont(f); p.setPen(C_DIM)
        p.drawText(QRect(int(x + 6), int(y + 43), int(cw - 12), 14), Qt.AlignLeft | Qt.AlignVCenter,
                   "offline" if not s.get("online") else ("NPU --" if npu is None else f"NPU {npu}%"))


def _digital(self, p):
    w, h = self.width(), self.height()
    bg = QColor(8, 20, 31); aqua = QColor(65, 230, 210)
    p.setPen(Qt.NoPen); p.setBrush(bg)
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 7, 7)
    p.setPen(QPen(QColor(42, 95, 105), 1)); p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 7, 7)
    mono = QFont("Consolas", 8)
    p.setFont(mono); p.setPen(aqua)
    p.drawText(QRect(9, 3, 130, 16), Qt.AlignLeft | Qt.AlignVCenter, "[ RESOURCE NODE ]")
    for i, s in enumerate(servers_of(self)):
        y = 23 + i * 34
        cpu, npu, _, _ = metrics(s)
        st = s.get("status") or "down"
        col = LEVEL.get(st, C_DOWN)
        p.setFont(mono); p.setPen(QColor(180, 225, 225))
        p.drawText(QRect(9, y, 120, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(mono, s.get("name", ""), 120))
        p.setPen(col)
        p.drawText(QRect(w - 54, y, 46, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "--" if not s.get("online") else f"{cpu:05.1f}")
        if s.get("online"):
            # 10段式指示器
            for j in range(10):
                on = cpu is not None and j < round(cpu / 10)
                p.setBrush(col if on else QColor(24, 54, 63))
                p.setPen(Qt.NoPen)
                p.drawRect(QRectF(9 + j * 12, y + 19, 9, 5))
            p.setPen(aqua)
            p.drawText(QRect(133, y + 15, 72, 13), Qt.AlignLeft | Qt.AlignVCenter,
                       "N--" if npu is None else f"N{npu:02d}")
        else:
            p.setPen(C_CRIT); p.drawText(QRect(9, y + 16, 120, 12), Qt.AlignLeft, "LINK_ERR")


def _frosted_dark(self, p):
    w, h = self.width(), self.height()
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0, QColor(25, 39, 67, 235))
    grad.setColorAt(.55, QColor(30, 28, 55, 235))
    grad.setColorAt(1, QColor(17, 35, 48, 235))
    p.setPen(Qt.NoPen); p.setBrush(grad)
    p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 14, 14)
    p.setPen(QPen(QColor(155, 180, 230, 100), 1)); p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 14, 14)
    header(p, w, "服务器 · LIVE", QColor(235, 240, 255))
    f = QFont("Microsoft YaHei", 8)
    for i, s in enumerate(servers_of(self)):
        y = 24 + i * 34
        cpu, npu, _, _ = metrics(s)
        st = s.get("status") or "down"
        col = LEVEL.get(st, C_DOWN)
        p.setBrush(QColor(255, 255, 255, 12)); p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(7, y, w - 14, 29), 7, 7)
        p.setBrush(col); p.drawEllipse(QRectF(13, y + 4, 7, 7))
        p.setPen(QColor(230, 235, 250)); p.setFont(f)
        p.drawText(QRect(25, y + 1, 116, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fit(f, s.get("name", ""), 116))
        p.setPen(col)
        p.drawText(QRect(w - 45, y + 1, 37, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "离线" if not s.get("online") else f"{cpu:.0f}%")
        if s.get("online"):
            bar(p, 13, y + 19, 96, cpu, QColor(90, 150, 255), QColor(255, 255, 255, 25), 4)
            bar(p, 119, y + 19, 52, npu, QColor(55, 220, 160), QColor(255, 255, 255, 25), 4)
        else:
            p.setPen(C_CRIT); p.drawText(QRect(13, y + 16, w - 21, 12), Qt.AlignLeft, "连接失败")


MORE_STYLES = [
    ("G 终端绿屏", _terminal),
    ("H 赛博霓虹", _neon),
    ("I 工业仪表", _industrial),
    ("J 石墨单色", _graphite),
    ("K 状态色块", _status_blocks),
    ("L 双栏矩阵", _matrix),
    ("M 分段数码", _digital),
    ("N 深色玻璃", _frosted_dark),
]


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    state = build_state()
    tiles = []
    for label, fn in MORE_STYLES:
        w = BallWidget(state, {"interval_seconds": 15, "listen_host": "127.0.0.1", "listen_port": 8899}, [])
        w._tick(); w._anchor = QPoint(1200, 700); w._collapse(); app.processEvents()
        pm = QPixmap(w.geometry().width(), w.geometry().height())
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
        fn.__get__(w, BallWidget)(p)
        p.end(); tiles.append((label, pm))

    cols, pad, label_h = 4, 16, 26
    tw = max(pm.width() for _, pm in tiles)
    th = max(pm.height() for _, pm in tiles)
    rows = 2
    canvas = QImage(cols * (tw + pad) + pad, rows * (th + label_h + pad) + pad, QImage.Format_ARGB32)
    canvas.fill(QColor(240, 240, 244))
    p = QPainter(canvas); p.setRenderHint(QPainter.Antialiasing)
    title_font = QFont("Microsoft YaHei", 11, QFont.Bold)
    for idx, (label, pm) in enumerate(tiles):
        r, c = divmod(idx, cols)
        x = pad + c * (tw + pad); y = pad + r * (th + label_h + pad)
        p.setPen(QColor(35, 35, 44)); p.setFont(title_font)
        p.drawText(x, y, tw, label_h - 5, Qt.AlignLeft | Qt.AlignVCenter, label)
        p.drawPixmap(QPoint(x, y + label_h), pm)
    p.end(); canvas.save("samples/style-mock-more.png")
    print("saved samples/style-mock-more.png")


if __name__ == "__main__":
    main()
