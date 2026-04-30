#!/usr/bin/env python3
"""
SnapMark — Lightweight Windows Screenshot & Annotation Tool
------------------------------------------------------------
• Press Ctrl+F1 anywhere to enter area-selection mode
• Drag to select a region — the area is captured automatically
• Annotate with arrows and rectangles, then copy to clipboard or save
• Runs quietly in the system tray
"""

import sys
import math
import threading
import time

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QPushButton, QSpinBox, QLabel,
    QSystemTrayIcon, QMenu,
    QFileDialog, QColorDialog,
    QScrollArea, QFrame,
)
from PyQt6.QtCore import (
    Qt, QRect, QPoint, QSize,
    pyqtSignal, QObject, QTimer,
)
from PyQt6.QtGui import (
    QPixmap, QPainter, QPen, QColor, QBrush,
    QCursor, QIcon, QAction, QImage,
    QFont, QFontMetrics, QPolygon,
)

import keyboard
import mss


# ─────────────────────────────────────────────────────────────────────────────
#  Cross-thread bridge  (hotkey fires in a non-Qt thread → needs signal)
# ─────────────────────────────────────────────────────────────────────────────

class _Bridge(QObject):
    trigger = pyqtSignal()

_bridge = _Bridge()


# ─────────────────────────────────────────────────────────────────────────────
#  Per-screen selection overlay
#
#  Why one overlay per screen?
#  On Windows, Qt reports screen *positions* in physical pixels but screen
#  *sizes* in logical pixels (device-independent pixels).  A single window
#  that spans all screens therefore mixes coordinate systems, producing the
#  wrong capture region when screens have different DPI scales.
#  The industry-standard fix (used by Flameshot, ShareX, etc.) is one
#  borderless overlay per screen so every mouse coordinate is unambiguously
#  in that screen's own logical space.
#
#  Coordinate conversion (logical → physical for mss):
#      physical_x = screen.geometry().x() + local_x * dpr
#      physical_y = screen.geometry().y() + local_y * dpr
#      physical_w = local_w * dpr
#      physical_h = local_h * dpr
#
#  screen.geometry().topLeft() is already in physical pixels on Windows
#  (Qt scales the *size* but not the *position* of each screen).
# ─────────────────────────────────────────────────────────────────────────────

from PyQt6.QtGui import QScreen   # noqa: E402 (re-import for clarity)


