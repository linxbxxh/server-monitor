"""服务器监控 - 桌面悬浮球入口。

无边框置顶小圆球, 贴近屏幕边缘自动缩小隐藏; 鼠标移上去展开状态面板,
移开后自动收起。可拖拽, 托盘常驻, 右键菜单可打开网页版完整面板。

用法:  python widget.py [--config 路径]
"""
import os

# 打包成 exe 后 conda 版 cryptography 加载 OpenSSL legacy provider 会失败;
# paramiko 只用现代算法, 直接禁用(必须在导入 paramiko 之前设置)
os.environ.setdefault("CRYPTOGRAPHY_OPENSSL_NO_LEGACY", "1")

import argparse
import logging
import sys
import threading
import time
import webbrowser

import json
try:
    import winreg
except ImportError:
    winreg = None

# 打包成 exe 后 Qt 平台插件(qwindows.dll)有时加载不到, 手动把插件目录指给 Qt
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    for _sub in ("PyQt5/Qt/plugins", "PyQt5/Qt5/plugins", "PyQt5/Qt5", "PyQt5"):
        _pp = os.path.join(_base, _sub)
        if os.path.isdir(os.path.join(_pp, "platforms")):
            os.environ["QT_PLUGIN_PATH"] = _pp
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(_pp, "platforms")
            break

from PyQt5.QtCore import (QPoint, QRect, QRectF, QPointF, Qt, QTimer,
                          QPropertyAnimation, QEasingCurve, QAbstractAnimation)
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QIcon, QLinearGradient, QPixmap,
                         QPainter, QPen, QBrush, QCursor)
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon, QWidget

from monitor.config import load_config
from monitor.poller import build_collectors, poll_loop
from monitor.store import MonitorState

log = logging.getLogger("widget")

REG_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ServerMonitor"

def is_autostart():
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, APP_NAME)
            return bool(val)
    except Exception:
        return False

def set_autostart(enable: bool):
    if not winreg:
        return
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_RUN) as k:
            if enable:
                exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        log.warning("设置自启动失败: %s", e)

def _settings_path():
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    return os.path.join(base, "user_settings.json")

def load_user_settings():
    path = _settings_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_user_settings(data):
    path = _settings_path()
    try:
        cur = load_user_settings()
        cur.update(data)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("保存设置失败: %s", e)

# ── 主题色(与网页版面板一致) ──────────────────────────────
C_BG = QColor(18, 26, 43)
C_LINE = QColor(42, 56, 90)
C_TXT = QColor(232, 237, 248)
C_MUT = QColor(148, 159, 182)
C_DIM = QColor(104, 116, 140)
C_OK = QColor(52, 211, 153)
C_WARN = QColor(251, 191, 36)
C_CRIT = QColor(248, 113, 113)
C_DOWN = QColor(100, 116, 139)
C_NPU = QColor(52, 211, 153)

# 迷你占比条配色(与网页版一致): kind -> (渐变起, 渐变止)
BAR_COLORS = {
    "cpu": (QColor(59, 130, 246), QColor(96, 165, 250)),
    "mem": (QColor(139, 92, 246), QColor(167, 139, 250)),
    "disk": (QColor(20, 184, 166), QColor(45, 212, 191)),
    "npu": (QColor(16, 185, 129), QColor(52, 211, 153)),
    "hbm": (QColor(236, 72, 153), QColor(244, 114, 182)),
}
BAR_WARN = QColor(217, 119, 6)
BAR_WARN2 = QColor(251, 191, 36)
BAR_CRIT = QColor(220, 38, 38)
BAR_CRIT2 = QColor(248, 113, 113)
BAR_TRACK = QColor(26, 35, 56)

CARD_W = 190         # 收起时窄长条卡片宽度
PANEL_W = 480        # 悬停展开的完整面板宽度
MINI_W, MINI_H = 18, 76   # 贴边细条的固定尺寸(醒目精致胶囊)
LEVEL = {"ok": C_OK, "warn": C_WARN, "crit": C_CRIT, "down": C_DOWN}
LEVEL_TXT = {"ok": "正常", "warn": "警告", "crit": "严重", "down": "离线"}

# ── 悬浮卡片外观主题 ──────────────────────────────────────
# 每套主题包含色板与布局类型, 切换后收起卡片和展开面板同步变化
THEMES = {
    "glass": {"label": "深色玻璃", "shape": "round", "glow": False,
              "bg": [(25, 39, 67), (30, 28, 55), (17, 35, 48)], "bg_alpha": (238, 248),
              "border": (155, 180, 230), "border_alpha": (85, 110),
              "txt": (238, 242, 255), "dim": (165, 178, 205), "hbm": (222, 205, 230),
              "track": (255, 255, 255, 32), "row": (255, 255, 255, 19),
              "cpu": (88, 148, 252, 132, 186, 255), "acc": (43, 203, 148, 80, 232, 178)},
    "night": {"label": "暗夜极简", "shape": "flat", "glow": False,
              "bg": [(10, 12, 18), (10, 12, 18), (10, 12, 18)], "bg_alpha": (245, 252),
              "border": (50, 56, 72), "border_alpha": (140, 170),
              "txt": (225, 230, 240), "dim": (130, 140, 160), "hbm": (200, 195, 215),
              "track": (255, 255, 255, 18), "row": (255, 255, 255, 7),
              "cpu": (96, 125, 200, 130, 160, 220), "acc": (60, 175, 135, 90, 205, 165)},
    "light": {"label": "浅色玻璃", "shape": "round", "glow": False,
              "bg": [(240, 244, 252), (232, 238, 248), (236, 240, 250)], "bg_alpha": (240, 247),
              "border": (180, 190, 215), "border_alpha": (160, 200),
              "txt": (30, 40, 60), "dim": (105, 118, 142), "hbm": (120, 90, 160),
              "track": (30, 45, 80, 22), "row": (255, 255, 255, 90),
              "cpu": (59, 130, 246, 37, 99, 235), "acc": (16, 150, 110, 5, 120, 90)},
    "neon": {"label": "赛博霓虹", "shape": "round", "glow": True,
             "bg": [(12, 6, 28), (24, 8, 40), (8, 4, 22)], "bg_alpha": (240, 250),
             "border": (120, 240, 255), "border_alpha": (120, 170),
             "txt": (210, 250, 255), "dim": (140, 190, 220), "hbm": (255, 150, 240),
             "track": (255, 255, 255, 22), "row": (90, 220, 255, 12),
             "cpu": (0, 220, 255, 90, 250, 255), "acc": (255, 60, 220, 255, 130, 240)},
    "term": {"label": "终端绿屏", "shape": "flat", "glow": False,
             "bg": [(4, 18, 10), (6, 24, 13), (3, 14, 8)], "bg_alpha": (245, 252),
             "border": (40, 160, 90), "border_alpha": (130, 170),
             "txt": (170, 245, 195), "dim": (95, 165, 120), "hbm": (190, 240, 170),
             "track": (120, 255, 170, 20), "row": (120, 255, 170, 9),
             "cpu": (60, 220, 130, 120, 250, 170), "acc": (40, 200, 110, 90, 235, 150)},
    "vivid": {"label": "高对比", "shape": "flat", "glow": False,
              "bg": [(16, 18, 26), (16, 18, 26), (16, 18, 26)], "bg_alpha": (250, 255),
              "border": (90, 100, 130), "border_alpha": (160, 200),
              "txt": (255, 255, 255), "dim": (170, 178, 196), "hbm": (255, 205, 235),
              "track": (255, 255, 255, 28), "row": (255, 255, 255, 10),
              "cpu": (70, 160, 255, 120, 200, 255), "acc": (50, 230, 150, 110, 250, 190)},
}
THEME_ORDER = ["glass", "night", "light", "neon", "term", "vivid"]


def fmt_gb(v):
    if v is None:
        return "—"
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


