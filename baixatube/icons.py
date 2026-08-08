"""Small, dependency-free vector icons for the BraXYTDow interface.

The glyphs are drawn on a 24 x 24 logical grid and packaged into ``QIcon``
instances at multiple device-pixel ratios.  Keeping the artwork here avoids
platform-dependent emoji and icon-font rendering while still allowing every
control to inherit the colour that belongs to its current state.
"""

from __future__ import annotations

from functools import lru_cache
from math import cos, pi, sin
from typing import Callable

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF


DEFAULT_COLOR = "#AFC5E8"

ICON_NAMES = frozenset(
    {
        "brand",
        "brand/play",
        "play",
        "download",
        "queue",
        "history",
        "tools",
        "settings",
        "credits",
        "search",
        "paste",
        "folder",
        "pause",
        "retry",
        "cancel",
        "trash",
        "external",
        "link",
        "check",
        "warning",
        "audio",
        "video",
    }
)

_ALIASES = {"brand/play": "brand"}
_DEVICE_PIXEL_RATIOS = (1.0, 2.0, 3.0)


def _line(painter: QPainter, x1: float, y1: float, x2: float, y2: float) -> None:
    painter.drawLine(QLineF(x1, y1, x2, y2))


def _polyline(painter: QPainter, *points: tuple[float, float]) -> None:
    painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))


def _draw_play_mark(painter: QPainter, inset: float = 0.0) -> None:
    path = QPainterPath()
    path.moveTo(8.3 + inset, 6.2 + inset * 0.45)
    path.cubicTo(8.3 + inset, 5.5, 9.15 + inset, 5.1, 9.8 + inset, 5.55)
    path.lineTo(17.45 - inset, 10.75 + inset * 0.15)
    path.cubicTo(18.25 - inset, 11.28, 18.25 - inset, 12.72, 17.45 - inset, 13.25 - inset * 0.15)
    path.lineTo(9.8 + inset, 18.45 - inset * 0.45)
    path.cubicTo(9.15 + inset, 18.9, 8.3 + inset, 18.5, 8.3 + inset, 17.8 - inset * 0.45)
    path.closeSubpath()
    colour = painter.pen().color()
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    painter.drawPath(path)
    painter.restore()


def _draw_brand(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(3.0, 3.0, 18.0, 18.0))
    _draw_play_mark(painter, 1.45)


def _draw_play(painter: QPainter) -> None:
    _draw_play_mark(painter)


def _draw_download(painter: QPainter) -> None:
    _line(painter, 12, 3.5, 12, 14.7)
    _polyline(painter, (7.6, 10.6), (12, 15.0), (16.4, 10.6))
    path = QPainterPath()
    path.moveTo(4.2, 16.5)
    path.lineTo(4.2, 18.1)
    path.cubicTo(4.2, 19.4, 5.25, 20.5, 6.6, 20.5)
    path.lineTo(17.4, 20.5)
    path.cubicTo(18.75, 20.5, 19.8, 19.4, 19.8, 18.1)
    path.lineTo(19.8, 16.5)
    painter.drawPath(path)


def _draw_queue(painter: QPainter) -> None:
    for y in (6.0, 12.0, 18.0):
        painter.drawEllipse(QRectF(3.3, y - 0.8, 1.6, 1.6))
    _line(painter, 7.3, 6.0, 20.0, 6.0)
    _line(painter, 7.3, 12.0, 15.0, 12.0)
    _line(painter, 7.3, 18.0, 20.0, 18.0)
    path = QPainterPath()
    path.moveTo(17.0, 9.4)
    path.lineTo(20.8, 12.0)
    path.lineTo(17.0, 14.6)
    path.closeSubpath()
    colour = painter.pen().color()
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    painter.drawPath(path)
    painter.restore()


def _draw_history(painter: QPainter) -> None:
    path = QPainterPath()
    path.arcMoveTo(QRectF(4.0, 4.0, 16.0, 16.0), 132)
    path.arcTo(QRectF(4.0, 4.0, 16.0, 16.0), 132, 292)
    painter.drawPath(path)
    _polyline(painter, (3.2, 5.4), (4.3, 10.0), (8.4, 7.8))
    _line(painter, 12.0, 7.4, 12.0, 12.1)
    _line(painter, 12.0, 12.1, 15.5, 14.2)


