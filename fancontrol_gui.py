#!/usr/bin/env python3
"""
fancontrol-gui - visual dashboard for fancontrold

Two-column layout: temp history + per-core temps on the left,
control panel + throttled processes on the right. Compact stat
row up top. Minimizes to a tray icon.

Requires: PySide6  (pip install PySide6 --break-system-packages)
Requires fancontrold + fanctl backend to already be installed.
"""

import sys
import re
import time
import subprocess
from pathlib import Path
from collections import deque

try:
    import psutil
except ImportError:
    psutil = None

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QIcon, QPixmap, QAction, QPainterPath, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QLineEdit, QSystemTrayIcon, QMenu,
    QFrame, QSizePolicy, QGridLayout, QLayout, QDialog, QListWidget,
    QListWidgetItem
)

CONFIG_PATH = Path("/etc/fancontrold/config.toml")
MODE_FILE = Path("/run/fancontrold/mode")
LEVEL_FILE = Path("/run/fancontrold/level")
FAN_PROC = Path("/proc/acpi/ibm/fan")
HWMON_PATH = Path("/sys/class/hwmon")

BG = "#151521"
CARD = "#1e1e30"
ACCENT = "#7F77DD"
ACCENT_MUTED = "#2c2a4a"
GREEN = "#63A722"
YELLOW = "#EF9F27"
RED = "#E24B4A"
TEXT = "#eceaf5"
TEXT_DIM = "#9a98b5"
TEXT_MUTED = "#6f6d8a"
BORDER = "#2c2c44"

HISTORY_LEN = 150


def find_hwmon_coretemp():
    for hwmon in HWMON_PATH.glob("hwmon*"):
        name_file = hwmon / "name"
        if name_file.exists() and name_file.read_text().strip() == "coretemp":
            return hwmon
    return None


def find_package_temp_input():
    hwmon = find_hwmon_coretemp()
    if not hwmon:
        return None
    for label_file in hwmon.glob("temp*_label"):
        if "Package" in label_file.read_text():
            idx = re.search(r"temp(\d+)_label", label_file.name).group(1)
            return hwmon / f"temp{idx}_input"
    return None


def find_core_temp_inputs():
    hwmon = find_hwmon_coretemp()
    cores = []
    if not hwmon:
        return cores
    for label_file in sorted(hwmon.glob("temp*_label")):
        label = label_file.read_text().strip()
        if label.startswith("Core"):
            idx = re.search(r"temp(\d+)_label", label_file.name).group(1)
            cores.append((label, hwmon / f"temp{idx}_input"))
    cores.sort(key=lambda c: int(re.search(r"\d+", c[0]).group()))
    return cores


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        cfg.setdefault("curve", [[0, 0], [50, 1], [58, 2], [65, 3], [72, 4], [78, 5], [85, 6], [92, 7]])
        cfg.setdefault("watchdog_timeout", 30)
        cfg.setdefault("throttle_processes", [])
        return cfg
    return {
        "curve": [[0, 0], [50, 1], [58, 2], [65, 3], [72, 4], [78, 5], [85, 6], [92, 7]],
        "watchdog_timeout": 30,
        "throttle_processes": [],
    }


def temp_color(temp):
    if temp < 65:
        return QColor(GREEN)
    if temp < 82:
        return QColor(YELLOW)
    return QColor(RED)


# ---------- reusable UI atoms ----------

class Card(QFrame):
    def __init__(self, padding=12):
        super().__init__()
        self.setStyleSheet(f"background-color: {CARD}; border-radius: 10px;")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(padding, padding, padding, padding)
        self.layout_.setSpacing(4)


class StatCard(Card):
    def __init__(self, label):
        super().__init__()
        head = QLabel(label)
        head.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        self.value_lbl = QLabel("--")
        self.value_lbl.setStyleSheet(f"color: {TEXT}; font-size: 20px; font-weight: 600;")
        self.sub_lbl = QLabel("")
        self.sub_lbl.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.layout_.addWidget(head)
        self.layout_.addWidget(self.value_lbl)
        self.layout_.addWidget(self.sub_lbl)

    def set_value(self, text, color=None):
        self.value_lbl.setText(text)
        c = color or TEXT
        self.value_lbl.setStyleSheet(f"color: {c}; font-size: 20px; font-weight: 600;")

    def set_sub(self, text, color=None):
        self.sub_lbl.setText(text)
        c = color or TEXT_MUTED
        self.sub_lbl.setStyleSheet(f"color: {c}; font-size: 11px;")


