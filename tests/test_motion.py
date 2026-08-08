from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLabel, QProxyStyle, QStyle

from baixatube.motion import AnimatedProgressBar, AnimatedStackedWidget, TransferRail


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _show(widget, app: QApplication) -> None:
    widget.show()
    app.processEvents()


def test_stacked_widget_fades_and_coalesces_reentrant_navigation(app: QApplication) -> None:
    stack = AnimatedStackedWidget(transition_duration=45)
    for text in ("Baixar", "Fila", "Histórico"):
        stack.addWidget(QLabel(text))
    stack.resize(360, 180)
    _show(stack, app)

    started = QSignalSpy(stack.transitionStarted)
    finished = QSignalSpy(stack.transitionFinished)
    stack.setCurrentIndex(1)
    stack.setCurrentIndex(2)  # Coalesced while the first fade is active.

    assert stack.is_transitioning()
    assert stack.currentIndex() == 1
    # Two 45 ms fades plus a little headroom for coarse Windows/offscreen
    # timer scheduling on busy CI workers.
    QTest.qWait(220)

    assert not stack.is_transitioning()
    assert stack.currentIndex() == 2
    assert started.count() == 2
    assert finished.count() == 2
    stack.close()


def test_stacked_widget_latest_request_can_cancel_a_queued_page(app: QApplication) -> None:
    stack = AnimatedStackedWidget(transition_duration=45)
    for text in ("Origem", "Destino", "Ignorada"):
        stack.addWidget(QLabel(text))
    _show(stack, app)

    stack.setCurrentIndex(1)
    stack.setCurrentIndex(2)
    stack.setCurrentIndex(1)  # Latest intent is to remain on the active destination.
    QTest.qWait(120)

    assert stack.currentIndex() == 1
    assert not stack.is_transitioning()
    stack.close()


def test_stacked_widget_local_motion_switch_is_immediate(app: QApplication) -> None:
    stack = AnimatedStackedWidget(transition_duration=80)
    stack.addWidget(QLabel("Origem"))
    stack.addWidget(QLabel("Destino"))
    _show(stack, app)

    stack.set_animations_enabled(False)
    stack.setCurrentIndex(1)

    assert stack.currentIndex() == 1
    assert not stack.is_transitioning()
    stack.close()


def test_transfer_rail_exposes_four_stages_and_accessible_state(app: QApplication) -> None:
    rail = TransferRail(sweep_duration=300)
    rail.resize(520, 62)
    _show(rail, app)

    rail.set_stage(2)
    assert rail.stages == ("Link", "Preparação", "Download", "Finalização")
    assert rail.active_stage == 2
    assert rail.completed_stages == 2
    assert "Etapa ativa 3 de 4: Download" in rail.accessibleDescription()
    assert "2 etapas concluídas" in rail.accessibleDescription()
    assert rail.is_sweep_running()
    assert not rail.grab().isNull()

    rail.complete()
    assert rail.active_stage is None
    assert rail.completed_stages == 4
    assert "Transferência concluída" in rail.accessibleDescription()
    assert not rail.is_sweep_running()
    rail.close()


def test_transfer_rail_validates_stage_contract(app: QApplication) -> None:
    with pytest.raises(ValueError, match="exactly four"):
        TransferRail(stages=("um", "dois", "três"))

    rail = TransferRail()
    with pytest.raises(ValueError, match="stage index"):
        rail.set_stage(4)
    with pytest.raises(ValueError, match="completed stage count"):
        rail.set_completed_stages(5)
    rail.close()


class _NoMotionStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, return_data=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_Widget_Animate:
            return 0
        return super().styleHint(hint, option, widget, return_data)


def test_transfer_rail_respects_qt_widget_animate_style_hint(app: QApplication) -> None:
    rail = TransferRail()
    no_motion_style = _NoMotionStyle("Fusion")
    rail.setStyle(no_motion_style)
    rail.resize(520, 62)
    _show(rail, app)

    assert not rail.is_sweep_running()
    rail.close()


def test_progress_bar_interpolates_monotonically_without_bounce(app: QApplication) -> None:
    bar = AnimatedProgressBar(animation_duration=90)
    bar.setRange(0, 100)
    bar.setValue(0, animate=False)
    bar.resize(320, 20)
    _show(bar, app)

    observed: list[int] = []
    bar.valueChanged.connect(observed.append)
    bar.setValue(80)
    assert bar.is_animating()
    QTest.qWait(140)

    assert bar.value() == 80
    assert bar.target_value() == 80
    assert observed
    assert all(0 <= value <= 80 for value in observed)
    assert observed == sorted(observed)
    assert not bar.is_animating()
    bar.close()


def test_progress_bar_retargets_from_visible_value_and_can_disable_motion(app: QApplication) -> None:
    bar = AnimatedProgressBar(animation_duration=120)
    bar.setRange(0, 100)
    bar.setValue(0, animate=False)
    _show(bar, app)

    bar.setValue(100)
    QTest.qWait(35)
    visible_value = bar.value()
    bar.setValue(60)
    assert bar.value() >= visible_value - 1
    QTest.qWait(170)
    assert bar.value() == 60

    bar.setValue(90)
    bar.set_animations_enabled(False)
    assert bar.value() == 90
    assert not bar.is_animating()
    assert "90%" in bar.accessibleDescription()
    bar.close()