class BallWidget(QWidget):
    def __init__(self, state, settings, collectors):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)   # 防止系统先擦背景造成闪烁
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        self.state = state
        self.settings = settings
        self._collectors = collectors
        self.snap = {"servers": [], "counts": {}, "alerts": [], "ts": None}
        self.expanded = False
        self.pinned = False
        self.docked = False
        self.mini = False
        self.dragging = False
        self._drag_off = QPoint(0, 0)
        self._anchor = QPoint(0, 0)      # 收起时小方块的位置
        self._hover_token = 0
        self._web_running = False        # Web 面板已在运行
        self._web_starting = False       # Web 面板正在启动
        self._updated = None
        self._last_card_sig = None        # 小方块内容签名: 没变化就不重绘(防闪烁)
        self._panel_sig = None
        self._user_cfg = load_user_settings()
        self._theme = self._user_cfg.get("theme", "glass")   # 当前外观主题(右键菜单可切换, 记忆)
        self._dock_edge = self._user_cfg.get("edge", "right") # 当前停靠边: left 或 right
        self._anim = None                # 位置/尺寸动画对象

        self.f_base = QFont("Microsoft YaHei", 10)
        self.f_bold = QFont("Microsoft YaHei", 10, QFont.Bold)
        self.f_small = QFont("Microsoft YaHei", 9)
        self.f_tiny = QFont("Microsoft YaHei", 8)
        self.f_title = QFont("Microsoft YaHei", 11, QFont.Bold)

        self._pin_rect = QRect(PANEL_W - 70, 10, 58, 21)
        self._card_pin_rect = QRect(0, 0, 22, 15)   # 卡片右上角锁定按钮(随宽度调整)
        self._lines_cache = None

        self._build_menu()          # 先建主菜单(内含外观子菜单和 _theme_actions)
        self._tray = self._make_tray()

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._tick)
        self._ui_timer.start(1500)

    # ── 启动位置: 记忆位置优先, 否则贴屏幕右侧垂直居中 ──
    def showEvent(self, e):
        super().showEvent(e)
        if self._anchor == QPoint(0, 0):
            saved = self._user_cfg.get("anchor")
            self._dock_edge = self._user_cfg.get("edge", "right")
            ok = False
            if isinstance(saved, (list, tuple)) and len(saved) == 2:
                cand = QPoint(int(saved[0]), int(saved[1]))
                for scr in QApplication.screens():
                    if scr.availableGeometry().contains(cand):
                        self._anchor = cand
                        ok = True
                        break
            if not ok:
                avail = QApplication.primaryScreen().availableGeometry()
                self._dock_edge = "right"
                self._anchor = QPoint(avail.right() - CARD_W + 1,
                                      avail.top() + max(8, (avail.height() - self._card_height()) // 2))
            # 确保 _anchor 贴合在所在屏幕的有效边缘
            avail = self._screen_at(self._anchor).availableGeometry()
            if self._dock_edge == "left":
                ax = avail.left()
            else:
                ax = avail.right() - CARD_W + 1
            ay = max(avail.top() + 8, min(self._anchor.y(), avail.bottom() - self._card_height() - 8))
            self._anchor = QPoint(ax, ay)

            # 启动时以完整小卡片亮相, 避免直接缩成细条让用户以为"启动消失了"
            self.setGeometry(self._anchor.x(), self._anchor.y(), CARD_W, self._card_height())
            self.show()
            if not self.pinned:
                token = self._hover_token
                QTimer.singleShot(2500, lambda: self._delayed_collapse(token))
            if os.environ.get("WIDGET_DEBUG_EXPAND"):  # 调试: 直接以展开状态启动
                QTimer.singleShot(300, self._expand)

    # ── 数据刷新 ────────────────────────────────────────
    def _tick(self):
        self.snap = self.state.snapshot()
        self._updated = time.localtime()
        self._lines_cache = None
        self.setToolTip(self._tooltip())
        if self.dragging:
            return   # 拖动时不改几何/重绘, 避免闪烁
        anim_running = (self._anim is not None
                        and self._anim.state() == QAbstractAnimation.Running)
        # 只在内容真正变化时重绘(半透明窗频繁重绘会闪)
        if self.expanded:
            sig = (self.snap.get("ts"), self.pinned, self._theme)
            if sig != self._panel_sig:
                self._panel_sig = sig
                if not anim_running:
                    self._apply_geometry()   # 动画进行时不抢 geometry, 防跳帧
                self.update()
        else:
            if self.mini:
                self.update()   # 细条状态点/在线数要随数据刷新, 只是不改几何
                return
            sig = self._card_sig()
            if sig != self._last_card_sig:
                self._last_card_sig = sig
                # 台数变化时卡片高度也要跟着变, 否则多出的行被裁掉
                want_h = self._card_height()
                if self.height() != want_h:
                    self.setGeometry(self._anchor.x(), self._anchor.y(), CARD_W, want_h)
                self.update()

    def _card_sig(self):
        """小方块内容签名: 数据/时间分钟/停放状态任一变化才重绘。"""
        rows = []
        for s in self.snap.get("servers", []):
            accel = s.get("accel") or {}
            chips = accel.get("items") or []
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = round(sum(utils) / len(utils)) if utils else None
            tot_u = round(sum(c.get("mem_used_gb") or 0 for c in chips), 1)
            tot_t = round(sum(c.get("mem_total_gb") or 0 for c in chips), 1)
            worst_disk = max((d.get("percent") or 0) for d in (s.get("disks") or [{}]))
            rows.append((s.get("name"), s.get("status"), s.get("cpu_percent"), avg, tot_u, tot_t,
                         worst_disk and worst_disk >= 80))
        c = self.snap.get("counts", {})
        return (self.docked, self._theme, time.strftime("%H:%M"),
                (c.get("warn") or 0) + (c.get("crit") or 0), tuple(rows))

    def _tooltip(self):
        c = self.snap.get("counts", {})
        peak = self._peak_cpu()
        lines = [f"在线 {c.get('ok', 0)} · 警告 {c.get('warn', 0)} · 严重 {c.get('crit', 0)} · 离线 {c.get('down', 0)}"]
        for s in self.snap.get("servers", []):
            if s.get("online"):
                lines.append(f"{s['name']}: CPU {s.get('cpu_percent') or 0:.0f}%  {LEVEL_TXT.get(s.get('status'), '')}")
            else:
                lines.append(f"{s['name']}: 离线")
        return f"服务器监控(悬停查看逐芯片详情)\nCPU峰值 {peak}\n" + "\n".join(lines)

    def _peak_cpu(self):
        vals = [s.get("cpu_percent") for s in self.snap.get("servers", [])
                if s.get("online") and s.get("cpu_percent") is not None]
        return f"{max(vals):.0f}%" if vals else "…"

    def _worst_level(self):
        c = self.snap.get("counts", {})
        for k in ("crit", "warn", "down"):
            if c.get(k):
                return k
        return "ok"

    # ── 展开 / 收起 / 贴边(带平滑动画) ───────────────────
    def _show_card(self):
        """从细条/隐藏状态恢复成完整小悬浮框(滑出动画)。
        卡片精准回到用户锚定的 _anchor 位置, 绝不上下漂移或跳屏。"""
        self._hover_token += 1
        self.expanded = False
        self.mini = False
        self._panel_sig = None
        self._last_card_sig = None   # 恢复显示时强制重绘一次
        h = self._card_height()
        avail = self._screen_at(self._anchor).availableGeometry()

        # 卡片依附在细条所在的同一侧边缘
        if getattr(self, "_dock_edge", "right") == "left":
            x = avail.left()
        else:
            x = avail.right() - CARD_W + 1

        y = max(avail.top() + 8, min(self._anchor.y(), avail.bottom() - h - 8))
        self._anchor = QPoint(x, y)
        self._animate_to(x, y, CARD_W, h)
        self.update()

    def _expand(self):
        """展开成大悬浮框(面板), 平滑放大。"""
        self._hover_token += 1
        self.docked = False
        self.mini = False
        self.expanded = True
        self._panel_sig = None
        h = self._panel_height()
        avail = self._screen_at(self._anchor).availableGeometry()
        bx, by = self._anchor.x(), self._anchor.y()
        ch = self._card_height()
        if getattr(self, "_dock_edge", "right") == "left":
            px = avail.left() + 8
        else:
            px = max(avail.left() + 8, min(bx + CARD_W - PANEL_W + 12, avail.right() - PANEL_W - 8))
        py = max(avail.top() + 8, min(by + ch - h, avail.bottom() - h - 8))
        self._animate_to(px, py, PANEL_W, h)
        self.update()

    def _collapse(self):
        """收起成小悬浮框(卡片), 位置和尺寸一起平滑过渡。"""
        self._hover_token += 1
        self.expanded = False
        self._panel_sig = None
        self.mini = False
        h = self._card_height()
        avail = self._screen_at(self._anchor).availableGeometry()
        if getattr(self, "_dock_edge", "right") == "left":
            x = avail.left()
        else:
            x = avail.right() - CARD_W + 1
        y = max(avail.top() + 8, min(self._anchor.y(), avail.bottom() - h - 8))
        self._anchor = QPoint(x, y)
        self._animate_to(x, y, CARD_W, h)
        self.update()

    def _mini(self, animate=True):
        """贴边缩成细条(固定小尺寸), 默认平滑滑入。"""
        self._hover_token += 1
        self.expanded = False
        self._panel_sig = None
        self.mini = True
        self.docked = True
        avail = self._screen_at(self._anchor).availableGeometry()
        h = self._card_height()

        if getattr(self, "_dock_edge", "right") == "left":
            x = avail.left()
        else:
            x = avail.right() - MINI_W + 1

        y = max(avail.top() + 8, min(self._anchor.y() + (h - MINI_H) // 2, avail.bottom() - MINI_H - 8))
        if animate:
            self._animate_to(x, y, MINI_W, MINI_H)
        else:
            self.setGeometry(x, y, MINI_W, MINI_H)
        self._save_position()
        self.update()

    def _apply_geometry(self):
        h = self._panel_height()
        avail = self._screen_at(self._anchor).availableGeometry()
        bx, by = self._anchor.x(), self._anchor.y()
        ch = self._card_height()
        if getattr(self, "_dock_edge", "right") == "left":
            px = avail.left() + 8
        else:
            px = max(avail.left() + 8, min(bx + CARD_W - PANEL_W + 12, avail.right() - PANEL_W - 8))
        py = max(avail.top() + 8, min(by + ch - h, avail.bottom() - h - 8))
        self.setGeometry(px, py, PANEL_W, h)

    def _dock_to_edge(self):
        """贴边: 方块吸到当前所在屏幕的边缘并略微变淡。"""
        avail = self._screen_at(self._anchor).availableGeometry()
        h = self._card_height()
        if self.mini:
            if getattr(self, "_dock_edge", "right") == "left":
                x = avail.left()
            else:
                x = avail.right() - MINI_W + 1
            y = max(avail.top() + 8, min(self._anchor.y(), avail.bottom() - MINI_H - 8))
            self.setGeometry(x, y, MINI_W, MINI_H)
            self.docked = True
            return
        c = self._anchor + QPoint(CARD_W // 2, h // 2)
        # 判断离当前屏幕左边缘近还是右边缘近
        if abs(c.x() - avail.left()) < abs(avail.right() - c.x()):
            self._dock_edge = "left"
            x = avail.left()
        else:
            self._dock_edge = "right"
            x = avail.right() - CARD_W + 1
        y = max(avail.top() + 8, min(self._anchor.y(), avail.bottom() - h - 8))
        self._anchor = QPoint(x, y)
        self.move(self._anchor)
        self.docked = True
        self.mini = False

    # ── 鼠标交互 ────────────────────────────────────────
    def enterEvent(self, e):
        if self.dragging:
            return
        self._hover_token += 1
        if self.mini:
            self._show_card()

    def leaveEvent(self, e):
        if self.dragging or self.pinned:
            return
        # 隐藏窗口或处于细条状态时不触发收起
        if self.isHidden() or self.mini:
            return
        self._hover_token += 1
        token = self._hover_token
        # 留足 1.2 秒容错缓冲, 让用户从边缘移动光标到卡片按钮有充足时间
        QTimer.singleShot(1200, lambda: self._delayed_collapse(token))

    def _delayed_collapse(self, token):
        if token != self._hover_token or self.dragging or self.pinned:
            return
        # 鼠标其实还在窗口内, 不收起(防止动画过程中 leave 误触发)
        if self.geometry().contains(QCursor.pos()):
            return
        if self.expanded:
            self._collapse()
        elif not self.mini:
            self._mini()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.expanded and self._pin_rect.contains(e.pos()):
                self._set_pinned(not self.pinned)
                self.update()
                return
            if not self.expanded and not self.mini and self._card_pin_rect.contains(e.pos()):
                self._set_pinned(not self.pinned)
                self.update()
                return
            if self.mini:
                self._show_card()
                return
            self.dragging = True
            self._drag_off = e.globalPos() - self.geometry().topLeft()
            self._press_pos = e.globalPos()   # 用于区分点击和拖动

    def mouseMoveEvent(self, e):
        if self.dragging:
            self._hover_token += 1
            self.move(e.globalPos() - self._drag_off)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self.dragging:
            return
        self.dragging = False
        # 点击(没拖动): 切换展开/收起
        if getattr(self, "_press_pos", None) and (e.globalPos() - self._press_pos).manhattanLength() < 5:
            if self.expanded:
                self._collapse()
            else:
                self._expand()
            self._press_pos = None
            return
        self._press_pos = None
        if self.expanded:
            g = self.geometry()
            self._anchor = QPoint(g.right() - CARD_W + 10, g.bottom() - self._card_height() + 10)
        else:
            self._anchor = self.geometry().topLeft()
        self._dock_to_edge()
        if self.expanded:
            self._apply_geometry()
        self._save_position()   # 记忆停靠位置
        self._hover_token += 1  # 拖完原地停留, 不立刻收起

    def _save_position(self):
        """把当前停靠位置、停靠边与主题写入 user_settings.json。"""
        save_user_settings({"anchor": [self._anchor.x(), self._anchor.y()],
                            "edge": getattr(self, "_dock_edge", "right"),
                            "theme": self._theme})

    def _animate_to(self, x, y, w, h, done=None):
        """平滑移动/缩放窗口(220ms OutCubic), 完成后回调 done。"""
        if self._anim is not None:
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None
        g0 = self.geometry()
        start = (g0.x(), g0.y(), g0.width(), g0.height())
        if start == (x, y, w, h):
            if done:
                done()
            return
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(QRect(*start))
        anim.setEndValue(QRect(x, y, w, h))
        anim.finished.connect(self._on_anim_done)
        if done:
            anim.finished.connect(done)
        self._anim = anim
        anim.start()

    def _on_anim_done(self):
        if self._anim is not None:
            self._anim.deleteLater()
            self._anim = None

    def _screen_at(self, pos):
        """返回距离给定点最近的屏幕几何, 用于多屏支持(点落在屏幕间隙时也不跳主屏)。"""
        best, best_d = None, None
        for screen in QApplication.screens():
            avail = screen.availableGeometry()
            if avail.contains(pos):
                return screen
            c = avail.center()
            d = (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2
            if best_d is None or d < best_d:
                best, best_d = screen, d
        return best or QApplication.primaryScreen()

    def contextMenuEvent(self, e):
        self._menu.exec_(e.globalPos())

    # ── 右键菜单 / 托盘 ─────────────────────────────────
    def _build_menu(self):
        self._menu = QMenu()
        self._act_pin = QAction("取消锁定" if self.pinned else "锁定面板", self)
        self._act_pin.triggered.connect(self._toggle_pin)
        act_refresh = QAction("立即刷新", self)
        act_refresh.triggered.connect(self._refresh_now)
        act_web = QAction("在浏览器打开完整面板", self)
        act_web.triggered.connect(self.open_web)
        act_hide = QAction("隐藏悬浮窗(托盘可找回)", self)
        act_hide.triggered.connect(self.hide)
        self._act_autostart = QAction("开机自启动", self)
        self._act_autostart.setCheckable(True)
        self._act_autostart.setChecked(is_autostart())
        self._act_autostart.triggered.connect(self._toggle_autostart)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(QApplication.instance().quit)
        for a in (self._act_pin, act_refresh, act_web, act_hide):
            self._menu.addAction(a)
        self._menu.addSeparator()
        self._menu.addMenu(self._build_theme_menu())
        self._menu.addAction(self._act_autostart)
        self._menu.addSeparator()
        self._menu.addAction(act_quit)

    def _toggle_autostart(self):
        enable = self._act_autostart.isChecked()
        set_autostart(enable)
        self._act_autostart.setChecked(is_autostart())   # 以实际注册表状态为准

    def _toggle_pin(self):
        self._set_pinned(not self.pinned)

    def _set_pinned(self, v):
        """所有入口(面板按钮/右键菜单)统一走这里, 保证菜单文字同步。"""
        self.pinned = v
        if hasattr(self, "_act_pin"):
            self._act_pin.setText("取消锁定" if v else "锁定面板")
        self.update()

    def _set_theme(self, key):
        if key not in THEMES or key == self._theme:
            return
        self._theme = key
        for k, act in self._theme_actions:   # 同步所有菜单里的勾选状态
            act.setChecked(k == key)
        self._last_card_sig = None   # 强制重绘
        self._panel_sig = None
        self._save_position()        # 主题也记忆
        self.update()

    def _build_theme_menu(self):
        if not hasattr(self, "_theme_actions"):
            self._theme_actions = []
        sub = QMenu("切换外观", self)
        for key in THEME_ORDER:
            act = QAction(THEMES[key]["label"], self)
            act.setCheckable(True)
            act.setChecked(key == self._theme)
            act.triggered.connect(lambda checked=False, k=key: self._set_theme(k))
            sub.addAction(act)
            self._theme_actions.append((key, act))
        return sub

    def _refresh_now(self):
        # 防重入: 上一轮手动刷新没结束时忽略再次点击, 避免与轮询并发采集
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True

        def run():
            try:
                for col in self._collectors:
                    self.state.update(col.sample())
            finally:
                self._refreshing = False
        threading.Thread(target=run, daemon=True).start()

    def open_web(self):
        url = f"http://127.0.0.1:{self.settings['listen_port']}/"
        if getattr(self, "_web_running", False):     # 已在运行, 直接打开
            webbrowser.open(url)
            return
        if getattr(self, "_web_starting", False):    # 正在启动, 避免重复起 Flask
            return
        self._web_starting = True

        def run():
            try:
                from monitor.web import create_app
                app = create_app(self.state, self.settings)
                host, port = self.settings["listen_host"], self.settings["listen_port"]
                threading.Thread(
                    target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
                    daemon=True).start()
                if self._wait_port(port, 10):
                    self._web_running = True
                    webbrowser.open(url)
                else:
                    log.error("Web 面板启动失败(端口 %s 一直无响应), 可稍后重试", port)
            except Exception as exc:
                log.error("Web 面板启动异常: %s", exc)
            finally:
                self._web_starting = False
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _wait_port(port, timeout_s):
        """轮询直到本机端口可连接(避免浏览器打开时 Flask 还没监听)。"""
        import socket
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    def _make_tray(self):
        icon = self._tray_icon_pixmap()
        tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        act_show = QAction("显示悬浮窗", self)
        act_show.triggered.connect(self._restore_from_tray)
        act_web = QAction("在浏览器打开完整面板", self)
        act_web.triggered.connect(self.open_web)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(QApplication.instance().quit)
        for a in (act_show, act_web):
            menu.addAction(a)
        menu.addSeparator()
        menu.addMenu(self._build_theme_menu())
        menu.addAction(self._act_autostart)   # 复用同一个 QAction, 两处勾选状态天然同步
        menu.addSeparator()
        menu.addAction(act_quit)
        tray.setContextMenu(menu)
        tray.setToolTip("服务器监控")
        tray.activated.connect(lambda r: self._restore_from_tray() if r == QSystemTrayIcon.DoubleClick else None)
        tray.show()
        return tray

    def _restore_from_tray(self):
        self.show()
        self._show_card()

    def _tray_icon_pixmap(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        # 深色圆角方块底
        bg = QColor(30, 36, 52)
        p.setBrush(bg)
        p.setPen(QPen(QColor(80, 90, 120), 1))
        p.drawRoundedRect(QRectF(2, 2, 28, 28), 6, 6)
        # 服务器机架: 3 层白色横条, 每条左侧带状态点
        accent = self._worst_color()
        for i in range(3):
            ry = 7 + i * 8
            # 机架条
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(210, 220, 240, 220))
            p.drawRoundedRect(QRectF(6, ry, 20, 5), 1.5, 1.5)
            # 状态点
            p.setBrush(accent if i == 0 else QColor(120, 200, 130))
            p.drawEllipse(QRectF(8, ry + 1.5, 2, 2))
        p.end()
        return QIcon(pm)

    def _worst_color(self):
        return LEVEL.get(self._worst_level(), C_DOWN)

    # ── 面板内容排版(高度计算与绘制共用) ─────────────────
    def _server_items(self, s):
        """把一台服务器转成绘制条目列表: (kind, ...)。"""
        items = []
        st = s.get("status") or "down"

        # 如果是通道类型: 只展示一行状态与探测摘要(如 HTTP 200 / 进程存活), 不展示虚假的 CPU/内存空条
        if s.get("is_tunnel"):
            msg = s.get("tunnel_msg") or ("正常" if s.get("online") else (s.get("error") or "离线"))
            items.append(("tunnel_row", LEVEL.get(st, C_DOWN), s["name"], msg, LEVEL.get(st, C_DOWN)))
            items.append(("sep",))
            return items

        items.append(("srow", LEVEL.get(st, C_DOWN), s["name"], LEVEL_TXT.get(st, st), LEVEL.get(st, C_DOWN)))
        if not s.get("online"):
            items.append(("err", f"✕ {s.get('error') or '连接失败'}"))
            items.append(("sep",))
            return items
        mem = s.get("mem") or {}
        cpu = s.get("cpu_percent")
        items.append(("meter", "CPU", cpu, "cpu", "…" if cpu is None else f"{cpu:.1f}%", 80, 95))
        items.append(("meter", "内存", mem.get("percent"), "mem",
                      f"{(mem.get('percent') or 0):.0f}% · {fmt_gb(mem.get('used_gb'))}/{fmt_gb(mem.get('total_gb'))}G",
                      85, 95))
        accel = s.get("accel") or {}
        chips = accel.get("items") or []
        if chips:
            is_npu = accel.get("type") == "NPU"
            utils = [c.get("util") for c in chips if c.get("util") is not None]
            avg = f"均{sum(utils) / len(utils):.0f}%" if utils else ""
            tot_u = sum(c.get("mem_used_gb") or 0 for c in chips)
            tot_t = sum(c.get("mem_total_gb") or 0 for c in chips)
            mem_txt = f"{tot_u:.0f}/{tot_t:.0f}G" if tot_t else ""
            head = f"{'NPU·昇腾' if is_npu else 'GPU'} {len(chips)}{'芯片' if is_npu else '卡'}"
            items.append(("npuhead", head, "  ".join(x for x in (avg, mem_txt) if x)))
            tokens = []
            for i, c in enumerate(chips):
                bad = c.get("health") not in (None, "", "OK")
                cid = c.get("id", i)
                tokens.append((f"#{cid}", c.get("util"), bad,
                               c.get("mem_used_gb"), c.get("mem_total_gb")))
            items.append(("chipbar", tokens))
        # 使用情况摘要: 仅展开面板显示，限制行数并由绘制层截断长文本。
        usage = s.get("usage") or {}
        for label, key in (("使用", "status"), ("用户", "users"), ("进程", "processes")):
            value = usage.get(key, s.get(key)) if isinstance(usage, dict) else None
            if value is not None and value != "":
                if isinstance(value, (list, tuple)):
                    if label == "进程":
                        value = ", ".join(
                            f"{x.get('name', '?')}({x.get('pid', '?')}) {x.get('cpu_percent', 0):.0f}%"
                            if isinstance(x, dict) else str(x) for x in value[:4])
                    else:
                        value = ", ".join(str(x) for x in value[:4])
                items.append(("usage", label, str(value)))

        disks = s.get("disks") or []
        if disks:
            worst = max(disks, key=lambda d: d.get("percent") or 0)
            pct = worst.get("percent") or 0
            mark = ""
            if pct >= 90:
                mark = " ⚠"
            elif pct >= 80:
                mark = " ⚠"
            hidden = f" ·另{s['disks_hidden']}个" if s.get("disks_hidden") else ""
            items.append(("meter", "磁盘", pct, "disk",
                          f"{pct:.0f}% · {worst['mount']}{mark}{hidden}", 80, 90))
        items.append(("sep",))
        return items

    def _panel_items(self):
        if self._lines_cache is None:
            items = []
            groups = self._grouped_servers(self.snap.get("servers", []))
            first = True
            for g in groups:
                if not first:
                    items.append(("ghead", g["group"]))
                first = False
                for s in g["items"]:
                    items.extend(self._server_items(s))
            self._lines_cache = items
        return self._lines_cache

    def _chip_rows(self, tokens):
        """芯片条目每行放两个; 每组画两行(算力 + 显存)。"""
        return [tokens[i:i + 2] for i in range(0, len(tokens), 2)]

    def _draw_meter(self, p, x, y, w, pct, kind, warn, crit):
        """迷你占比条: 超过阈值时条色变为黄/红。轨道色随当前主题。"""
        th = THEMES.get(self._theme, THEMES["glass"])
        tr = th["track"]
        track = QColor(tr[0], tr[1], tr[2], tr[3])
        h = 7
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(x, y, w, h), 3.5, 3.5)
        if pct is None:
            return
        pct = max(0.0, min(100.0, pct))
        if pct <= 0:
            return
        if crit is not None and pct >= crit:
            c1, c2 = BAR_CRIT, BAR_CRIT2
        elif warn is not None and pct >= warn:
            c1, c2 = BAR_WARN, BAR_WARN2
        else:
            c1, c2 = BAR_COLORS.get(kind, BAR_COLORS["cpu"])
        fill_w = max(4.0, w * pct / 100.0)
        grad = QLinearGradient(x, y, x + w, y)
        grad.setColorAt(0, c1)
        grad.setColorAt(1, c2)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(x, y, fill_w, h), 3.5, 3.5)

    def _panel_height(self):
        h = 42  # header
        for item in self._panel_items():
            kind = item[0]
            if kind in ("srow", "tunnel_row"):
                h += 25
            elif kind in ("meter", "npuhead", "err", "usage"):
                h += 19
            elif kind == "chipbar":
                h += 38 * len(self._chip_rows(item[1]))
            elif kind == "ghead":
                h += 24
            elif kind == "sep":
                h += 12
        alerts = self.snap.get("alerts") or []
        if alerts:
            h += 6 + 18 * min(2, len(alerts))
        return h + 30  # footer + bottom padding

    # ── 绘制 ────────────────────────────────────────────
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.mini:
            self._paint_mini(p)
        elif self.expanded:
            self._paint_panel(p)
        else:
            self._paint_card(p)

    def _paint_mini(self, p):
        """细条状态: 极简圆角小条, 顶部状态点。"""
        w, h = self.width(), self.height()
        th = THEMES.get(self._theme, THEMES["glass"])
        a_bg = th["bg_alpha"][0] if self.docked else th["bg_alpha"][1]
        def C(rgb, a):
            c = QColor(*rgb); c.setAlpha(a); return c

        worst = self._worst_level()
        dot_col = LEVEL.get(worst, C_OK)

        # 投影 + 圆角条底(更大圆角, 接近参考图)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 100))
        p.drawRoundedRect(QRectF(2, 3.5, w - 1, h - 1), 5, 5)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, C(th["bg"][0], a_bg))
        grad.setColorAt(1, C(th["bg"][2], a_bg))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 5, 5)
        p.setPen(QPen(C(th["border"], th["border_alpha"][0] if self.docked else th["border_alpha"][1]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 5, 5)
        p.setPen(Qt.NoPen)

        # 中轴状态色带(上亮下隐)
        band = QLinearGradient(0, 22, 0, h - 8)
        band.setColorAt(0, QColor(dot_col.red(), dot_col.green(), dot_col.blue(), 180))
        band.setColorAt(1, QColor(dot_col.red(), dot_col.green(), dot_col.blue(), 25))
        p.setBrush(QBrush(band))
        p.drawRoundedRect(QRectF(w / 2 - 1.5, 22, 3, h - 30), 1.5, 1.5)

        # 顶部状态点: 光晕 + 实心点(参考图样式)
        cx, cy = w / 2, 11
        for i, alpha in ((7, 30), (5, 70)):
            hc = QColor(dot_col); hc.setAlpha(alpha)
            p.setBrush(hc)
            p.drawEllipse(QPointF(cx, cy), i, i)
        p.setBrush(dot_col)
        p.drawEllipse(QPointF(cx, cy), 3, 3)

    def _card_height(self):
        servers = self.snap.get("servers") or []
        if not servers:
            return 18 + 30 + 6
        groups = self._grouped_servers(servers)
        n_heads = len(groups) - 1   # 第一组不算头部间隔
        total_h = 18 + n_heads * 22 + 6
        for g in groups:
            items = g["items"]
            if all(s.get("is_tunnel") for s in items):
                total_h += 26   # 通道组合并为一行极简显示
            else:
                total_h += len(items) * 52
        return total_h

    def _grouped_servers(self, servers):
        """按 group 分组并保持配置顺序, 返回 [{group, items}, ...]。"""
        order, seen = [], {}
        for s in servers:
            g = s.get("group") or "其他"
            if g not in seen:
                seen[g] = []
                order.append(g)
            seen[g].append(s)
        return [{"group": g, "items": seen[g]} for g in order]

    def _paint_card(self, p):
        """收起状态: 窄长条卡片, 每行含 CPU/加速占比条与显存/内存。配色取自当前主题。"""
        w, h = self.width(), self.height()
        th = THEMES.get(self._theme, THEMES["glass"])
        a_bg = th["bg_alpha"][0] if self.docked else th["bg_alpha"][1]
        a_txt = 200 if self.docked else 255

        def C(rgb, a):
            c = QColor(*rgb)
            c.setAlpha(a)
            return c

        t_txt = C(th["txt"], a_txt)
        t_dim = C(th["dim"], a_txt)
        t_hbm = C(th["hbm"], a_txt)
        tr = th["track"]
        track = QColor(tr[0], tr[1], tr[2], tr[3] if not self.docked else max(12, tr[3] - 8))

        # 卡片底: 斜向渐变 + 阴影 + 边框 + 顶部高光
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70 if self.docked else 90))
        p.drawRoundedRect(QRectF(1.5, 2, w - 2, h - 1), 13, 13)

        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0, C(th["bg"][0], a_bg))
        grad.setColorAt(.55, C(th["bg"][1], a_bg))
        grad.setColorAt(1, C(th["bg"][2], a_bg))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 13, 13)
        p.setPen(QPen(C(th["border"], th["border_alpha"][0] if self.docked else th["border_alpha"][1]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 13, 13)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 11 if self.docked else 18))
        p.drawRoundedRect(QRectF(1.5, 1, w - 3, 6), 3, 3)

        def gbar(x, y, bw, pct, c1, c2):
            p.setBrush(track)
            p.drawRoundedRect(QRectF(x, y, bw, 4), 2, 2)
            if pct is None or pct <= 0:
                return
            fw = max(3.0, bw * min(100.0, pct) / 100.0)
            g = QLinearGradient(x, y, x + bw, y)
            g.setColorAt(0, c1)
            g.setColorAt(1, c2)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(x, y, fw, 4), 2, 2)

        fm_s = QFontMetrics(self.f_tiny)
        # 标题行
        p.setPen(t_txt)
        p.setFont(self.f_tiny)
        p.drawText(QRect(10, 3, w - 76, 15), Qt.AlignLeft | Qt.AlignVCenter, "服务器 · LIVE")
        # 卡片右上角: 锁定按钮(锁定时不自动缩回细条)
        self._card_pin_rect = QRect(w - 26, 3, 20, 15)
        if self.pinned:
            p.setPen(QPen(C_WARN, 1))
            p.setBrush(QColor(251, 191, 36, 45))
        else:
            p.setPen(QPen(t_dim, 1))
            p.setBrush(QColor(255, 255, 255, 8))
        p.drawRoundedRect(QRectF(self._card_pin_rect), 4, 4)
        # 画一个简约锁形: 锁体(圆角矩形) + 锁钩(半圆弧)
        lock_col = C_WARN if self.pinned else t_dim
        r = self._card_pin_rect
        cx = r.x() + r.width() / 2
        body_y = r.y() + 7
        p.setPen(QPen(lock_col, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(cx - 4, body_y, 8, 6), 1.5, 1.5)      # 锁体
        if self.pinned:
            p.drawArc(QRectF(cx - 3, body_y - 5, 6, 6), 0 * 16, 180 * 16)   # 锁钩闭合
        else:
            p.drawArc(QRectF(cx - 2, body_y - 5, 6, 6), 30 * 16, 180 * 16)  # 锁钩半开(偏移)
        if self._updated:
            p.setPen(t_dim)
            p.drawText(QRect(w - 66, 3, 34, 15), Qt.AlignRight | Qt.AlignVCenter,
                       time.strftime("%H:%M", self._updated))
        issues = self.snap.get("counts", {})
        n_badge = (issues.get("warn") or 0) + (issues.get("crit") or 0)
        if n_badge:
            bcol = C_CRIT if issues.get("crit") else C_WARN
            pill = QRectF(w - 96, 4, 24, 13)
            fill = QColor(bcol); fill.setAlpha(45)
            p.setBrush(fill)
            p.setPen(QPen(bcol, 1))
            p.drawRoundedRect(pill, 7, 7)
            p.setPen(bcol)
            p.drawText(pill, Qt.AlignCenter, f"!{min(n_badge, 9)}")

        servers = self.snap.get("servers") or []
        if not servers:
            p.setPen(t_dim)
            p.drawText(QRect(10, 28, w - 20, 17), Qt.AlignLeft | Qt.AlignVCenter,
                       "暂无服务器")
            return

        cpu_c = th["cpu"]
        acc_c = th["acc"]
        row_a = th["row"][3] if not self.docked else max(6, th["row"][3] - 7)
        rr = 6 if th.get("shape") == "round" else 2      # 行底圆角
        glow = th.get("glow")
        ctx = {"th": th, "t_txt": t_txt, "t_dim": t_dim, "t_hbm": t_hbm,
               "track": track, "fm": fm_s, "cpu_c": cpu_c, "acc_c": acc_c,
               "row_a": row_a, "rr": rr, "glow": glow, "gbar": gbar, "w": w}
        groups = self._grouped_servers(servers)
        y = 18
        first = True
        for g in groups:
            if not first:
                # 分组头: 一条细分隔 + 分组名小字, 留足上下空白
                p.setPen(C(th["border"], 60 if self.docked else 90))
                p.drawLine(QPointF(10, y + 4), QPointF(w - 10, y + 4))
                p.setPen(t_dim)
                p.setFont(self.f_tiny)
                p.drawText(QRect(10, y + 7, w - 20, 14), Qt.AlignLeft | Qt.AlignVCenter, g["group"])
                y += 22
            first = False
            items = g["items"]
            if all(s.get("is_tunnel") for s in items):
                self._row_tunnels(p, items, y, ctx)
                y += 26
            else:
                for s in items:
                    self._row_rows(p, s, y, ctx)
                    y += 52

    def _row_tunnels(self, p, items, y, ctx):
        """通道组单行并排显示: 多个通道并排在一行, 每项一个小圆点 + 简称。"""
        th, w = ctx["th"], ctx["w"]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(th["row"][0], th["row"][1], th["row"][2], ctx["row_a"]))
        p.drawRoundedRect(QRectF(7, y, w - 14, 24), ctx["rr"], ctx["rr"])

        # 名字简称映射
        short_names = {
            "codeg 隧道": "codeg",
            "AgentDock 隧道": "agent",
            "Azure 反向隧道": "azure",
            "Tencent 反向隧道": "tx",
        }
        n = len(items)
        if n == 0:
            return
        slot_w = (w - 20) / n
        p.setFont(self.f_tiny)

        for i, s in enumerate(items):
            x_start = 10 + i * slot_w
            online = s.get("online")
            dot_col = C_OK if online else C_CRIT

            # 状态圆点
            dot_y = y + 12.0
            p.setPen(Qt.NoPen)
            p.setBrush(dot_col)
            p.drawEllipse(QPointF(x_start + 4, dot_y), 2.5, 2.5)

            # 简写文字
            name = short_names.get(s.get("name"), s.get("name", "")[:4])
            text_col = ctx["t_txt"] if online else C_CRIT
            p.setPen(text_col)
            p.drawText(QRect(int(x_start + 11), y + 3, int(slot_w - 11), 18),
                       Qt.AlignLeft | Qt.AlignVCenter, name)

    # ── 卡片服务器行: 状态点 + 名字 + CPU%，下行 CPU/GPU 条 + 显存/内存 ──
    def _row_base(self, p, s, y, ctx, draw_rowbg=True):
        """准备所有布局共用的指标, 同时绘制服务器行背景。"""
        th = ctx["th"]
        st = s.get("status") or "down"
        col = LEVEL.get(st, C_DOWN)
        ROW_H = 50   # 行内容高度(卡片区每行 52px, 留 2px 底部间隔)
        if ctx["glow"]:
            gc = QColor(col)
            gc.setAlpha(70 if self.docked else 110)
            p.setPen(QPen(gc, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(6.5, y - 0.5, ctx["w"] - 13, ROW_H), ctx["rr"], ctx["rr"])
        if draw_rowbg:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(th["row"][0], th["row"][1], th["row"][2], ctx["row_a"]))
            p.drawRoundedRect(QRectF(7, y, ctx["w"] - 14, ROW_H), ctx["rr"], ctx["rr"])
            row_col = QColor(col)
            row_col.setAlpha(165 if self.docked else 220)
            p.setBrush(row_col)
            p.drawRoundedRect(QRectF(7, y + 4, 3, ROW_H - 8), 1.5, 1.5)

        cpu = s.get("cpu_percent")
        mem = s.get("mem") or {}
        mem_pct = mem.get("percent")
        mem_used = mem.get("used_gb")
        mem_total = mem.get("total_gb")
        accel = s.get("accel") or {}
        chips = accel.get("items") or []
        utils = [c.get("util") for c in chips if c.get("util") is not None]
        gpu_avg = round(sum(utils) / len(utils)) if utils else None
        hbm_used = sum(c.get("mem_used_gb") or 0 for c in chips) if chips else None
        hbm_total = sum(c.get("mem_total_gb") or 0 for c in chips) if chips else None
        hbm_pct = (hbm_used / hbm_total * 100) if hbm_used is not None and hbm_total else None
        mem_text = "M —" if mem_pct is None else f"M {mem_pct:.0f}% {mem_used or 0:.0f}/{mem_total or 0:.0f}G"
        gpu_text = "G —" if not chips else (
            f"G {gpu_avg if gpu_avg is not None else '—'}% "
            f"{hbm_used:.0f}/{hbm_total:.0f}G" if hbm_total else
            f"G {gpu_avg if gpu_avg is not None else '—'}%")
        return {"st": st, "col": col, "cpu": cpu, "online": s.get("online"),
                "name": s.get("name", ""), "chips": chips, "gpu_avg": gpu_avg,
                "hbm_used": hbm_used, "hbm_total": hbm_total, "hbm_pct": hbm_pct,
                "mem": mem, "mem_pct": mem_pct, "mem_used": mem_used,
                "mem_total": mem_total, "mem_text": mem_text, "gpu_text": gpu_text,
                "tunnel_msg": s.get("tunnel_msg")}

    def _row_rows(self, p, s, y, ctx):
        """状态点 + 名字 + CPU%，下行 CPU/GPU 条 + 显存/内存文字。"""
        th, fm, w = ctx["th"], ctx["fm"], ctx["w"]
        t_txt, t_dim, t_hbm = ctx["t_txt"], ctx["t_dim"], ctx["t_hbm"]
        d = self._row_base(p, s, y, ctx)
        st, col = d["st"], d["col"]
        # 通道类: 只显示状态点 + 名字 + 探测信息(垂直居中), 不画占比条
        if s.get("is_tunnel"):
            if not d["online"]:
                cy = y + 16
                p.setBrush(C_CRIT)
                if th.get("shape") == "round":
                    p.drawEllipse(QRectF(14, cy, 6, 6))
                else:
                    p.drawRect(QRectF(14, cy, 6, 6))
                p.setPen(t_txt)
                p.setFont(self.f_tiny)
                p.drawText(QRect(24, cy - 6, w - 100, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(d["name"], Qt.ElideRight, w - 100))
                p.setPen(C_CRIT)
                p.drawText(QRect(w - 76, cy - 6, 68, 18), Qt.AlignRight | Qt.AlignVCenter,
                           fm.elidedText("离线", Qt.ElideRight, 68))
            else:
                cy = y + 16
                p.setBrush(col)
                if th.get("shape") == "round":
                    p.drawEllipse(QRectF(14, cy, 6, 6))
                else:
                    p.drawRect(QRectF(14, cy, 6, 6))
                p.setPen(t_txt)
                p.setFont(self.f_tiny)
                p.drawText(QRect(24, cy - 6, w - 100, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(d["name"], Qt.ElideRight, w - 100))
                p.setPen(t_dim)
                p.drawText(QRect(w - 76, cy - 6, 68, 18), Qt.AlignRight | Qt.AlignVCenter,
                           fm.elidedText(d.get("tunnel_msg") or "正常", Qt.ElideRight, 68))
            return
        p.setBrush(col)
        if th.get("shape") == "round":
            p.drawEllipse(QRectF(14, y + 8, 6, 6))
        else:
            p.drawRect(QRectF(14, y + 8, 6, 6))
        p.setPen(t_txt)
        p.setFont(self.f_tiny)
        p.drawText(QRect(24, y + 2, w - 72, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(d["name"], Qt.ElideRight, w - 74))
        if not d["online"]:
            p.setPen(C_CRIT)
            p.drawText(QRect(w - 43, y + 2, 35, 15), Qt.AlignRight | Qt.AlignVCenter, "离线")
            p.drawText(QRect(13, y + 26, w - 24, 13), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(f"✕ {s.get('error') or '连接失败'}", Qt.ElideMiddle, w - 24))
            return
        cpu = d["cpu"]
        p.setPen(col if st in ("warn", "crit") else t_txt)
        p.drawText(QRect(w - 43, y + 2, 35, 15), Qt.AlignRight | Qt.AlignVCenter,
                   "…" if cpu is None else f"{cpu:.0f}%")
        cpu_c, acc_c = ctx["cpu_c"], ctx["acc_c"]
        BAR_X, BAR_W, GAP = 13, 44, 4   # 三条等长占比条
        x1, x2, x3 = BAR_X, BAR_X + BAR_W + GAP, BAR_X + (BAR_W + GAP) * 2
        ctx["gbar"](x1, y + 25, BAR_W, cpu, QColor(*cpu_c[:3]), QColor(*cpu_c[3:]))
        if d["chips"]:
            ctx["gbar"](x2, y + 25, BAR_W, d["gpu_avg"], QColor(*acc_c[:3]), QColor(*acc_c[3:]))
            # 显存占比条(不显文字)
            hb1, hb2 = QColor(222, 205, 230), QColor(200, 175, 220)
            if d["hbm_pct"] is not None and d["hbm_pct"] >= 95:
                hb1, hb2 = QColor(220, 38, 38), QColor(248, 113, 113)
            elif d["hbm_pct"] is not None and d["hbm_pct"] >= 85:
                hb1, hb2 = QColor(217, 119, 6), QColor(251, 191, 36)
            ctx["gbar"](x3, y + 25, BAR_W, d["hbm_pct"], hb1, hb2)
            # 条下方: 三个百分比文字
            p.setFont(self.f_tiny)
            p.setPen(t_dim)
            p.drawText(QRect(x1, y + 33, BAR_W, 12), Qt.AlignCenter, "C …" if cpu is None else f"C {cpu:.0f}%")
            p.setPen(t_hbm)
            p.drawText(QRect(x2, y + 33, BAR_W, 12), Qt.AlignCenter, "G …" if d["gpu_avg"] is None else f"G {d['gpu_avg']:.0f}%")
            p.drawText(QRect(x3, y + 33, BAR_W, 12), Qt.AlignCenter, "M …" if d["hbm_pct"] is None else f"M {d['hbm_pct']:.0f}%")
        else:
            mc1, mc2 = QColor(150, 105, 250), QColor(178, 145, 252)
            if d["mem_pct"] is not None and d["mem_pct"] >= 95:
                mc1, mc2 = QColor(220, 38, 38), QColor(248, 113, 113)
            elif d["mem_pct"] is not None and d["mem_pct"] >= 85:
                mc1, mc2 = QColor(217, 119, 6), QColor(251, 191, 36)
            ctx["gbar"](x2, y + 25, BAR_W, d["mem_pct"], mc1, mc2)
            # 无 GPU 机: 两个百分比 + 内存
            p.setFont(self.f_tiny)
            p.setPen(t_dim)
            p.drawText(QRect(x1, y + 33, BAR_W, 12), Qt.AlignCenter, "C …" if cpu is None else f"C {cpu:.0f}%")
            p.drawText(QRect(x2, y + 33, BAR_W, 12), Qt.AlignCenter, "M …" if d["mem_pct"] is None else f"M {d['mem_pct']:.0f}%")
            p.drawText(QRect(x3, y + 33, BAR_W, 12), Qt.AlignCenter, "用 …" if d["mem_used"] is None else f"用 {d['mem_used']:.0f}G")

    def _paint_panel(self, p):
        w, h = self.width(), self.height()
        th = THEMES.get(self._theme, THEMES["glass"])
        P_TXT = QColor(*th["txt"])
        P_MUT = QColor(*th["dim"])
        P_DIM = QColor(*th["dim"])
        P_DIM.setAlpha(200)
        P_LINE = QColor(*th["border"])
        P_LINE.setAlpha(150)

        shadow = QColor(0, 0, 0, 70)
        p.setPen(Qt.NoPen)
        p.setBrush(shadow)
        p.drawRoundedRect(QRectF(1.5, 2.5, w - 3, h - 3), 13, 13)

        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0, QColor(*th["bg"][0], 247))
        grad.setColorAt(.55, QColor(*th["bg"][1], 247))
        grad.setColorAt(1, QColor(*th["bg"][2], 247))
        p.setBrush(grad)
        p.setPen(QPen(QColor(*th["border"], th["border_alpha"][1]), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)

        # header
        p.setPen(P_TXT)
        p.setFont(self.f_title)
        p.drawText(QRect(18, 7, 190, 27), Qt.AlignLeft | Qt.AlignVCenter, "服务器监控")
        if self._updated:
            p.setPen(P_DIM)
            p.setFont(self.f_small)
            p.drawText(QRect(w - 230, 7, 155, 27), Qt.AlignRight | Qt.AlignVCenter,
                       time.strftime("更新于 %H:%M:%S", self._updated))
        p.setPen(QPen(P_LINE, 1))
        p.drawLine(14, 39, w - 14, 39)
        self._paint_pin(p)

        y = 49
        for item in self._panel_items():
            kind = item[0]
            if kind == "srow":
                _, dot, name, status_txt, st_col = item
                p.setPen(Qt.NoPen)
                p.setBrush(dot)
                p.drawEllipse(QRectF(19, y + 7, 9, 9))
                p.setPen(P_TXT)
                p.setFont(self.f_bold)
                p.drawText(QRect(37, y, 250, 21), Qt.AlignLeft | Qt.AlignVCenter, name)
                p.setPen(st_col)
                p.setFont(self.f_small)
                p.drawText(QRect(w - 86, y, 68, 21), Qt.AlignRight | Qt.AlignVCenter, status_txt)
                y += 25
            elif kind == "tunnel_row":
                _, dot, name, msg, st_col = item
                p.setPen(Qt.NoPen)
                p.setBrush(dot)
                p.drawEllipse(QRectF(19, y + 7, 9, 9))
                p.setPen(P_TXT)
                p.setFont(self.f_bold)
                p.drawText(QRect(37, y, 180, 21), Qt.AlignLeft | Qt.AlignVCenter, name)
                p.setPen(st_col)
                p.setFont(self.f_small)
                fm_msg = QFontMetrics(self.f_small)
                p.drawText(QRect(w - 220, y, 202, 21), Qt.AlignRight | Qt.AlignVCenter,
                           fm_msg.elidedText(msg, Qt.ElideLeft, 202))
                y += 25
            elif kind == "meter":
                _, label, pct, bar_kind, val_txt, warn, crit = item
                p.setPen(P_MUT)
                p.setFont(self.f_small)
                p.drawText(QRect(37, y, 44, 18), Qt.AlignLeft | Qt.AlignVCenter, label)
                self._draw_meter(p, 84, y + 5.5, PANEL_W - 84 - 178, pct, bar_kind, warn, crit)
                vcol = P_TXT
                if pct is not None and warn is not None:
                    if pct >= crit:
                        vcol = C_CRIT
                    elif pct >= warn:
                        vcol = C_WARN
                p.setPen(vcol)
                fm_v = QFontMetrics(self.f_small)
                p.drawText(QRect(w - 176, y, 158, 18), Qt.AlignRight | Qt.AlignVCenter,
                           fm_v.elidedText(val_txt, Qt.ElideMiddle, 158))
                y += 19
            elif kind == "npuhead":
                p.setPen(C_NPU)
                p.setFont(self.f_small)
                p.drawText(QRect(37, y, 240, 18), Qt.AlignLeft | Qt.AlignVCenter, item[1])
                p.setPen(P_DIM)
                p.drawText(QRect(w - 190, y, 172, 18), Qt.AlignRight | Qt.AlignVCenter, item[2])
                y += 19
            elif kind == "chipbar":
                for row in self._chip_rows(item[1]):
                    for i, (label, util, bad, mu, mt) in enumerate(row):
                        x = 37 + i * 213
                        col = C_CRIT if bad else C_NPU
                        p.setPen(col)
                        p.setFont(self.f_small)
                        p.drawText(QRect(x, y, 48, 18), Qt.AlignLeft | Qt.AlignVCenter, label)
                        self._draw_meter(p, x + 52, y + 5.5, 100, util, "npu", None, None)
                        p.setPen(C_CRIT if bad else P_TXT)
                        p.drawText(QRect(x + 158, y, 44, 18), Qt.AlignRight | Qt.AlignVCenter,
                                   "…" if util is None else f"{util}%")
                        # 第二行: HBM 显存占比
                        hbm_pct = (mu / mt * 100) if (mu is not None and mt) else None
                        self._draw_meter(p, x + 52, y + 24.5, 100, hbm_pct, "hbm", None, None)
                        p.setPen(P_DIM)
                        short = lambda v: "—" if v is None else (f"{v:.0f}" if v >= 10 else f"{v:.1f}")
                        p.drawText(QRect(x + 152, y + 19, 68, 18), Qt.AlignRight | Qt.AlignVCenter,
                                   f"{short(mu)}/{short(mt)}G" if mu is not None else "—")
                    y += 38
            elif kind == "usage":
                p.setPen(P_DIM)
                p.setFont(self.f_small)
                fm_u = QFontMetrics(self.f_small)
                p.drawText(QRect(37, y, 44, 18), Qt.AlignLeft | Qt.AlignVCenter, item[1])
                p.drawText(QRect(84, y, w - 102, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           fm_u.elidedText(item[2], Qt.ElideRight, w - 102))
                y += 19
            elif kind == "err":
                p.setPen(C_CRIT)
                p.setFont(self.f_small)
                p.drawText(QRect(37, y, w - 55, 18), Qt.AlignLeft | Qt.AlignVCenter, item[1])
                y += 19
            elif kind == "ghead":
                # 分组头: 分隔线 + 分组名(主题色)
                p.setPen(QPen(P_LINE, 1))
                p.drawLine(14, y + 4, w - 14, y + 4)
                p.setPen(P_TXT)
                p.setFont(self.f_title)
                p.drawText(QRect(18, y + 6, w - 36, 20), Qt.AlignLeft | Qt.AlignVCenter, item[1])
                y += 24
            elif kind == "sep":
                p.setPen(QPen(P_LINE, 1))
                p.drawLine(16, y + 5, w - 16, y + 5)
                y += 12

        alerts = self.snap.get("alerts") or []
        if alerts:
            y += 2
            for a in alerts[:2]:
                p.setPen(LEVEL.get(a.get("level"), P_MUT))
                p.setBrush(LEVEL.get(a.get("level"), P_MUT))
                p.drawEllipse(QRectF(19, y + 6, 7, 7))
                p.setPen(P_MUT)
                msg = f"{a.get('name', '')}  {a.get('msg', '')}"
                p.drawText(QRect(35, y, w - 53, 17), Qt.AlignLeft | Qt.AlignVCenter,
                           QFontMetrics(self.f_small).elidedText(msg, Qt.ElideMiddle, w - 70))
                y += 18

        p.setPen(P_DIM)
        p.setFont(self.f_small)
        interval = self.settings.get("interval_seconds", 15)
        p.drawText(QRect(18, h - 27, w - 36, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   f"每{interval}s经SSH采集 · 右键菜单可锁定/打开网页版")

    def _paint_pin(self, p):
        th = THEMES.get(self._theme, THEMES["glass"])
        dim = QColor(*th["dim"])
        mut = QColor(*th["dim"])
        r = self._pin_rect
        p.setPen(QPen(C_WARN if self.pinned else dim, 1))
        if self.pinned:
            p.setBrush(QColor(251, 191, 36, 40))
        else:
            p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 9, 9)
        p.setPen(C_WARN if self.pinned else mut)
        p.setFont(self.f_small)
        p.drawText(r, Qt.AlignCenter, "已锁定" if self.pinned else "锁定")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="多服务器监控桌面悬浮球")
    ap.add_argument("--config", default=None, help="配置文件路径(默认为项目目录下 config.yaml)")
    args = ap.parse_args()

    settings, servers = load_config(args.config)
    if not servers:
        log.warning("config.yaml 中没有 enabled: true 的服务器")

    state = MonitorState(settings["history_points"], settings["thresholds"])
    collectors = build_collectors(servers, settings["ssh_timeout"], settings.get("process_detail", False))

    stop_event = threading.Event()
    threading.Thread(target=poll_loop, args=(settings, collectors, state, stop_event),
                     daemon=True).start()

    log.info("creating QApplication")
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    qapp = QApplication(sys.argv)
    # 关键保护: 托盘常驻应用禁止在窗口隐藏或切换时自动退出事件循环
    qapp.setQuitOnLastWindowClosed(False)

    def excepthook(exc_type, exc_value, exc_tb):
        import traceback
        err = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical("Uncaught exception:\n%s", err)
        try:
            base_dir = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
            with open(os.path.join(base_dir, "crash.log"), "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {err}\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    w = BallWidget(state, settings, collectors)
    w.show()

    try:
        code = qapp.exec_()
    finally:
        stop_event.set()
    return code


if __name__ == "__main__":
    sys.exit(main())