def section_title(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
    return lbl


class Pill(QLabel):
    def __init__(self, text, bg=ACCENT_MUTED, fg=ACCENT):
        super().__init__(text)
        self.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 11px; "
            f"padding: 4px 10px; border-radius: 9px;"
        )


# ---------- custom-painted widgets ----------

class TempGraph(QWidget):
    def __init__(self):
        super().__init__()
        self.history = deque(maxlen=HISTORY_LEN)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def push(self, temp):
        self.history.append(temp)
        self.update()

    def stats(self):
        if not self.history:
            return None
        return min(self.history), sum(self.history) / len(self.history), max(self.history)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad = 10
        lo, hi = 30, 100

        grid_pen = QPen(QColor(BORDER))
        grid_pen.setWidth(1)
        for t in (40, 60, 80, 100):
            y = h - pad - ((t - lo) / (hi - lo)) * (h - 2 * pad)
            p.setPen(grid_pen)
            p.drawLine(QPointF(pad, y), QPointF(w - pad, y))
            p.setPen(QColor(TEXT_MUTED))
            p.drawText(int(pad + 2), int(y - 3), f"{t}°")

        if len(self.history) < 2:
            p.end()
            return

        n = len(self.history)
        step = (w - 2 * pad) / max(HISTORY_LEN - 1, 1)
        points = []
        for i, t in enumerate(self.history):
            x = pad + (HISTORY_LEN - n + i) * step
            y = h - pad - ((max(lo, min(hi, t)) - lo) / (hi - lo)) * (h - 2 * pad)
            points.append(QPointF(x, y))

        fill_path = QPainterPath()
        fill_path.moveTo(points[0].x(), h - pad)
        for pt in points:
            fill_path.lineTo(pt)
        fill_path.lineTo(points[-1].x(), h - pad)
        fill_path.closeSubpath()
        fill_color = QColor(ACCENT)
        fill_color.setAlpha(26)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(fill_color))
        p.drawPath(fill_path)

        line_pen = QPen(QColor(ACCENT))
        line_pen.setWidth(2)
        p.setPen(line_pen)
        for i in range(len(points) - 1):
            p.drawLine(points[i], points[i + 1])

        last = points[-1]
        p.setBrush(QBrush(temp_color(self.history[-1])))
        p.setPen(Qt.NoPen)
        p.drawEllipse(last, 4, 4)
        p.end()


class CoreBar(QWidget):
    def __init__(self, label):
        super().__init__()
        self.setFixedHeight(16)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.name_lbl = QLabel(label)
        self.name_lbl.setFixedWidth(48)
        self.name_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")

        self.track = QFrame()
        self.track.setFixedHeight(5)
        self.track.setStyleSheet(f"background-color: {BG}; border-radius: 2px;")
        track_layout = QHBoxLayout(self.track)
        track_layout.setContentsMargins(0, 0, 0, 0)
        self.fill = QFrame()
        self.fill.setStyleSheet(f"background-color: {GREEN}; border-radius: 2px;")
        track_layout.addWidget(self.fill, 0)
        track_layout.addStretch(100)

        self.val_lbl = QLabel("--°")
        self.val_lbl.setFixedWidth(30)
        self.val_lbl.setAlignment(Qt.AlignRight)
        self.val_lbl.setStyleSheet(f"color: {TEXT}; font-size: 11px; font-weight: 600;")

        row.addWidget(self.name_lbl)
        row.addWidget(self.track, 1)
        row.addWidget(self.val_lbl)

    def set_temp(self, temp):
        pct = max(2, min(100, int(temp)))
        color = temp_color(temp).name()
        self.fill.setStyleSheet(f"background-color: {color}; border-radius: 2px;")
        total = max(self.track.width(), 1)
        self.fill.setFixedWidth(int(total * pct / 100))
        self.val_lbl.setText(f"{temp:.0f}°")