def _draw_tools(painter: QPainter) -> None:
    # Open-ended wrench, crossed by a compact screwdriver.
    path = QPainterPath()
    path.moveTo(5.0, 4.1)
    path.cubicTo(3.8, 6.8, 4.5, 9.6, 6.7, 10.8)
    path.lineTo(14.7, 18.8)
    path.cubicTo(15.55, 19.65, 16.95, 19.65, 17.8, 18.8)
    path.cubicTo(18.65, 17.95, 18.65, 16.55, 17.8, 15.7)
    path.lineTo(9.8, 7.7)
    path.cubicTo(9.7, 5.1, 7.2, 3.25, 4.8, 4.0)
    path.lineTo(7.0, 6.2)
    path.lineTo(6.25, 7.8)
    path.lineTo(4.65, 8.5)
    painter.drawPath(path)
    _line(painter, 15.9, 4.2, 19.7, 8.0)
    _line(painter, 18.9, 3.3, 20.6, 5.0)
    _line(painter, 14.2, 10.0, 18.8, 5.4)


def _draw_settings(painter: QPainter) -> None:
    path = QPainterPath()
    teeth = 8
    for index in range(teeth * 4):
        phase = index % 4
        radius = 9.0 if phase in (1, 2) else 7.35
        angle = -pi / 2 + index * (2 * pi / (teeth * 4))
        point = QPointF(12 + cos(angle) * radius, 12 + sin(angle) * radius)
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawEllipse(QRectF(9.0, 9.0, 6.0, 6.0))


def _draw_credits(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(4.0, 2.8, 16.0, 18.4), 2.6, 2.6)
    painter.drawEllipse(QRectF(9.2, 6.0, 5.6, 5.6))
    path = QPainterPath()
    path.moveTo(7.4, 17.6)
    path.cubicTo(8.0, 14.9, 9.6, 13.6, 12.0, 13.6)
    path.cubicTo(14.4, 13.6, 16.0, 14.9, 16.6, 17.6)
    painter.drawPath(path)


def _draw_search(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(3.6, 3.6, 12.8, 12.8))
    _line(painter, 15.0, 15.0, 20.4, 20.4)


def _draw_paste(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(5.0, 4.7, 14.0, 16.3), 2.0, 2.0)
    path = QPainterPath()
    path.moveTo(9.2, 5.6)
    path.lineTo(9.2, 4.8)
    path.cubicTo(9.2, 3.8, 10.0, 3.0, 11.0, 3.0)
    path.lineTo(13.0, 3.0)
    path.cubicTo(14.0, 3.0, 14.8, 3.8, 14.8, 4.8)
    path.lineTo(14.8, 5.6)
    painter.drawPath(path)
    _line(painter, 8.5, 10.5, 15.5, 10.5)
    _line(painter, 8.5, 14.3, 15.5, 14.3)
    _line(painter, 8.5, 18.1, 13.0, 18.1)


