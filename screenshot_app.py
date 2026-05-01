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
    QPushButton, QLabel, QLineEdit,
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
    area_selected = pyqtSignal(int, int, int, int, float)  # x, y, w, h, dpr
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
        return px, py, pw, ph, dpr  # include dpr so canvas can display at logical size

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
    """
    Drawing canvas supporting:
      rect · ellipse · line · arrow · pencil · highlight · blur · text · step-number
    """

    def __init__(self, pixmap: QPixmap):
        super().__init__()

        # Keep the FULL physical-resolution capture as base — never downscale it.
        # Downscaling here is what was causing the pixelated/blurry look.
        # Instead we keep full res and let paintEvent draw it scaled with smooth
        # interpolation.  The canvas widget is sized to logical pixels so the
        # window stays at a sane size; Qt's SmoothPixmapTransform does the
        # high-quality downscale only at render time (no data thrown away).
        self._base = pixmap.copy()
        dpr        = pixmap.devicePixelRatio() if pixmap.devicePixelRatio() > 0 else 1.0
        logical_w  = max(1, int(pixmap.width()  / dpr))
        logical_h  = max(1, int(pixmap.height() / dpr))
        self._logical_size = QSize(logical_w, logical_h)

        # Overlay lives in logical pixel space (matches mouse event coords)
        self._overlay = QPixmap(self._logical_size)
        self._overlay.fill(Qt.GlobalColor.transparent)

        self._tool      = "arrow"
        self._color     = QColor("#e53935")
        self._thickness = 3

        self._p1 = QPoint()
        self._p2 = QPoint()
        self._drawing = False

        self._path: list[QPoint] = []
        self._step_n  = 1
        self._text_widget: QLineEdit | None = None
        self._text_pos  = QPoint()

        self._history: list[QPixmap] = []

        self.setFixedSize(self._logical_size)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def tool(self):         return self._tool
    @tool.setter
    def tool(self, v):
        self._tool = v
        # commit any open text box when switching tools
        if v != "text" and self._text_widget:
            self._commit_text()

    @property
    def color(self):        return self._color
    @color.setter
    def color(self, v):     self._color = v

    @property
    def thickness(self):    return self._thickness
    @thickness.setter
    def thickness(self, v): self._thickness = v

    def reset_step(self):
        self._step_n = 1

    # ── paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _e):
        p = QPainter(self)
        # Scale full-res base down to logical widget size at paint time.
        # SmoothPixmapTransform ensures high-quality interpolation — no data is
        # thrown away, the downscale happens only on-screen each frame.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), self._base)
        p.drawPixmap(0, 0, self._overlay)

        if self._drawing:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self._tool in ("pencil", "highlight"):
                self._paint_path(p, self._path)
            elif self._tool == "step":
                self._paint_step(p, self._p1, self._step_n)
            elif self._tool == "blur":
                # live preview: dashed border over the drag region
                sel = QRect(self._p1, self._p2).normalized()
                p.setPen(QPen(QColor("#ffffff"), 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(0, 0, 0, 40))
                p.drawRect(sel)
            else:
                self._apply_pen(p)
                self._draw_shape(p, self._p1, self._p2)

    # ── mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return

        # commit any open text box first
        if self._text_widget:
            self._commit_text()
            return

        pos = ev.pos()

        if self._tool == "text":
            self._start_text_input(pos)

        elif self._tool == "step":
            # step is click-to-place, not drag
            self._p1 = pos
            self._drawing = True
            self._commit_step(pos)
            self._drawing = False

        elif self._tool in ("pencil", "highlight"):
            self._path = [pos]
            self._drawing = True

        else:
            self._p1 = self._p2 = pos
            self._drawing = True

        self.update()

    def mouseMoveEvent(self, ev):
        if not self._drawing:
            return
        if self._tool in ("pencil", "highlight"):
            self._path.append(ev.pos())
        else:
            self._p2 = ev.pos()
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or not self._drawing:
            return
        self._drawing = False

        if self._tool in ("pencil", "highlight"):
            self._path.append(ev.pos())
            self._commit_path()
        elif self._tool == "blur":
            self._p2 = ev.pos()
            self._commit_blur()
        else:
            self._p2 = ev.pos()
            self._commit()

        self.update()

    # ── drawing helpers ───────────────────────────────────────────────────────

    def _apply_pen(self, p: QPainter, color: QColor | None = None,
                   width: int | None = None):
        pen = QPen(
            color or self._color,
            width or self._thickness,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_shape(self, p: QPainter, p1: QPoint, p2: QPoint):
        if p1 == p2:
            return
        t = self._tool
        if t == "rect":
            p.drawRect(QRect(p1, p2).normalized())
        elif t == "ellipse":
            p.drawEllipse(QRect(p1, p2).normalized())
        elif t == "line":
            p.drawLine(p1, p2)
        elif t == "blur":
            pass  # handled separately in _commit_blur
        else:  # arrow
            self._draw_arrow(p, p1, p2)

    def _draw_arrow(self, p: QPainter, p1: QPoint, p2: QPoint):
        p.drawLine(p1, p2)
        dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
        lng = math.hypot(dx, dy)
        if lng < 1:
            return
        ux, uy  = dx / lng, dy / lng
        head_sz = max(12, self._thickness * 4)
        ca, sa  = math.cos(0.45), math.sin(0.45)
        lx = p2.x() - head_sz * (ux * ca - uy * sa)
        ly = p2.y() - head_sz * (uy * ca + ux * sa)
        rx = p2.x() - head_sz * (ux * ca + uy * sa)
        ry = p2.y() - head_sz * (uy * ca - ux * sa)
        p.setBrush(QBrush(self._color))
        p.drawPolygon(QPolygon([p2, QPoint(int(lx), int(ly)), QPoint(int(rx), int(ry))]))

    def _paint_path(self, p: QPainter, pts: list[QPoint]):
        if len(pts) < 2:
            return
        if self._tool == "highlight":
            c = QColor(self._color.red(), self._color.green(),
                       self._color.blue(), 80)
            pen = QPen(c, max(16, self._thickness * 6),
                       Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        else:
            pen = QPen(self._color, self._thickness,
                       Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        for i in range(len(pts) - 1):
            p.drawLine(pts[i], pts[i + 1])

    def _paint_step(self, p: QPainter, center: QPoint, n: int):
        r = 13 + self._thickness
        p.setBrush(QBrush(self._color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, r, r)
        p.setPen(QColor("white"))
        p.setFont(QFont("Segoe UI", max(9, r - 4), QFont.Weight.Bold))
        p.drawText(QRect(center.x() - r, center.y() - r, r * 2, r * 2),
                   Qt.AlignmentFlag.AlignCenter, str(n))

    # ── commit helpers ────────────────────────────────────────────────────────

    def _commit(self):
        if self._p1 == self._p2:
            return
        self._history.append(self._overlay.copy())
        pa = QPainter(self._overlay)
        pa.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._apply_pen(pa)
        self._draw_shape(pa, self._p1, self._p2)
        pa.end()

    def _commit_path(self):
        if len(self._path) < 2:
            return
        self._history.append(self._overlay.copy())
        pa = QPainter(self._overlay)
        pa.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_path(pa, self._path)
        pa.end()
        self._path = []

    def _commit_blur(self):
        """Pixelate the selected region to hide sensitive content."""
        rect = QRect(self._p1, self._p2).normalized()
        if rect.width() < 4 or rect.height() < 4:
            return
        self._history.append(self._overlay.copy())

        # Work in plain pixel space so coords match mouse events (logical px).
        # QPixmap.scaled() preserves the source DPR in Qt6 — if we leave DPR=2
        # on base_logical, QPainter opens in a shrunken logical coord system and
        # copy() treats the mouse-event rect as physical offsets → wrong region.
        # Stripping DPR to 1.0 makes pixel coords == logical coords throughout.
        base_logical = self._base.scaled(
            self._logical_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        base_logical.setDevicePixelRatio(1.0)   # ← fix: plain pixel coords
        pa_m = QPainter(base_logical)
        pa_m.drawPixmap(0, 0, self._overlay)
        pa_m.end()
        region = base_logical.copy(rect)

        # Pixelate: shrink to ~10px blocks then enlarge back
        block  = max(6, min(rect.width(), rect.height()) // 8)
        sw     = max(1, rect.width()  // block)
        sh     = max(1, rect.height() // block)
        small  = region.scaled(sw, sh,
                               Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.FastTransformation)
        mosaic = small.scaled(rect.width(), rect.height(),
                              Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.FastTransformation)

        pa = QPainter(self._overlay)
        pa.drawPixmap(rect.topLeft(), mosaic)
        pa.end()
        self.update()

    def _commit_step(self, center: QPoint):
        self._history.append(self._overlay.copy())
        pa = QPainter(self._overlay)
        pa.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_step(pa, center, self._step_n)
        pa.end()
        self._step_n += 1
        self.update()

    # ── text tool ─────────────────────────────────────────────────────────────

    def _start_text_input(self, pos: QPoint):
        te = QLineEdit(self)
        te.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        te.setStyleSheet(
            f"QLineEdit {{ color: {self._color.name()};"
            "  background: rgba(0,0,0,160);"
            f"  border: 1px dashed {self._color.name()};"
            "  border-radius: 3px; padding: 2px 6px; }}"
        )
        te.setMinimumWidth(160)
        te.move(pos)
        te.resize(200, 32)
        te.show()
        te.setFocus()
        self._text_widget = te
        self._text_pos    = pos
        te.returnPressed.connect(self._commit_text)

    def _commit_text(self):
        if not self._text_widget:
            return
        text = self._text_widget.text().strip()
        pos  = self._text_pos
        self._text_widget.hide()
        self._text_widget.deleteLater()
        self._text_widget = None
        if text:
            self._history.append(self._overlay.copy())
            pa = QPainter(self._overlay)
            pa.setRenderHint(QPainter.RenderHint.Antialiasing)
            pa.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
            # subtle shadow
            pa.setPen(QColor(0, 0, 0, 160))
            pa.drawText(pos + QPoint(1, 1), text)
            pa.setPen(self._color)
            pa.drawText(pos, text)
            pa.end()
            self.update()

    # ── public ────────────────────────────────────────────────────────────────

    def undo(self):
        if self._text_widget:
            self._text_widget.hide()
            self._text_widget.deleteLater()
            self._text_widget = None
            return
        if self._history:
            self._overlay = self._history.pop()
            self.update()

    def merged_pixmap(self) -> QPixmap:
        """Return a full-resolution composite (base + annotations) for copy/save."""
        if self._text_widget:
            self._commit_text()
        result = self._base.copy()
        # Strip DPR so QPainter on result uses physical pixel coordinates.
        # Leaving DPR > 1 would make the painter's logical coord system
        # smaller than the physical canvas, clipping annotations to a corner.
        result.setDevicePixelRatio(1.0)
        # Scale logical overlay up to physical resolution before compositing.
        overlay_scaled = self._overlay.scaled(
            result.width(), result.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        p = QPainter(result)
        p.drawPixmap(0, 0, overlay_scaled)
        p.end()
        return result


# ─────────────────────────────────────────────────────────────────────────────
#  Annotation window
# ─────────────────────────────────────────────────────────────────────────────

# ── shared styles ─────────────────────────────────────────────────────────────
# v1.4 — Fluent Design icon buttons
#   • Uses Segoe Fluent Icons (Win 11) / Segoe MDL2 Assets (Win 10) — both ship
#     with Windows, no extra files needed.
#   • Transparent backgrounds, blue glow on active tool (matches Snipping Tool)
#   • 40×40 px buttons with 18 px icons

_ICON_FONT = "'Segoe Fluent Icons', 'Segoe MDL2 Assets'"

# Tool toggle buttons (rect, ellipse, arrow …)
_TOOL_BTN_SS = f"""
QPushButton {{
    background: transparent;
    color: #c8c8c8;
    border: 1px solid transparent;
    border-radius: 6px;
    font-family: {_ICON_FONT};
    font-size: 18px;
    min-width:  40px; max-width:  40px;
    min-height: 40px; max-height: 40px;
    padding: 0px;
}}
QPushButton:hover   {{ background: rgba(255,255,255,0.08); color: #ffffff; }}
QPushButton:checked {{ background: rgba(0,120,212,0.20); color: #4cc2ff;
                       border-color: rgba(0,120,212,0.55); }}
QPushButton:pressed {{ background: rgba(255,255,255,0.05); }}
"""

# Action icon buttons (undo, copy, save, pin, close)
_ACT_BTN_SS = f"""
QPushButton {{
    background: transparent;
    color: #c8c8c8;
    border: 1px solid transparent;
    border-radius: 6px;
    font-family: {_ICON_FONT};
    font-size: 18px;
    min-width:  40px; max-width:  40px;
    min-height: 40px; max-height: 40px;
    padding: 0px;
}}
QPushButton:hover   {{ background: rgba(255,255,255,0.08); color: #ffffff; }}
QPushButton:checked {{ background: rgba(0,120,212,0.20); color: #4cc2ff;
                       border-color: rgba(0,120,212,0.55); }}
QPushButton:pressed {{ background: rgba(255,255,255,0.05); }}
"""

# Text-label buttons (step reset "①→1")
_BTN = """
QPushButton {
    background: transparent;
    color: #c8c8c8;
    border: 1px solid transparent;
    border-radius: 6px;
    font-family: 'Segoe UI';
    font-size: 12px;
    font-weight: 500;
    min-height: 40px;
    padding: 0px 12px;
}
QPushButton:hover   { background: rgba(255,255,255,0.08); color: #ffffff; }
QPushButton:pressed { background: rgba(255,255,255,0.05); }
"""

def _make_color_icon(color: QColor, size: int = 14) -> QIcon:
    """Small filled circle showing the current annotation color.
    Rendered as a QIcon so the button matches the flat transparent style
    of all other toolbar buttons — no oversized solid-fill square."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(255, 255, 255, 55), 1.2))
    p.setBrush(QBrush(color))
    r = size // 2 - 1
    p.drawEllipse(QPoint(size // 2, size // 2), r, r)
    p.end()
    return QIcon(pm)


def _sep():
    """Thin vertical divider between toolbar groups."""
    s = QFrame()
    s.setFrameShape(QFrame.Shape.VLine)
    s.setFixedSize(1, 24)
    s.setStyleSheet("background: rgba(255,255,255,0.12);")
    return s


def _tool_btn(symbol: str, tip: str) -> QPushButton:
    b = QPushButton(symbol)
    b.setCheckable(True)
    b.setToolTip(tip)
    b.setStyleSheet(_TOOL_BTN_SS)
    return b


def _make_anno_icon(name: str, size: int = 20) -> QIcon:
    """
    Custom-drawn icons for tools that have no suitable Segoe Fluent glyph:
      line, arrow, blur, step.
    Drawn in the same #c8c8c8 colour as the Fluent icon buttons so they blend in.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor("#c8c8c8")

    if name == "line":
        # Diagonal line bottom-left → top-right
        p.setPen(QPen(c, 2.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(3, size - 3, size - 3, 3)

    elif name == "arrow":
        # Same diagonal line + filled arrowhead at the tip
        p.setPen(QPen(c, 2.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(4, size - 4, size - 5, 5)
        tip   = QPoint(size - 4, 4)
        left  = QPoint(size - 4, 4 + 8)
        right = QPoint(size - 4 - 8, 4)
        p.setBrush(QBrush(c))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygon([tip, left, right]))

    elif name == "blur":
        # 3 × 3 mosaic tiles — visually suggests pixelation / redaction
        tile = (size - 4) // 3
        for row in range(3):
            for col in range(3):
                alpha = 210 if (row + col) % 2 == 0 else 80
                p.fillRect(
                    2 + col * tile, 2 + row * tile,
                    tile - 1, tile - 1,
                    QColor(c.red(), c.green(), c.blue(), alpha),
                )

    elif name == "step":
        # Circle outline with "1" — conveys numbered step markers
        r  = size // 2 - 2
        cx = cy = size // 2
        p.setPen(QPen(c, 1.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPoint(cx, cy), r, r)
        p.setPen(c)
        f = QFont("Segoe UI", max(6, size // 3), QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRect(0, 0, size, size),
                   Qt.AlignmentFlag.AlignCenter, "1")

    p.end()
    return QIcon(pm)


class AnnotationWindow(QMainWindow):
    def __init__(self, pixmap: QPixmap):
        super().__init__()
        self.setWindowTitle("SnapMark — Annotate")
        self._pinned = False
        self.setWindowFlags(Qt.WindowType.Window)
        self.setStyleSheet("background: #1e1e1e;")

        self._canvas = Canvas(pixmap)

        # ── tool buttons ─────────────────────────────────────────────────────
        # Segoe Fluent Icons codepoints (built-in on Windows 10/11 — no files needed)
        self._tool_btns: dict[str, QPushButton] = {}
        tools = [
            ("rect",      "",  "Rectangle  (R)"),        # RectangularClipping
            ("ellipse",   "",  "Ellipse  (E)"),           # CircleRing
            ("line",      "",  "Straight Line  (L)"),     # Draw
            ("arrow",     "",  "Arrow  (A)"),             # InkingTool
            ("pencil",    "",  "Pencil — freehand  (P)"), # Pencil
            ("highlight", "",  "Highlighter  (H)"),       # Marker
            ("blur",      "",  "Blur / Redact  (B)"),     # ClippingTool
            ("text",      "",  "Add Text  (T)"),           # Font
            ("step",      "",  "Step Numbers  (S)"),      # CircleFill
        ]
        for key, sym, tip in tools:
            b = _tool_btn(sym, tip)
            b.clicked.connect(lambda _, k=key: self._set_tool(k))
            self._tool_btns[key] = b

        # Replace Fluent-font glyphs with custom-drawn icons for the four tools
        # that don't have a suitable glyph in the font (line, arrow, blur, step).
        for name in ("line", "arrow", "blur", "step"):
            btn = self._tool_btns[name]
            btn.setText("")
            btn.setIcon(_make_anno_icon(name, 20))
            btn.setIconSize(QSize(20, 20))

        self._tool_btns["arrow"].setChecked(True)

        # ── toolbar ───────────────────────────────────────────────────────────
        tb = QWidget()
        tb.setStyleSheet("background: #1e1e1e; border-bottom: 1px solid #2d2d2d;")
        tbl = QHBoxLayout(tb)
        tbl.setContentsMargins(12, 5, 12, 5)
        tbl.setSpacing(2)

        for b in self._tool_btns.values():
            tbl.addWidget(b)

        tbl.addSpacing(4)
        tbl.addWidget(_sep())
        tbl.addSpacing(4)

        # color swatch
        self._color_btn = QPushButton()
        self._color_btn.setToolTip("Annotation color")
        self._color_btn.setStyleSheet(_ACT_BTN_SS)
        self._color_btn.setIcon(_make_color_icon(QColor("#e53935")))
        self._color_btn.setIconSize(QSize(14, 14))
        self._color_btn.clicked.connect(self._pick_color)
        tbl.addWidget(self._color_btn)

        tbl.addSpacing(3)

        # stroke size — compact  −  [n]  +  row, no QSpinBox (its arrows can't
        # be cleanly themed without external image files)
        _MINI = """
QPushButton {
    background: transparent; color: #c8c8c8; border: none;
    font-family: 'Segoe UI'; font-size: 16px; font-weight: 300;
    min-width: 22px; max-width: 22px;
    min-height: 40px; max-height: 40px;
    padding: 0;
}
QPushButton:hover   { color: #ffffff; background: rgba(255,255,255,0.08);
                      border-radius: 5px; }
QPushButton:pressed { background: rgba(255,255,255,0.05); border-radius: 5px; }
"""
        self._sz_value = 3

        btn_sz_dec = QPushButton("−")
        btn_sz_dec.setStyleSheet(_MINI)
        btn_sz_dec.setToolTip("Decrease stroke thickness")

        self._sz_label = QLabel("3")
        self._sz_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sz_label.setFixedWidth(18)
        self._sz_label.setStyleSheet(
            "QLabel { color: #c8c8c8; font-family: 'Segoe UI'; font-size: 13px; }"
        )

        btn_sz_inc = QPushButton("+")
        btn_sz_inc.setStyleSheet(_MINI)
        btn_sz_inc.setToolTip("Increase stroke thickness")

        def _sz_dec():
            v = max(1, self._sz_value - 1)
            self._sz_value = v
            self._sz_label.setText(str(v))
            self._canvas.thickness = v

        def _sz_inc():
            v = min(20, self._sz_value + 1)
            self._sz_value = v
            self._sz_label.setText(str(v))
            self._canvas.thickness = v

        btn_sz_dec.clicked.connect(_sz_dec)
        btn_sz_inc.clicked.connect(_sz_inc)

        sz_row = QWidget()
        sz_row.setStyleSheet("background: transparent;")
        sz_layout = QHBoxLayout(sz_row)
        sz_layout.setContentsMargins(0, 0, 0, 0)
        sz_layout.setSpacing(1)
        sz_layout.addWidget(btn_sz_dec)
        sz_layout.addWidget(self._sz_label)
        sz_layout.addWidget(btn_sz_inc)
        tbl.addWidget(sz_row)

        tbl.addSpacing(4)
        tbl.addWidget(_sep())
        tbl.addSpacing(4)

        # Undo — Segoe Fluent
        btn_undo = QPushButton("")
        btn_undo.setToolTip("Undo  (Ctrl+Z)")
        btn_undo.setStyleSheet(_ACT_BTN_SS)
        btn_undo.clicked.connect(self._canvas.undo)
        tbl.addWidget(btn_undo)

        tbl.addSpacing(2)
        tbl.addWidget(_sep())
        tbl.addSpacing(2)

        # Copy —    Save — 
        btn_copy = QPushButton("")
        btn_copy.setToolTip("Copy to clipboard  (Ctrl+C)")
        btn_copy.setStyleSheet(_ACT_BTN_SS)
        btn_copy.clicked.connect(self._copy)
        tbl.addWidget(btn_copy)

        btn_save = QPushButton("")
        btn_save.setToolTip("Save as PNG / JPG  (Ctrl+S)")
        btn_save.setStyleSheet(_ACT_BTN_SS)
        btn_save.clicked.connect(self._save)
        tbl.addWidget(btn_save)

        tbl.addSpacing(2)
        tbl.addWidget(_sep())
        tbl.addSpacing(2)

        # Pin —  (pinned) /  (unpinned)
        self._pin_btn = QPushButton("")
        self._pin_btn.setToolTip("Always on top — OFF")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(False)
        self._pin_btn.setStyleSheet(_ACT_BTN_SS)
        self._pin_btn.clicked.connect(self._toggle_pin)
        tbl.addWidget(self._pin_btn)

        # Close — 
        btn_close = QPushButton("")
        btn_close.setToolTip("Close")
        btn_close.setStyleSheet(_ACT_BTN_SS)
        btn_close.clicked.connect(self.close)
        tbl.addWidget(btn_close)

        tbl.addStretch()

        # ── scrollable canvas ─────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setStyleSheet("QScrollArea { border: none; background: #141414; }"
                             " QScrollBar:vertical { background: #1e1e1e; width: 8px; border-radius: 4px; }"
                             " QScrollBar::handle:vertical { background: #3a3a3a; border-radius: 4px; }"
                             " QScrollBar:horizontal { background: #1e1e1e; height: 8px; border-radius: 4px; }"
                             " QScrollBar::handle:horizontal { background: #3a3a3a; border-radius: 4px; }")

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(tb)
        root.addWidget(scroll)
        self.setCentralWidget(central)

        # ── window sizing ─────────────────────────────────────────────────────
        canvas_sz = self._canvas.size()
        avail = QApplication.primaryScreen().availableGeometry()
        w = min(canvas_sz.width()  + 24,  avail.width()  - 60)
        h = min(canvas_sz.height() + 110, avail.height() - 60)
        self.resize(w, h)
        self.move(
            avail.x() + (avail.width()  - w) // 2,
            avail.y() + (avail.height() - h) // 2,
        )

    # ── keyboard shortcuts ────────────────────────────────────────────────────

    def keyPressEvent(self, ev):
        mods = ev.modifiers()
        key  = ev.key()
        ctrl = Qt.KeyboardModifier.ControlModifier
        key_map = {
            Qt.Key.Key_Z: self._canvas.undo,
            Qt.Key.Key_C: self._copy,
            Qt.Key.Key_S: self._save,
        }
        tool_map = {
            Qt.Key.Key_R: "rect",
            Qt.Key.Key_E: "ellipse",
            Qt.Key.Key_L: "line",
            Qt.Key.Key_A: "arrow",
            Qt.Key.Key_P: "pencil",
            Qt.Key.Key_H: "highlight",
            Qt.Key.Key_B: "blur",
            Qt.Key.Key_T: "text",
            Qt.Key.Key_S: "step",
        }
        if mods & ctrl and key in key_map:
            key_map[key]()
        elif not (mods & ctrl) and key in tool_map:
            self._set_tool(tool_map[key])
        else:
            super().keyPressEvent(ev)

    # ── toolbar helpers ───────────────────────────────────────────────────────

    def _set_tool(self, tool: str):
        self._canvas.tool = tool
        for k, b in self._tool_btns.items():
            b.setChecked(k == tool)

    def _toggle_pin(self):
        self._pinned = not self._pinned
        flags = Qt.WindowType.Window
        if self._pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        # Swap icon: E718 = Pin (pinned), E77A = Unpin (unpinned)
        self._pin_btn.setText("" if self._pinned else "")
        tip = "Always on top — ON" if self._pinned else "Always on top — OFF"
        self._pin_btn.setToolTip(tip)
        self._pin_btn.setChecked(self._pinned)
        self.setWindowFlags(flags)
        self.show()

    def _refresh_color_btn(self, color: QColor):
        self._color_btn.setIcon(_make_color_icon(color))

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

    def _on_area(self, x: int, y: int, w: int, h: int, dpr: float):
        # Hide all other overlays immediately (user picked a region on one screen)
        self._hide_all()
        # Short delay so overlays fully vanish before we grab pixels
        QTimer.singleShot(80, lambda: self._capture(x, y, w, h, dpr))

    def _capture(self, x: int, y: int, w: int, h: int, dpr: float):
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
        # Tag the pixmap with its DPR so Canvas can display it at logical size
        pixmap.setDevicePixelRatio(dpr)
        # Auto-copy the raw capture to clipboard immediately
        QApplication.clipboard().setPixmap(pixmap)
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