class Chip(QFrame):
    def __init__(self, text, on_remove):
        super().__init__()
        self.setStyleSheet(f"background-color: {ACCENT_MUTED}; border-radius: 11px;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 3, 5, 3)
        layout.setSpacing(5)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {ACCENT}; font-size: 11px; background: transparent;")
        close_btn = QPushButton("x")
        close_btn.setFixedSize(15, 15)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {ACCENT}; border: none; "
            f"font-size: 10px; font-weight: 700; }}"
            f"QPushButton:hover {{ color: {TEXT}; }}"
        )
        close_btn.clicked.connect(lambda: on_remove(text, self))
        layout.addWidget(lbl)
        layout.addWidget(close_btn)


class FlowLayout(QLayout):
    """Minimal flow layout so chips wrap onto new lines."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRectF(0, 0, width, 0).toRect(), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        return QSize(0, 0)

    def _do_layout(self, rect, test_only):
        x, y = rect.x(), rect.y()
        line_height = 0
        for item in self._items:
            wid = item.widget()
            sp_x, sp_y = self._spacing, self._spacing
            next_x = x + wid.sizeHint().width() + sp_x
            if next_x - sp_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + sp_y
                next_x = x + wid.sizeHint().width() + sp_x
                line_height = 0
            if not test_only:
                item.setGeometry(QRectF(x, y, wid.sizeHint().width(), wid.sizeHint().height()).toRect())
            x = next_x
            line_height = max(line_height, wid.sizeHint().height())
        return y + line_height - rect.y()


def top_processes(limit=25):
    """Sample CPU% across user-space processes and return the top consumers,
    aggregated by name. Kernel worker threads (kworker, ksoftirqd, migration,
    etc.) are excluded - they have no cmdline/executable backing them, aren't
    stably nameable, and renice-ing them doesn't reduce kernel-side work."""
    if psutil is None:
        return []
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)  # prime the interval
        except Exception:
            continue
    time.sleep(0.15)
    agg = {}
    for p in psutil.process_iter(['name']):
        try:
            name = p.info.get('name')
            if not name:
                continue
            try:
                if not p.cmdline():
                    continue  # kernel thread - no real executable/cmdline
            except Exception:
                continue
            cpu = p.cpu_percent(None)
            agg[name] = agg.get(name, 0.0) + cpu
        except Exception:
            continue
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:limit]


