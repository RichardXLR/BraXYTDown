"""Small, accessible motion primitives for the BraXYTDow interface.

The widgets in this module deliberately keep motion short and monotonic.  They
also honour Qt's ``SH_Widget_Animate`` style hint and expose a local switch so
the application (or the user) can disable animation without replacing a
widget.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QProgressBar, QSizePolicy, QStackedWidget, QStyle, QWidget


__all__ = ["AnimatedProgressBar", "AnimatedStackedWidget", "TransferRail"]


# Named product tokens keep the custom-painted rail and progress bar aligned
# with the application's midnight-blue/cyan visual language.
_MIDNIGHT_INSET = "#111D2B"
_SLATE_TRACK = "#294158"
_SLATE_EDGE = "#3B5872"
_SIGNAL_CYAN = "#43D3F3"
_SIGNAL_BLUE = "#4B91F1"
_SUCCESS_CYAN = "#67E1D0"
_TEXT_PRIMARY = "#EAF7FC"
_TEXT_SECONDARY = "#92A9BC"
_CHECK_INK = "#07151D"


def _style_allows_animation(widget: QWidget) -> bool:
    """Return the platform/theme motion policy for *widget*.

    ``SH_Widget_Animate`` is the Qt-level reduced-motion hook used by native
    styles.  Keeping the check in one place makes every primitive behave the
    same way when a platform or accessibility theme disables animation.
    """

    return bool(
        widget.style().styleHint(
            QStyle.StyleHint.SH_Widget_Animate,
            None,
            widget,
        )
    )


class _FadeOverlay(QWidget):
    """Mouse-transparent snapshot used to fade one stacked page into another."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._opacity = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def present(self, pixmap: QPixmap, geometry) -> None:
        self._pixmap = pixmap
        self._opacity = 1.0
        self.setGeometry(geometry)
        self.show()
        self.raise_()
        self.update()

    def set_opacity(self, opacity: float) -> None:
        self._opacity = max(0.0, min(1.0, float(opacity)))
        self.update()

    def clear(self) -> None:
        self.hide()
        self._pixmap = QPixmap()
        self._opacity = 0.0

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        if self._pixmap.isNull() or self._opacity <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(self.rect(), self._pixmap)


class AnimatedStackedWidget(QStackedWidget):
    """A stacked widget with a short, non-blocking cross-fade.

    A snapshot of the outgoing page fades over the already-visible incoming
    page.  This avoids replacing page graphics effects and keeps focus and
    accessibility on the destination page throughout the transition.

    Calls made while a transition is active are coalesced: only the most recent
    requested page is queued.  This protects the animation from re-entry while
    preserving the user's latest navigation intent.
    """

    transitionStarted = Signal(int, int)
    transitionFinished = Signal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        transition_duration: int = 180,
    ) -> None:
        super().__init__(parent)
        self._animations_enabled = True
        self._transition_duration = max(0, int(transition_duration))
        self._transitioning = False
        self._pending_index: int | None = None

        self._overlay = _FadeOverlay(self)
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setDuration(self._transition_duration)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._overlay.set_opacity)
        self._fade.finished.connect(self._finish_transition)

        self.setAccessibleName("Área principal do aplicativo")
        self.setAccessibleDescription("Conteúdo da seção selecionada")

    def animations_enabled(self) -> bool:
        return self._animations_enabled

    def set_animations_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._animations_enabled == enabled:
            return
        self._animations_enabled = enabled
        if not enabled and self._transitioning:
            self._finish_transition_immediately()

    def transition_duration(self) -> int:
        return self._transition_duration

    def set_transition_duration(self, duration: int) -> None:
        self._transition_duration = max(0, int(duration))
        self._fade.setDuration(self._transition_duration)
        if self._transition_duration == 0 and self._transitioning:
            self._finish_transition_immediately()

    def is_transitioning(self) -> bool:
        return self._transitioning

    def setCurrentIndex(self, index: int, *, animate: bool = True) -> None:  # noqa: N802
        index = int(index)
        if not 0 <= index < self.count():
            return
        if self._transitioning:
            self._pending_index = None if index == self.currentIndex() else index
            return
        if index == self.currentIndex():
            self._pending_index = None
            return

        should_animate = (
            animate
            and self._animations_enabled
            and self._transition_duration > 0
            and self.isVisible()
            and _style_allows_animation(self)
            and self.currentWidget() is not None
        )
        if not should_animate:
            QStackedWidget.setCurrentIndex(self, index)
            return

        outgoing = self.currentWidget()
        assert outgoing is not None
        snapshot = outgoing.grab()
        if snapshot.isNull():
            QStackedWidget.setCurrentIndex(self, index)
            return

        previous_index = self.currentIndex()
        self._transitioning = True
        self._pending_index = None
        self.transitionStarted.emit(previous_index, index)
        QStackedWidget.setCurrentIndex(self, index)

        destination = self.currentWidget()
        geometry = destination.geometry() if destination is not None else self.contentsRect()
        self._overlay.present(snapshot, geometry)
        self._fade.setDuration(self._transition_duration)
        self._fade.start()

    def setCurrentWidget(self, widget: QWidget, *, animate: bool = True) -> None:  # noqa: N802
        index = self.indexOf(widget)
        if index >= 0:
            self.setCurrentIndex(index, animate=animate)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().resizeEvent(event)
        if self._transitioning:
            destination = self.currentWidget()
            self._overlay.setGeometry(destination.geometry() if destination is not None else self.contentsRect())

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.StyleChange
            and self._transitioning
            and not _style_allows_animation(self)
        ):
            self._finish_transition_immediately()

    def _finish_transition_immediately(self) -> None:
        self._fade.stop()
        self._finish_transition()

    def _finish_transition(self) -> None:
        if not self._transitioning:
            return
        self._overlay.clear()
        self._transitioning = False

        finished_index = self.currentIndex()
        queued_index = self._pending_index
        self._pending_index = None
        self.transitionFinished.emit(finished_index)

        # A transitionFinished handler may itself navigate.  That newer action
        # wins over the request captured before the signal was emitted.
        if self._transitioning or self.currentIndex() != finished_index:
            return
        if queued_index is not None and queued_index != finished_index:
            self.setCurrentIndex(queued_index)