def _draw_folder(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(3.0, 7.4)
    path.lineTo(3.0, 18.1)
    path.cubicTo(3.0, 19.4, 4.0, 20.4, 5.3, 20.4)
    path.lineTo(18.7, 20.4)
    path.cubicTo(20.0, 20.4, 21.0, 19.4, 21.0, 18.1)
    path.lineTo(21.0, 8.3)
    path.cubicTo(21.0, 7.2, 20.1, 6.3, 19.0, 6.3)
    path.lineTo(12.0, 6.3)
    path.lineTo(10.2, 4.3)
    path.lineTo(5.2, 4.3)
    path.cubicTo(4.0, 4.3, 3.0, 5.3, 3.0, 6.5)
    path.closeSubpath()
    painter.drawPath(path)


def _draw_pause(painter: QPainter) -> None:
    pen = painter.pen()
    pen.setWidthF(3.0)
    painter.save()
    painter.setPen(pen)
    _line(painter, 8.3, 5.0, 8.3, 19.0)
    _line(painter, 15.7, 5.0, 15.7, 19.0)
    painter.restore()


def _draw_retry(painter: QPainter) -> None:
    path = QPainterPath()
    path.arcMoveTo(QRectF(4.0, 4.0, 16.0, 16.0), 44)
    path.arcTo(QRectF(4.0, 4.0, 16.0, 16.0), 44, 286)
    painter.drawPath(path)
    _polyline(painter, (18.0, 3.5), (19.8, 8.1), (15.0, 7.5))


def _draw_cancel(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(3.0, 3.0, 18.0, 18.0))
    _line(painter, 8.2, 8.2, 15.8, 15.8)
    _line(painter, 15.8, 8.2, 8.2, 15.8)


def _draw_trash(painter: QPainter) -> None:
    _line(painter, 4.3, 6.5, 19.7, 6.5)
    path = QPainterPath()
    path.moveTo(6.3, 6.5)
    path.lineTo(7.1, 19.0)
    path.cubicTo(7.2, 20.1, 8.0, 20.8, 9.1, 20.8)
    path.lineTo(14.9, 20.8)
    path.cubicTo(16.0, 20.8, 16.8, 20.1, 16.9, 19.0)
    path.lineTo(17.7, 6.5)
    painter.drawPath(path)
    path = QPainterPath()
    path.moveTo(9.0, 6.0)
    path.lineTo(9.4, 4.3)
    path.cubicTo(9.55, 3.65, 10.1, 3.2, 10.8, 3.2)
    path.lineTo(13.2, 3.2)
    path.cubicTo(13.9, 3.2, 14.45, 3.65, 14.6, 4.3)
    path.lineTo(15.0, 6.0)
    painter.drawPath(path)
    _line(painter, 10.0, 10.0, 10.4, 17.2)
    _line(painter, 14.0, 10.0, 13.6, 17.2)


def _draw_external(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(11.0, 5.0)
    path.lineTo(6.1, 5.0)
    path.cubicTo(4.9, 5.0, 4.0, 5.9, 4.0, 7.1)
    path.lineTo(4.0, 17.9)
    path.cubicTo(4.0, 19.1, 4.9, 20.0, 6.1, 20.0)
    path.lineTo(16.9, 20.0)
    path.cubicTo(18.1, 20.0, 19.0, 19.1, 19.0, 17.9)
    path.lineTo(19.0, 13.0)
    painter.drawPath(path)
    _polyline(painter, (14.0, 4.0), (20.0, 4.0), (20.0, 10.0))
    _line(painter, 19.6, 4.4, 11.0, 13.0)


def _draw_link(painter: QPainter) -> None:
    first = QPainterPath()
    first.moveTo(9.6, 14.4)
    first.lineTo(7.6, 16.4)
    first.cubicTo(6.35, 17.65, 4.35, 17.65, 3.1, 16.4)
    first.cubicTo(1.85, 15.15, 1.85, 13.15, 3.1, 11.9)
    first.lineTo(6.4, 8.6)
    first.cubicTo(7.65, 7.35, 9.65, 7.35, 10.9, 8.6)
    painter.drawPath(first)
    second = QPainterPath()
    second.moveTo(13.1, 15.4)
    second.cubicTo(14.35, 16.65, 16.35, 16.65, 17.6, 15.4)
    second.lineTo(20.9, 12.1)
    second.cubicTo(22.15, 10.85, 22.15, 8.85, 20.9, 7.6)
    second.cubicTo(19.65, 6.35, 17.65, 6.35, 16.4, 7.6)
    second.lineTo(14.4, 9.6)
    painter.drawPath(second)
    _line(painter, 8.6, 15.4, 15.4, 8.6)


def _draw_check(painter: QPainter) -> None:
    pen = painter.pen()
    pen.setWidthF(2.25)
    painter.save()
    painter.setPen(pen)
    _polyline(painter, (4.5, 12.2), (9.6, 17.1), (19.6, 6.9))
    painter.restore()


def _draw_warning(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(10.25, 4.3)
    path.cubicTo(11.0, 2.9, 13.0, 2.9, 13.75, 4.3)
    path.lineTo(21.0, 18.0)
    path.cubicTo(21.75, 19.4, 20.75, 21.0, 19.15, 21.0)
    path.lineTo(4.85, 21.0)
    path.cubicTo(3.25, 21.0, 2.25, 19.4, 3.0, 18.0)
    path.closeSubpath()
    painter.drawPath(path)
    _line(painter, 12.0, 8.0, 12.0, 14.0)
    colour = painter.pen().color()
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    painter.drawEllipse(QRectF(10.85, 16.6, 2.3, 2.3))
    painter.restore()


def _draw_audio(painter: QPainter) -> None:
    _line(painter, 10.0, 6.0, 19.0, 4.0)
    _line(painter, 10.0, 6.0, 10.0, 16.5)
    _line(painter, 19.0, 4.0, 19.0, 14.0)
    _line(painter, 10.0, 9.4, 19.0, 7.4)
    painter.drawEllipse(QRectF(4.2, 15.0, 5.8, 4.6))
    painter.drawEllipse(QRectF(13.2, 12.5, 5.8, 4.6))


def _draw_video(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(3.0, 5.0, 13.5, 14.0), 2.5, 2.5)
    path = QPainterPath()
    path.moveTo(16.5, 10.0)
    path.lineTo(20.2, 7.7)
    path.cubicTo(20.95, 7.25, 21.8, 7.8, 21.8, 8.65)
    path.lineTo(21.8, 15.35)
    path.cubicTo(21.8, 16.2, 20.95, 16.75, 20.2, 16.3)
    path.lineTo(16.5, 14.0)
    painter.drawPath(path)


_DRAWERS: dict[str, Callable[[QPainter], None]] = {
    "brand": _draw_brand,
    "play": _draw_play,
    "download": _draw_download,
    "queue": _draw_queue,
    "history": _draw_history,
    "tools": _draw_tools,
    "settings": _draw_settings,
    "credits": _draw_credits,
    "search": _draw_search,
    "paste": _draw_paste,
    "folder": _draw_folder,
    "pause": _draw_pause,
    "retry": _draw_retry,
    "cancel": _draw_cancel,
    "trash": _draw_trash,
    "external": _draw_external,
    "link": _draw_link,
    "check": _draw_check,
    "warning": _draw_warning,
    "audio": _draw_audio,
    "video": _draw_video,
}


def _render_pixmap(name: str, colour: QColor, size: int, device_pixel_ratio: float) -> QPixmap:
    physical_size = max(1, round(size * device_pixel_ratio))
    pixmap = QPixmap(physical_size, physical_size)
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.scale(size / 24.0, size / 24.0)
    pen = QPen(colour, 1.85, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    _DRAWERS[name](painter)
    painter.end()
    return pixmap


@lru_cache(maxsize=256)
def _build_icon(name: str, rgba: int, size: int) -> QIcon:
    colour = QColor.fromRgba(rgba)
    result = QIcon()
    for device_pixel_ratio in _DEVICE_PIXEL_RATIOS:
        result.addPixmap(_render_pixmap(name, colour, size, device_pixel_ratio), QIcon.Normal, QIcon.Off)
    return result


def icon(name: str, color: str = DEFAULT_COLOR, size: int = 20) -> QIcon:
    """Return a cached, resolution-independent-looking icon.

    Args:
        name: One of :data:`ICON_NAMES`. ``"brand/play"`` aliases ``"brand"``.
        color: Any colour string accepted by :class:`QColor`.
        size: Logical square size in device-independent pixels.

    Raises:
        ValueError: If the name, colour, or size is invalid.
        RuntimeError: If called before a Qt GUI application has been created.
    """

    if not isinstance(name, str):
        raise ValueError("icon name must be a string")
    requested_name = name.strip().lower()
    canonical_name = _ALIASES.get(requested_name, requested_name)
    if canonical_name not in _DRAWERS:
        available = ", ".join(sorted(ICON_NAMES))
        raise ValueError(f"unknown icon name {name!r}; available icons: {available}")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("icon size must be a positive integer")

    colour = QColor(color)
    if not colour.isValid():
        raise ValueError(f"invalid icon color: {color!r}")
    if QGuiApplication.instance() is None:
        raise RuntimeError("a QGuiApplication must exist before creating icons")

    # Return an implicitly-shared copy so callers cannot mutate the cache entry.
    return QIcon(_build_icon(canonical_name, colour.rgba(), size))


__all__ = ["DEFAULT_COLOR", "ICON_NAMES", "icon"]