class ProcessPickerDialog(QDialog):
    """Scrollable list of top CPU-consuming processes to pick from."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_name = None
        self.setWindowTitle("Pick a process")
        self.setFixedSize(340, 420)
        self.setStyleSheet(f"background-color: {CARD}; color: {TEXT};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        head = QLabel("Top CPU consumers right now")
        head.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        layout.addWidget(head)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("filter...")
        self.filter_input.setStyleSheet(
            f"background-color: {BG}; color: {TEXT}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; padding: 6px;"
        )
        self.filter_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_input)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(
            f"""
            QListWidget {{
                background-color: {BG}; color: {TEXT}; border: 1px solid {BORDER};
                border-radius: 6px; padding: 4px;
            }}
            QListWidget::item {{ padding: 6px; border-radius: 4px; }}
            QListWidget::item:selected {{ background-color: {ACCENT_MUTED}; color: {ACCENT}; }}
            """
        )
        self.list_widget.itemDoubleClicked.connect(self._choose_current)
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        choose_btn = QPushButton("Add selected")
        cancel_btn = QPushButton("Cancel")
        for b in (refresh_btn, choose_btn, cancel_btn):
            b.setStyleSheet(
                f"QPushButton {{ background-color: transparent; color: {TEXT}; "
                f"border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 10px; }}"
                f"QPushButton:hover {{ border-color: {ACCENT}; }}"
            )
        refresh_btn.clicked.connect(self._populate)
        choose_btn.clicked.connect(self._choose_current)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        btn_row.addWidget(choose_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._all_rows = []
        self._populate()

    def _populate(self):
        self.list_widget.clear()
        self.list_widget.addItem("Sampling CPU usage...")
        QApplication.processEvents()
        self._all_rows = top_processes(30)
        self._render_rows(self._all_rows)

    def _render_rows(self, rows):
        self.list_widget.clear()
        if not rows:
            msg = "psutil not installed" if psutil is None else "No processes found"
            self.list_widget.addItem(msg)
            return
        for name, cpu in rows:
            item = QListWidgetItem(f"{name}   ·   {cpu:.1f}%")
            item.setData(Qt.UserRole, name)
            self.list_widget.addItem(item)

    def _apply_filter(self, text):
        text = text.lower().strip()
        if not text:
            self._render_rows(self._all_rows)
            return
        filtered = [(n, c) for n, c in self._all_rows if text in n.lower()]
        self._render_rows(filtered)

    def _choose_current(self):
        item = self.list_widget.currentItem()
        if item and item.data(Qt.UserRole):
            self.selected_name = item.data(Qt.UserRole)
            self.accept()


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fan Control")
        self.setFixedSize(820, 500)
        self.setStyleSheet(f"background-color: {BG}; color: {TEXT};")

        self.cfg = load_config()
        self.temp_input = find_package_temp_input()
        self.core_inputs = find_core_temp_inputs()
        self.core_bars = {}

        root = QWidget()
        root.setStyleSheet(f"background-color: {BG};")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 16)
        outer.setSpacing(8)

        # header
        header = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {GREEN}; font-size: 9px;")
        host_lbl = QLabel("ThinkPad X1 Carbon Gen 8 · fancontrold")
        host_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        header.addWidget(dot)
        header.addWidget(host_lbl)
        header.addStretch()
        self.watchdog_pill = Pill(f"Watchdog · {self.cfg['watchdog_timeout']}s")
        header.addWidget(self.watchdog_pill)
        outer.addLayout(header)

        # stat row
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        self.temp_stat = StatCard("CPU package")
        self.fan_stat = StatCard("Fan")
        self.mode_stat = StatCard("Mode")
        self.session_stat = StatCard("Session")
        for s in (self.temp_stat, self.fan_stat, self.mode_stat, self.session_stat):
            stats_row.addWidget(s)
        outer.addLayout(stats_row)

        # two-column body
        body = QHBoxLayout()
        body.setSpacing(8)

        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        left_widget = QWidget()
        left_widget.setLayout(left_col)
        right_widget = QWidget()
        right_widget.setLayout(right_col)
        body.addWidget(left_widget, 3)
        body.addWidget(right_widget, 2)
        outer.addLayout(body)
        outer.addStretch(1)

        # left: temp graph
        graph_card = Card()
        graph_card.layout_.addWidget(section_title("Temperature history"))
        self.temp_graph = TempGraph()
        graph_card.layout_.addWidget(self.temp_graph)
        left_col.addWidget(graph_card)

        # left: core temps
        core_card = Card()
        core_card.layout_.addWidget(section_title("Per-core temps"))
        core_body = QVBoxLayout()
        core_body.setSpacing(3)
        if self.core_inputs:
            for label, _ in self.core_inputs:
                bar = CoreBar(label)
                self.core_bars[label] = bar
                core_body.addWidget(bar)
        else:
            no_core = QLabel("No per-core sensors found")
            no_core.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
            core_body.addWidget(no_core)
        core_card.layout_.addLayout(core_body)
        left_col.addWidget(core_card)
        left_col.addStretch(1)

        # right: control
        control_card = Card()
        control_card.layout_.addWidget(section_title("Control"))

        toggle_wrap = QFrame()
        toggle_wrap.setStyleSheet(f"background-color: {BG}; border-radius: 8px;")
        toggle_layout = QHBoxLayout(toggle_wrap)
        toggle_layout.setContentsMargins(3, 3, 3, 3)
        toggle_layout.setSpacing(3)
        self.auto_btn = QPushButton("Auto")
        self.manual_btn = QPushButton("Manual")
        for b in (self.auto_btn, self.manual_btn):
            b.setCheckable(True)
            b.setMinimumHeight(26)
            b.setStyleSheet(self._toggle_style())
        self.auto_btn.setChecked(True)
        self.auto_btn.clicked.connect(self.set_auto)
        self.manual_btn.clicked.connect(lambda: self.set_manual(self.level_slider.value()))
        toggle_layout.addWidget(self.auto_btn)
        toggle_layout.addWidget(self.manual_btn)
        control_card.layout_.addWidget(toggle_wrap)

        slider_head = QHBoxLayout()
        slider_cap = QLabel("Level")
        slider_cap.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        self.slider_label = QLabel("0")
        self.slider_label.setStyleSheet(f"color: {TEXT}; font-size: 11px; font-weight: 600;")
        slider_head.addWidget(slider_cap)
        slider_head.addStretch()
        slider_head.addWidget(self.slider_label)
        self.slider_wrap = QWidget()
        sw_layout = QVBoxLayout(self.slider_wrap)
        sw_layout.setContentsMargins(0, 0, 0, 0)
        sw_layout.setSpacing(2)
        sw_layout.addLayout(slider_head)
        self.level_slider = QSlider(Qt.Horizontal)
        self.level_slider.setRange(0, 7)
        self.level_slider.setStyleSheet(self._slider_style())
        self.level_slider.valueChanged.connect(self.on_slider_change)
        sw_layout.addWidget(self.level_slider)
        self.slider_wrap.setEnabled(False)
        control_card.layout_.addWidget(self.slider_wrap)

        extra_row = QHBoxLayout()
        extra_row.setSpacing(8)
        full_btn = QPushButton("Full speed")
        disengaged_btn = QPushButton("Disengaged")
        for b in (full_btn, disengaged_btn):
            b.setStyleSheet(self._outline_btn_style())
            b.setMinimumHeight(26)
        full_btn.clicked.connect(lambda: self.set_manual("full-speed"))
        disengaged_btn.clicked.connect(lambda: self.set_manual("disengaged"))
        extra_row.addWidget(full_btn)
        extra_row.addWidget(disengaged_btn)
        control_card.layout_.addLayout(extra_row)

        right_col.addWidget(control_card)

        # right: throttled processes
        proc_card = Card()
        proc_card.layout_.addWidget(section_title("Throttled processes"))

        self.chip_container = QWidget()
        self.chip_container.setLayout(FlowLayout(spacing=6))
        proc_card.layout_.addWidget(self.chip_container)

        for name in self.cfg.get("throttle_processes", []):
            self._add_chip(name)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.proc_input = QLineEdit()
        self.proc_input.setPlaceholderText("or type a name + Enter")
        self.proc_input.setStyleSheet(
            f"background-color: {BG}; color: {TEXT}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; padding: 5px;"
        )
        self.proc_input.returnPressed.connect(self.add_process)
        add_btn = QPushButton("Add")
        add_btn.setStyleSheet(self._outline_btn_style())
        add_btn.clicked.connect(self.open_process_picker)
        add_row.addWidget(self.proc_input)
        add_row.addWidget(add_btn)
        proc_card.layout_.addLayout(add_row)

        note = QLabel("Reniced when hot. Persist via config.toml's throttle_processes.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
        proc_card.layout_.addWidget(note)

        right_col.addWidget(proc_card)
        right_col.addStretch(1)

        # tray
        self.tray = QSystemTrayIcon(self._make_icon(GREEN), self)
        self.tray.setToolTip("Fan Control")
        tray_menu = QMenu()
        show_action = QAction("Show dashboard", self)
        show_action.triggered.connect(self.showNormal)
        auto_action = QAction("Set: Auto", self)
        auto_action.triggered.connect(self.set_auto)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        tray_menu.addAction(show_action)
        tray_menu.addAction(auto_action)
        for lvl in range(0, 8):
            act = QAction(f"Set: Level {lvl}", self)
            act.triggered.connect(lambda checked=False, l=lvl: self.set_manual(l))
            tray_menu.addAction(act)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        self.poll()

    # -- style helpers --

    def _toggle_style(self):
        return f"""
            QPushButton {{
                background-color: transparent; color: {TEXT_DIM}; border: none;
                border-radius: 6px; font-size: 12px; font-weight: 600;
            }}
            QPushButton:checked {{ background-color: {ACCENT}; color: white; }}
        """

    def _outline_btn_style(self):
        return f"""
            QPushButton {{
                background-color: transparent; color: {TEXT}; border: 1px solid {BORDER};
                border-radius: 7px; font-size: 12px; font-weight: 500; padding: 4px 10px;
            }}
            QPushButton:hover {{ border-color: {ACCENT}; }}
        """

    def _slider_style(self):
        return f"""
            QSlider::groove:horizontal {{
                height: 5px; background: {BORDER}; border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ACCENT}; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: white; width: 15px; height: 15px;
                margin: -5px 0; border-radius: 7px; border: 2px solid {ACCENT};
            }}
        """

    def _make_icon(self, color_hex):
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(color_hex)))
        p.setPen(Qt.NoPen)
        p.drawEllipse(4, 4, 24, 24)
        p.end()
        return QIcon(pix)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.showNormal()
            self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    # -- control actions --

    def set_auto(self):
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text("auto")
        self.auto_btn.setChecked(True)
        self.manual_btn.setChecked(False)
        self.slider_wrap.setEnabled(False)

    def set_manual(self, level):
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEVEL_FILE.write_text(str(level))
        MODE_FILE.write_text("manual")
        self.auto_btn.setChecked(False)
        self.manual_btn.setChecked(True)
        self.slider_wrap.setEnabled(True)
        if isinstance(level, int):
            self.level_slider.blockSignals(True)
            self.level_slider.setValue(level)
            self.level_slider.blockSignals(False)

    def on_slider_change(self, value):
        self.slider_label.setText(str(value))
        if self.manual_btn.isChecked():
            self.set_manual(value)

    def _add_chip(self, name):
        chip = Chip(name, self._remove_chip)
        self.chip_container.layout().addWidget(chip)

    def _remove_chip(self, name, widget):
        self.chip_container.layout().removeWidget(widget)
        widget.deleteLater()

    def open_process_picker(self):
        dialog = ProcessPickerDialog(self)
        if dialog.exec() == QDialog.Accepted and dialog.selected_name:
            self._add_chip(dialog.selected_name)
        self.proc_input.clear()

    def add_process(self):
        name = self.proc_input.text().strip()
        if name:
            self._add_chip(name)
            self.proc_input.clear()

    # -- polling --

    def poll(self):
        temp = None
        if self.temp_input and self.temp_input.exists():
            try:
                temp = int(self.temp_input.read_text().strip()) / 1000.0
            except Exception:
                pass

        if temp is not None:
            color = temp_color(temp).name()
            status = "Nominal" if temp < 65 else ("Elevated" if temp < 82 else "Hot")
            self.temp_stat.set_value(f"{temp:.1f}°C", color)
            self.temp_stat.set_sub(status, color)
            self.temp_graph.push(temp)
            self.tray.setIcon(self._make_icon(color))

            stats = self.temp_graph.stats()
            if stats:
                mn, avg, mx = stats
                self.session_stat.set_value(f"{avg:.0f}°avg")
                self.session_stat.set_sub(f"{mn:.0f}° – {mx:.0f}°")

        for label, path in self.core_inputs:
            if path.exists():
                try:
                    t = int(path.read_text().strip()) / 1000.0
                    self.core_bars[label].set_temp(t)
                except Exception:
                    pass

        fan_level_text = "--"
        rpm = 0
        if FAN_PROC.exists():
            try:
                text = FAN_PROC.read_text()
                m = re.search(r"level:\s*(\S+)", text)
                if m:
                    fan_level_text = m.group(1)
                m2 = re.search(r"speed:\s*(\d+)", text)
                if m2:
                    rpm = int(m2.group(1))
            except Exception:
                pass
        level_display = f"L{fan_level_text}" if fan_level_text.isdigit() else fan_level_text.upper()
        self.fan_stat.set_value(level_display)
        self.fan_stat.set_sub(f"{rpm} rpm" if rpm else "")

        mode = MODE_FILE.read_text().strip() if MODE_FILE.exists() else "auto"
        self.mode_stat.set_value(mode.capitalize(), GREEN if mode == "auto" else ACCENT)
        self.mode_stat.set_sub("Curve-managed" if mode == "auto" else "Manual override")


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
