from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from baixatube.icons import ICON_NAMES, icon


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("name", sorted(ICON_NAMES))
def test_every_named_icon_renders_visible_pixels(name: str):
    result = icon(name)

    assert not result.isNull()
    pixmap = result.pixmap(QSize(24, 24))
    assert not pixmap.isNull()
    image = pixmap.toImage()
    assert any(
        image.pixelColor(x, y).alpha() > 0
        for y in range(image.height())
        for x in range(image.width())
    )


def test_icon_factory_reuses_cached_artwork_without_exposing_it():
    first = icon("download", "#AFC5E8", 20)
    second = icon("download", "#afc5e8", 20)

    assert first is not second
    assert first.cacheKey() == second.cacheKey()


def test_brand_slash_play_is_an_alias_for_brand():
    assert icon("brand/play").cacheKey() == icon("brand").cacheKey()


def test_icon_supports_high_dpi_pixmaps():
    pixmap = icon("settings", size=20).pixmap(QSize(20, 20), 2.0)

    assert not pixmap.isNull()
    assert pixmap.devicePixelRatio() == pytest.approx(2.0)
    assert pixmap.width() >= 40
    assert pixmap.height() >= 40


def test_unknown_icon_name_is_rejected():
    with pytest.raises(ValueError, match="unknown icon name"):
        icon("does-not-exist")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"color": "not-a-colour"}, "invalid icon color"),
        ({"size": 0}, "icon size must be a positive integer"),
    ],
)
def test_invalid_icon_options_are_rejected(kwargs, message: str):
    with pytest.raises(ValueError, match=message):
        icon("play", **kwargs)