class TransferRail(QWidget):
    """A four-stage transfer status rail with a restrained activity sweep."""

    STAGE_COUNT = 4
    DEFAULT_STAGES = ("Link", "Preparação", "Download", "Finalização")

    activeStageChanged = Signal(int)
    completedStagesChanged = Signal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        stages: Sequence[str] | None = None,
        sweep_duration: int = 1700,
    ) -> None:
        super().__init__(parent)
        labels = tuple(str(label).strip() for label in (stages or self.DEFAULT_STAGES))
        if len(labels) != self.STAGE_COUNT or any(not label for label in labels):
            raise ValueError("TransferRail requires exactly four non-empty stage labels")

        self._stages = labels
        self._active_stage: int | None = 0
        self._completed_stages = 0
        self._animations_enabled = True
        self._sweep_duration = max(240, int(sweep_duration))
        self._sweep_position = -0.18

        self._sweep = QVariantAnimation(self)
        self._sweep.setStartValue(-0.18)
        self._sweep.setEndValue(1.18)
        self._sweep.setDuration(self._sweep_duration)
        self._sweep.setLoopCount(-1)
        self._sweep.setEasingCurve(QEasingCurve.Type.Linear)
        self._sweep.valueChanged.connect(self._set_sweep_position)

        self.setMinimumSize(280, 58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Etapas da transferência")
        self._update_accessibility()

    @property
    def stages(self) -> tuple[str, str, str, str]:
        return self._stages  # type: ignore[return-value]

    @property
    def active_stage(self) -> int | None:
        return self._active_stage

    @property
    def completed_stages(self) -> int:
        return self._completed_stages

    def animations_enabled(self) -> bool:
        return self._animations_enabled

    def set_animations_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._animations_enabled == enabled:
            return
        self._animations_enabled = enabled
        self._sync_sweep()

    def set_stage(self, index: int, *, completed: int | None = None) -> None:
        index = self._validate_stage(index)
        completed_count = index if completed is None else self._validate_completed(completed)

        active_changed = self._active_stage != index
        completed_changed = self._completed_stages != completed_count
        self._active_stage = index
        self._completed_stages = completed_count
        if completed_changed:
            self.completedStagesChanged.emit(completed_count)
        if active_changed:
            self.activeStageChanged.emit(index)
        self._state_changed()

    def set_active_stage(self, index: int | None) -> None:
        value = None if index is None else self._validate_stage(index)
        if self._active_stage == value:
            return
        self._active_stage = value
        self.activeStageChanged.emit(-1 if value is None else value)
        self._state_changed()

    def set_completed_stages(self, count: int) -> None:
        value = self._validate_completed(count)
        if self._completed_stages == value:
            return
        self._completed_stages = value
        self.completedStagesChanged.emit(value)
        self._state_changed()

    def reset(self) -> None:
        self.set_completed_stages(0)
        self.set_active_stage(0)

    def complete(self) -> None:
        self.set_completed_stages(self.STAGE_COUNT)
        self.set_active_stage(None)

    def is_sweep_running(self) -> bool:
        return self._sweep.state() == QAbstractAnimation.State.Running

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(520, 62)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt virtual method
        return QSize(280, 58)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().showEvent(event)
        self._sync_sweep()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        self._sweep.stop()
        super().hideEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.EnabledChange):
            self._sync_sweep()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        metrics = painter.fontMetrics()
        label_half_width = max(metrics.horizontalAdvance(label) for label in self._stages) / 2.0
        side_margin = max(18.0, min(label_half_width, self.width() * 0.16))
        start_x = side_margin
        end_x = max(start_x + 1.0, self.width() - side_margin)
        rail_y = 17.0
        step = (end_x - start_x) / (self.STAGE_COUNT - 1)
        nodes = [QPointF(start_x + step * index, rail_y) for index in range(self.STAGE_COUNT)]

        base_pen = QPen(QColor(_SLATE_TRACK), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(base_pen)
        painter.drawLine(nodes[0], nodes[-1])

        progress_end = self._progress_end_index()
        if progress_end > 0:
            progress_pen = QPen(
                QColor(_SIGNAL_BLUE),
                3.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
            painter.setPen(progress_pen)
            painter.drawLine(nodes[0], nodes[progress_end])

        self._paint_sweep(painter, nodes[0], nodes[-1])

        for index, point in enumerate(nodes):
            completed = index < self._completed_stages
            active = index == self._active_stage
            if active:
                painter.setPen(QPen(QColor(_SIGNAL_CYAN), 1.4))
                painter.setBrush(QColor(67, 211, 243, 32))
                painter.drawEllipse(point, 8.2, 8.2)
            painter.setPen(QPen(QColor(_SLATE_EDGE), 1.2))
            painter.setBrush(QColor(_SUCCESS_CYAN if completed else (_SIGNAL_CYAN if active else _MIDNIGHT_INSET)))
            painter.drawEllipse(point, 5.5, 5.5)
            if completed:
                check = QPainterPath()
                check.moveTo(point.x() - 2.7, point.y())
                check.lineTo(point.x() - 0.7, point.y() + 2.1)
                check.lineTo(point.x() + 3.0, point.y() - 2.2)
                painter.setPen(
                    QPen(QColor(_CHECK_INK), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                )
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(check)

        label_top = rail_y + 13.0
        label_width = max(1.0, step - 8.0)
        normal_font = self.font()
        active_font = QFont(normal_font)
        active_font.setWeight(QFont.Weight.DemiBold)
        for index, (label, point) in enumerate(zip(self._stages, nodes, strict=True)):
            active = index == self._active_stage
            painter.setFont(active_font if active else normal_font)
            painter.setPen(QColor(_TEXT_PRIMARY if active else _TEXT_SECONDARY))
            elided = painter.fontMetrics().elidedText(
                label,
                Qt.TextElideMode.ElideRight,
                int(label_width),
            )
            label_rect = QRectF(point.x() - label_width / 2.0, label_top, label_width, metrics.height() + 3)
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)

    def _paint_sweep(self, painter: QPainter, start: QPointF, end: QPointF) -> None:
        if not self.is_sweep_running() or not 0.0 <= self._sweep_position <= 1.0:
            return
        center = self._sweep_position
        width = 0.12
        low = max(0.0, center - width)
        high = min(1.0, center + width)
        gradient = QLinearGradient(start, end)
        gradient.setColorAt(0.0, QColor(67, 211, 243, 0))
        if low > 0.0:
            gradient.setColorAt(low, QColor(67, 211, 243, 0))
        gradient.setColorAt(center, QColor(67, 211, 243, 96))
        if high < 1.0:
            gradient.setColorAt(high, QColor(67, 211, 243, 0))
        gradient.setColorAt(1.0, QColor(67, 211, 243, 0))
        painter.setPen(QPen(gradient, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, end)

    def _set_sweep_position(self, value) -> None:
        self._sweep_position = float(value)
        self.update()

    def _sync_sweep(self) -> None:
        should_run = (
            self._animations_enabled
            and self.isVisible()
            and self.isEnabled()
            and self._active_stage is not None
            and _style_allows_animation(self)
        )
        if should_run:
            if not self.is_sweep_running():
                self._sweep.setDuration(self._sweep_duration)
                self._sweep.start()
        else:
            self._sweep.stop()
            self._sweep_position = -0.18
            self.update()

    def _progress_end_index(self) -> int:
        if self._completed_stages >= self.STAGE_COUNT:
            return self.STAGE_COUNT - 1
        if self._active_stage is not None:
            return self._active_stage
        return max(0, self._completed_stages - 1)

    def _state_changed(self) -> None:
        self._update_accessibility()
        self._sync_sweep()
        self.update()

    def _update_accessibility(self) -> None:
        completed = self._completed_stages
        if self._active_stage is None:
            state = "Transferência concluída" if completed == self.STAGE_COUNT else "Transferência inativa"
        else:
            state = (
                f"Etapa ativa {self._active_stage + 1} de {self.STAGE_COUNT}: "
                f"{self._stages[self._active_stage]}"
            )
        suffix = "etapa concluída" if completed == 1 else "etapas concluídas"
        self.setAccessibleDescription(f"{state}. {completed} {suffix}.")

    @classmethod
    def _validate_stage(cls, index: int) -> int:
        value = int(index)
        if not 0 <= value < cls.STAGE_COUNT:
            raise ValueError(f"stage index must be between 0 and {cls.STAGE_COUNT - 1}")
        return value

    @classmethod
    def _validate_completed(cls, count: int) -> int:
        value = int(count)
        if not 0 <= value <= cls.STAGE_COUNT:
            raise ValueError(f"completed stage count must be between 0 and {cls.STAGE_COUNT}")
        return value


class AnimatedProgressBar(QProgressBar):
    """A progress bar that eases monotonically toward each new value."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        animation_duration: int = 220,
    ) -> None:
        super().__init__(parent)
        self._animations_enabled = True
        self._animation_duration = max(0, int(animation_duration))
        self._display_value = float(QProgressBar.value(self))
        self._target_value = QProgressBar.value(self)

        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self._animation_duration)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._apply_animated_value)
        self._animation.finished.connect(self._settle_target)

        self.setAccessibleName("Progresso da transferência")
        self.setStyleSheet(
            """
            AnimatedProgressBar {
                background: #111D2B;
                color: #EAF7FC;
                border: 1px solid #294158;
                border-radius: 5px;
                min-height: 10px;
                text-align: center;
            }
            AnimatedProgressBar::chunk {
                background: #43D3F3;
                border-radius: 4px;
            }
            """
        )

    def animations_enabled(self) -> bool:
        return self._animations_enabled

    def set_animations_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._animations_enabled == enabled:
            return
        self._animations_enabled = enabled
        if not enabled and self.is_animating():
            self._animation.stop()
            self._settle_target()

    def animation_duration(self) -> int:
        return self._animation_duration

    def set_animation_duration(self, duration: int) -> None:
        self._animation_duration = max(0, int(duration))
        self._animation.setDuration(self._animation_duration)
        if self._animation_duration == 0 and self.is_animating():
            self._animation.stop()
            self._settle_target()

    def target_value(self) -> int:
        return self._target_value

    def is_animating(self) -> bool:
        return self._animation.state() == QAbstractAnimation.State.Running

    def setValue(self, value: int, *, animate: bool = True) -> None:  # noqa: N802
        target = max(self.minimum(), min(self.maximum(), int(value)))
        self._target_value = target
        self._update_accessible_progress(target)

        current = self._display_value
        if current < self.minimum() or current > self.maximum():
            current = float(max(self.minimum(), min(self.maximum(), QProgressBar.value(self))))
            if current < self.minimum():
                current = float(self.minimum())
                QProgressBar.setValue(self, self.minimum())
            self._display_value = current

        should_animate = (
            animate
            and self._animations_enabled
            and self._animation_duration > 0
            and self.isVisible()
            and self.minimum() != self.maximum()
            and _style_allows_animation(self)
            and round(current) != target
        )
        self._animation.stop()
        if not should_animate:
            self._display_value = float(target)
            QProgressBar.setValue(self, target)
            return

        self._animation.setStartValue(current)
        self._animation.setEndValue(float(target))
        self._animation.setDuration(self._animation_duration)
        self._animation.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        if self.is_animating():
            self._animation.stop()
            self._settle_target()
        super().hideEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.StyleChange
            and self.is_animating()
            and not _style_allows_animation(self)
        ):
            self._animation.stop()
            self._settle_target()

    def _apply_animated_value(self, value) -> None:
        # OutCubic is monotonic; rounding to the nearest integral progress unit
        # keeps QProgressBar's native API while never overshooting the target.
        numeric = float(value)
        low = min(self._display_value, float(self._target_value))
        high = max(self._display_value, float(self._target_value))
        self._display_value = max(low, min(high, numeric))
        QProgressBar.setValue(self, int(round(self._display_value)))

    def _settle_target(self) -> None:
        self._display_value = float(self._target_value)
        QProgressBar.setValue(self, self._target_value)

    def _update_accessible_progress(self, target: int) -> None:
        span = self.maximum() - self.minimum()
        if span <= 0:
            description = "Progresso em andamento"
        else:
            percent = round((target - self.minimum()) * 100 / span)
            description = f"Progresso esperado: {percent}%"
        self.setAccessibleDescription(description)