class SelectionOverlay(QWidget):
    # Emits *physical* pixel coordinates ready for mss
    area_selected = pyqtSignal(int, int, int, int)
    cancelled     = pyqtSignal()

    def __init__(self, screen: QScreen):
        super().__init__()
        self._screen = screen

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

        self._origin  = QPoint()
        self._current = QPoint()
        self._active  = False

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _e):
        p = QPainter(self)

        p.fillRect(self.rect(), QColor(0, 0, 0, 110))

        if self._active and not self._origin.isNull():
            sel = QRect(self._origin, self._current).normalized()

            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(sel, Qt.GlobalColor.transparent)
            p.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )

            p.setPen(QPen(QColor("#0078d4"), 2, Qt.PenStyle.SolidLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(sel)

            # Show physical pixel dimensions (what will actually be captured)
            dpr  = self._screen.devicePixelRatio()
            pw   = int(sel.width()  * dpr)
            ph   = int(sel.height() * dpr)
            badge = f"  {pw} × {ph} px  "
            f  = QFont("Segoe UI", 10, QFont.Weight.Bold)
            p.setFont(f)
            fm = QFontMetrics(f)
            bw = fm.horizontalAdvance(badge)
            bh = fm.height() + 4
            bx = sel.x()
            by = sel.y() - bh - 4
            if by < 2:
                by = sel.y() + 4
            p.fillRect(bx, by, bw, bh, QColor("#0078d4"))
            p.setPen(QColor("white"))
            p.drawText(bx, by + bh - 4, badge)

        else:
            hint = "  Drag to select an area   ·   Esc to cancel  "
            f  = QFont("Segoe UI", 13)
            p.setFont(f)
            fm = QFontMetrics(f)
            tw = fm.horizontalAdvance(hint)
            th = fm.height()
            cx = (self.width()  - tw) // 2
            cy = (self.height() - th) // 2
            p.fillRect(cx - 4, cy - 8, tw + 8, th + 16, QColor(0, 0, 0, 170))
            p.setPen(QColor("white"))
            p.drawText(cx, cy + th - 2, hint)

    # ── mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._origin  = ev.pos()
            self._current = ev.pos()
            self._active  = True
            self.update()

    def mouseMoveEvent(self, ev):
        if self._active:
            self._current = ev.pos()
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self._active:
            self._current = ev.pos()
            self._active  = False
            local_rect = QRect(self._origin, self._current).normalized()
            self._reset()
            if local_rect.width() > 4 and local_rect.height() > 4:
                self.area_selected.emit(*self._to_physical(local_rect))
            else:
                self.cancelled.emit()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._reset()
            self.cancelled.emit()

    # ── coordinate conversion ─────────────────────────────────────────────────

    def _to_physical(self, local_rect: QRect):
        """
        Convert a rect in this overlay's local logical pixels to the physical
        pixel coordinates that mss expects.

        On Windows, Qt keeps each screen's *position* in physical pixels but
        reports its *size* in logical pixels.  So:
          physical_x = screen_origin_x  +  local_x * dpr
          physical_y = screen_origin_y  +  local_y * dpr
          physical_w = local_w * dpr
          physical_h = local_h * dpr
        """
        dpr = self._screen.devicePixelRatio()
        sg  = self._screen.geometry()          # .topLeft() is in physical px on Windows
        px  = int(sg.x() + local_rect.x()      * dpr)
        py  = int(sg.y() + local_rect.y()      * dpr)
        pw  = int(local_rect.width()            * dpr)
        ph  = int(local_rect.height()           * dpr)
        return px, py, pw, ph

    # ── public ────────────────────────────────────────────────────────────────

    def activate(self):
        self._reset()
        # Position this overlay exactly on its assigned screen
        self.setGeometry(self._screen.geometry())
        self.show()
        self.activateWindow()
        self.raise_()

    def _reset(self):
        self._origin  = QPoint()
        self._current = QPoint()
        self._active  = False
        self.update()


# ─────────────────────────────────────────────────────────────────────────────
#  Drawing canvas — renders base screenshot + committed annotations + live preview
# ─────────────────────────────────────────────────────────────────────────────

class Canvas(QWidget):
    def __init__(self, pixmap: QPixmap):
        super().__init__()
        self._base    = pixmap.copy()
        self._overlay = QPixmap(pixmap.size())
        self._overlay.fill(Qt.GlobalColor.transparent)

        self._tool      = "arrow"
        self._color     = QColor("#e53935")   # vivid red default
        self._thickness = 3

        self._p1 = QPoint()
        self._p2 = QPoint()
        self._drawing = False

        self._history: list[QPixmap] = []

        self.setFixedSize(pixmap.size())
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def tool(self):        return self._tool
    @tool.setter
    def tool(self, v):     self._tool = v

    @property
    def color(self):       return self._color
    @color.setter
    def color(self, v):    self._color = v

    @property
    def thickness(self):   return self._thickness
    @thickness.setter
    def thickness(self, v): self._thickness = v

    # ── paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _e):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._base)
        p.drawPixmap(0, 0, self._overlay)
        if self._drawing:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._apply_pen(p)
            self._draw_shape(p, self._p1, self._p2)

    # ── mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._p1 = self._p2 = ev.pos()
            self._drawing = True
            self.update()

    def mouseMoveEvent(self, ev):
        if self._drawing:
            self._p2 = ev.pos()
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self._drawing:
            self._p2 = ev.pos()
            self._drawing = False
            self._commit()
            self.update()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _apply_pen(self, painter: QPainter):
        pen = QPen(
            self._color, self._thickness,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_shape(self, p: QPainter, p1: QPoint, p2: QPoint):
        if self._tool == "rect":
            p.drawRect(QRect(p1, p2).normalized())
        else:
            self._draw_arrow(p, p1, p2)

    def _draw_arrow(self, p: QPainter, p1: QPoint, p2: QPoint):
        if p1 == p2:
            return
        p.drawLine(p1, p2)

        dx  = p2.x() - p1.x()
        dy  = p2.y() - p1.y()
        lng = math.hypot(dx, dy)
        if lng < 1:
            return

        ux, uy  = dx / lng, dy / lng
        head_sz = max(12, self._thickness * 4)
        angle   = 0.45   # ~26°

        ca, sa = math.cos(angle), math.sin(angle)

        lx = p2.x() - head_sz * (ux * ca - uy * sa)
        ly = p2.y() - head_sz * (uy * ca + ux * sa)
        rx = p2.x() - head_sz * (ux * ca + uy * sa)
        ry = p2.y() - head_sz * (uy * ca - ux * sa)

        tip = QPolygon([p2, QPoint(int(lx), int(ly)), QPoint(int(rx), int(ry))])
        p.setBrush(QBrush(self._color))
        p.drawPolygon(tip)

    def _commit(self):
        if self._p1 == self._p2:
            return
        self._history.append(self._overlay.copy())
        painter = QPainter(self._overlay)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._apply_pen(painter)
        self._draw_shape(painter, self._p1, self._p2)
        painter.end()

    # ── public ────────────────────────────────────────────────────────────────

    def undo(self):
        if self._history:
            self._overlay = self._history.pop()
            self.update()

    def merged_pixmap(self) -> QPixmap:
        """Return the base screenshot with all annotations burned in."""
        result = self._base.copy()
        p = QPainter(result)
        p.drawPixmap(0, 0, self._overlay)
        p.end()
        return result


# ─────────────────────────────────────────────────────────────────────────────
#  Annotation window
# ─────────────────────────────────────────────────────────────────────────────

_BTN = """
QPushButton {
    background: #2b2b2b;
    color: #e8e8e8;
    border: 2px solid transparent;
    border-radius: 6px;
    padding: 5px 12px;
    font-family: 'Segoe UI';
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover   { background: #3a3a3a; }
QPushButton:checked { border: 2px solid #0078d4; background: #1a3a5c; }
QPushButton:pressed { background: #444; }
"""

_COLOR_BTN = """
QPushButton {{
    background: {color};
    border: 2px solid #777;
    border-radius: 6px;
}}
QPushButton:hover {{ border: 2px solid white; }}
"""


def _sep():
    s = QFrame()
    s.setFrameShape(QFrame.Shape.VLine)
    s.setFixedSize(1, 26)
    s.setStyleSheet("background: #555;")
    return s


class AnnotationWindow(QMainWindow):
    def __init__(self, pixmap: QPixmap):
        super().__init__()
        self.setWindowTitle("SnapMark — Annotate")
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setStyleSheet("background: #1a1a1a;")

        self._canvas = Canvas(pixmap)

        # ── toolbar ──────────────────────────────────────────────────────────
        tb = QWidget()
        tb.setStyleSheet(
            "background: #222; border-bottom: 1px solid #3a3a3a;"
        )
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(10, 7, 10, 7)
        tbl.setSpacing(6)

        # tool buttons
        self._b_arrow = QPushButton("↗  Arrow")
        self._b_arrow.setCheckable(True)
        self._b_arrow.setChecked(True)
        self._b_arrow.setStyleSheet(_BTN)
        self._b_arrow.clicked.connect(lambda: self._set_tool("arrow"))

        self._b_rect = QPushButton("□  Rectangle")
        self._b_rect.setCheckable(True)
        self._b_rect.setStyleSheet(_BTN)
        self._b_rect.clicked.connect(lambda: self._set_tool("rect"))

        # color swatch
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(30, 30)
        self._color_btn.setToolTip("Pick annotation color")
        self._refresh_color_btn(QColor("#e53935"))
        self._color_btn.clicked.connect(self._pick_color)

        # stroke thickness
        sz_lbl = QLabel("Size:")
        sz_lbl.setStyleSheet(
            "color: #aaa; font-family: 'Segoe UI'; font-size: 12px;"
        )
        self._sz = QSpinBox()
        self._sz.setRange(1, 20)
        self._sz.setValue(3)
        self._sz.setFixedWidth(56)
        self._sz.setStyleSheet(
            "QSpinBox { background:#2b2b2b; color:white; border:1px solid #555;"
            " border-radius:4px; padding:2px 4px; font-size:13px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width:16px; }"
        )
        self._sz.valueChanged.connect(lambda v: setattr(self._canvas, "thickness", v))

        # action buttons
        btn_undo  = QPushButton("↩  Undo")
        btn_undo.setStyleSheet(_BTN)
        btn_undo.clicked.connect(self._canvas.undo)

        btn_copy  = QPushButton("📋  Copy")
        btn_copy.setStyleSheet(_BTN)
        btn_copy.clicked.connect(self._copy)

        btn_save  = QPushButton("💾  Save as…")
        btn_save.setStyleSheet(_BTN)
        btn_save.clicked.connect(self._save)

        btn_close = QPushButton("✕")
        btn_close.setStyleSheet(_BTN)
        btn_close.setFixedWidth(32)
        btn_close.clicked.connect(self.close)

        for w in [
            self._b_arrow, self._b_rect, _sep(),
            self._color_btn, sz_lbl, self._sz, _sep(),
            btn_undo, _sep(),
            btn_copy, btn_save, _sep(),
            btn_close,
        ]:
            tbl.addWidget(w)
        tbl.addStretch()

        # shortcut label
        hint = QLabel("  Ctrl+Z undo   ·   Ctrl+C copy   ·   Ctrl+S save")
        hint.setStyleSheet(
            "color: #555; font-family: 'Segoe UI'; font-size: 11px;"
        )
        tbl.addWidget(hint)

        # ── scrollable canvas area ────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: #141414; }"
        )

        central = QWidget()
        root    = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(tb)
        root.addWidget(scroll)
        self.setCentralWidget(central)

        # ── keyboard shortcuts ────────────────────────────────────────────────
        # (using keyPressEvent so we don't need QShortcut)

        # ── window sizing ─────────────────────────────────────────────────────
        avail = QApplication.primaryScreen().availableGeometry()
        w = min(pixmap.width()  + 24,  avail.width()  - 60)
        h = min(pixmap.height() + 100, avail.height() - 60)
        self.resize(w, h)
        cx = avail.x() + (avail.width()  - w) // 2
        cy = avail.y() + (avail.height() - h) // 2
        self.move(cx, cy)

    # ── keyboard shortcuts ────────────────────────────────────────────────────

    def keyPressEvent(self, ev):
        mods = ev.modifiers()
        key  = ev.key()
        ctrl = Qt.KeyboardModifier.ControlModifier
        if mods & ctrl and key == Qt.Key.Key_Z:
            self._canvas.undo()
        elif mods & ctrl and key == Qt.Key.Key_C:
            self._copy()
        elif mods & ctrl and key == Qt.Key.Key_S:
            self._save()
        else:
            super().keyPressEvent(ev)

    # ── toolbar helpers ───────────────────────────────────────────────────────

    def _set_tool(self, tool: str):
        self._canvas.tool = tool
        self._b_arrow.setChecked(tool == "arrow")
        self._b_rect.setChecked(tool  == "rect")

    def _refresh_color_btn(self, color: QColor):
        self._color_btn.setStyleSheet(_COLOR_BTN.format(color=color.name()))

    def _pick_color(self):
        c = QColorDialog.getColor(self._canvas.color, self, "Annotation Color")
        if c.isValid():
            self._canvas.color = c
            self._refresh_color_btn(c)

    def _copy(self):
        QApplication.clipboard().setPixmap(self._canvas.merged_pixmap())
        prev = self.windowTitle()
        self.setWindowTitle("SnapMark — ✓ Copied to clipboard!")
        QTimer.singleShot(1800, lambda: self.setWindowTitle(prev))

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Screenshot",
            "screenshot.png",
            "PNG Image (*.png);;JPEG (*.jpg)"
        )
        if path:
            saved = self._canvas.merged_pixmap().save(path)
            if saved:
                self.setWindowTitle("SnapMark — ✓ Saved!")
                QTimer.singleShot(
                    1800,
                    lambda: self.setWindowTitle("SnapMark — Annotate")
                )


# ─────────────────────────────────────────────────────────────────────────────
#  Main application
# ─────────────────────────────────────────────────────────────────────────────

class SnapMarkApp:
    def __init__(self):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)
        self._app.setApplicationName("SnapMark")

        self._windows: list[AnnotationWindow] = []

        # Build one overlay per screen; rebuild if screens change
        self._overlays: list[SelectionOverlay] = []
        self._build_overlays()
        self._app.screenAdded.connect(lambda _s: self._build_overlays())
        self._app.screenRemoved.connect(lambda _s: self._build_overlays())

        self._tray = self._build_tray()

        _bridge.trigger.connect(self._activate_all)
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    # ── overlay management ────────────────────────────────────────────────────

    def _build_overlays(self):
        """Create exactly one SelectionOverlay per connected screen."""
        for ov in self._overlays:
            ov.hide()
            ov.deleteLater()
        self._overlays.clear()

        for screen in QApplication.screens():
            ov = SelectionOverlay(screen)
            ov.area_selected.connect(self._on_area)
            ov.cancelled.connect(self._hide_all)
            self._overlays.append(ov)

    def _activate_all(self):
        """Show all per-screen overlays simultaneously."""
        for ov in self._overlays:
            ov.activate()

    def _hide_all(self):
        for ov in self._overlays:
            ov.hide()
            ov._reset()

    # ── hotkey ────────────────────────────────────────────────────────────────

    def _hotkey_loop(self):
        keyboard.add_hotkey("ctrl+f1", lambda: _bridge.trigger.emit())
        keyboard.wait()

    # ── screenshot capture ────────────────────────────────────────────────────

    def _on_area(self, x: int, y: int, w: int, h: int):
        # Hide all other overlays immediately (user picked a region on one screen)
        self._hide_all()
        # Short delay so overlays fully vanish before we grab pixels
        QTimer.singleShot(80, lambda: self._capture(x, y, w, h))

    def _capture(self, x: int, y: int, w: int, h: int):
        # x, y, w, h are already in *physical* pixels — conversion was done
        # inside SelectionOverlay._to_physical() using per-screen DPR.
        with mss.MSS() as sct:
            monitor = {"top": y, "left": x, "width": w, "height": h}
            raw = sct.grab(monitor)
            # mss returns BGRA bytes; on little-endian Windows this maps to
            # Qt's ARGB32 layout (0xAARRGGBB = B G R A in memory)
            img = QImage(
                bytes(raw.raw),
                raw.width,
                raw.height,
                raw.width * 4,
                QImage.Format.Format_ARGB32,
            )

        pixmap = QPixmap.fromImage(img)
        win = AnnotationWindow(pixmap)
        win.show()
        self._windows.append(win)
        win.destroyed.connect(
            lambda: self._windows.remove(win) if win in self._windows else None
        )

    # ── system tray ───────────────────────────────────────────────────────────

    def _build_tray(self) -> QSystemTrayIcon:
        icon = self._make_icon()
        tray = QSystemTrayIcon(icon)
        tray.setToolTip("SnapMark  ·  Ctrl+F1 to screenshot")

        menu = QMenu()
        menu.setStyleSheet(
            "QMenu { background:#252525; color:white; border:1px solid #444;"
            " border-radius:6px; padding:4px; }"
            "QMenu::item { padding:6px 20px; border-radius:4px; }"
            "QMenu::item:selected { background:#0078d4; }"
            "QMenu::separator { height:1px; background:#444; margin:4px 8px; }"
        )

        act_snap = menu.addAction("📸  Take Screenshot  (Ctrl+F1)")
        act_snap.triggered.connect(self._activate_all)
        menu.addSeparator()
        act_quit = menu.addAction("✕  Quit SnapMark")
        act_quit.triggered.connect(QApplication.quit)

        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._activate_all()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )
        tray.show()
        tray.showMessage(
            "SnapMark is running",
            "Press Ctrl+F1 anywhere to capture a screenshot.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )
        return tray

    @staticmethod
    def _make_icon() -> QIcon:
        """Draw a small camera icon programmatically (no file needed)."""
        pm = QPixmap(32, 32)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # blue rounded square background
        p.setBrush(QBrush(QColor("#0078d4")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(1, 1, 30, 30, 7, 7)

        # camera body outline
        pen = QPen(QColor("white"), 2.0, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # outer lens circle
        p.drawEllipse(8, 10, 16, 16)
        # inner lens circle
        p.drawEllipse(12, 14, 8, 8)
        # viewfinder notch
        p.drawLine(11, 10, 14, 7)
        p.drawLine(14, 7, 18, 7)
        p.drawLine(18, 7, 21, 10)
        p.end()

        return QIcon(pm)

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self):
        sys.exit(self._app.exec())


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    SnapMarkApp().run()
