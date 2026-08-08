from __future__ import annotations

import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QProcess, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .app_updates import AppUpdateCheck, AppUpdateManager, AppUpdateResult, AppUpdater
from .branding import (
    APP_NAME,
    APP_TAGLINE,
    CREATOR_AVATAR,
    CREATOR_NAME,
    CREATOR_ROLE,
    DISCORD_HANDLE,
    INSTAGRAM_HANDLE,
    INSTAGRAM_URL,
)
from .compatibility import CompatibilityLevel, CompatibilityReport, set_remote_ejs_enabled
from .cookie_auth import CookieConfig, SUPPORTED_BROWSERS
from .icons import icon as ui_icon
from .models import DownloadProgress, DownloadRequest, DownloadStatus, HistoryEntry, MediaInfo
from .motion import AnimatedProgressBar, AnimatedStackedWidget, TransferRail
from .network_policy import scheduled_rate_limit
from .paths import active_binary, resource_dir
from .plugin_security import PoTokenPluginController, PoTokenPluginManager, PluginState
from .library_cleanup import CleanupCandidate, remove_candidates, scan_partial_files
from .service import Analyzer, DownloadManager
from .storage import Storage
from .utils import estimate_output_bytes, format_bytes, format_duration, is_supported_url, preview_output_name
from .windows_toast import toast_command


# Visual checkpoint — BraXYTDow Studio 2.3
# Intent: a precise midnight media studio that feels calm, fast and dependable.
# Palette: graphite canvas, cobalt action, cyan transfer, mint success, amber warning.
# Depth: borders and tonal surface shifts only; no decorative gradients or glass.
# Surfaces: one integrated shell, compact work panels and a dominant queue canvas.
# Typography: Segoe UI Variable with a restrained 11/13/18/28 px hierarchy.
# Spacing: 4 px base rhythm, 12–20 px component gaps, 24–30 px page gutters.
THEME = {
    "canvas": "#070C12",
    "shell": "#090F17",
    "surface": "#0C131D",
    "surface_raised": "#101925",
    "surface_hover": "#152131",
    "input": "#090F17",
    "border": "#263447",
    "border_strong": "#3B4C64",
    "text": "#F4F7FB",
    "text_soft": "#C5CEDA",
    "muted": "#909DB0",
    "faint": "#647287",
    "accent": "#1976F3",
    "accent_hover": "#2B84FA",
    "accent_pressed": "#1164D3",
    "cyan": "#32D6E3",
    "gold": "#D7B56D",
    "gold_soft": "#F1D99A",
    "gold_surface": "#211C13",
    "gold_border": "#67562F",
    "success": "#3CCB74",
    "warning": "#F2B84B",
    "danger": "#F16F79",
}

STATUS_LABELS = {
    DownloadStatus.WAITING: "Aguardando",
    DownloadStatus.ANALYZING: "Analisando",
    DownloadStatus.DOWNLOADING: "Baixando",
    DownloadStatus.CONVERTING: "Convertendo",
    DownloadStatus.COMPLETED: "Concluído",
    DownloadStatus.CANCELLED: "Cancelado",
    DownloadStatus.ERROR: "Erro",
}

STATUS_COLORS = {
    DownloadStatus.WAITING: QColor(THEME["muted"]),
    DownloadStatus.ANALYZING: QColor(THEME["cyan"]),
    DownloadStatus.DOWNLOADING: QColor("#66A8FF"),
    DownloadStatus.CONVERTING: QColor("#BAA3FF"),
    DownloadStatus.COMPLETED: QColor(THEME["success"]),
    DownloadStatus.CANCELLED: QColor(THEME["muted"]),
    DownloadStatus.ERROR: QColor(THEME["danger"]),
}

STATUS_ICONS = {
    DownloadStatus.WAITING: "queue",
    DownloadStatus.ANALYZING: "search",
    DownloadStatus.DOWNLOADING: "download",
    DownloadStatus.CONVERTING: "tools",
    DownloadStatus.COMPLETED: "check",
    DownloadStatus.CANCELLED: "cancel",
    DownloadStatus.ERROR: "warning",
}

STATUS_ALIASES = {
    "waiting": DownloadStatus.WAITING,
    "analyzing": DownloadStatus.ANALYZING,
    "downloading": DownloadStatus.DOWNLOADING,
    "converting": DownloadStatus.CONVERTING,
    "completed": DownloadStatus.COMPLETED,
    "cancelled": DownloadStatus.CANCELLED,
    "canceled": DownloadStatus.CANCELLED,
    "error": DownloadStatus.ERROR,
}


def _status_from_value(value: str) -> DownloadStatus | None:
    try:
        return DownloadStatus(value)
    except ValueError:
        return STATUS_ALIASES.get(value.casefold())

PAGE_META = (
    ("Baixar", "Analise um link e configure o resultado antes de adicionar à fila."),
    ("Fila de downloads", "Acompanhe, pause, retome e organize suas transferências."),
    ("Biblioteca", "Pesquise downloads, confira arquivos movidos e sincronize playlists sem repetir itens."),
    ("Ferramentas e atualizações", "Mantenha os mecanismos de download compatíveis com o YouTube."),
)


def _button(
    text: str,
    role: str = "secondary",
    icon_name: str | None = None,
    icon_color: str | None = None,
) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("role", role)
    button.setCursor(Qt.PointingHandCursor)
    if icon_name:
        color = icon_color or ("#FFFFFF" if role == "primary" else THEME["text_soft"])
        button.setIcon(ui_icon(icon_name, color, 18))
        button.setIconSize(QSize(18, 18))
    return button


def _add_line_icon(field: QLineEdit, icon_name: str) -> QAction:
    action = field.addAction(ui_icon(icon_name, THEME["muted"], 18), QLineEdit.LeadingPosition)
    action.setEnabled(False)
    return action


def _transfer_divider(completed: int = 1, segments: int = 4) -> QWidget:
    rail = QWidget()
    rail.setFixedHeight(8)
    layout = QHBoxLayout(rail)
    layout.setContentsMargins(2, 3, 2, 3)
    layout.setSpacing(5)
    for index in range(segments):
        segment = QFrame()
        segment.setObjectName("railSegment")
        segment.setProperty("active", index < completed)
        layout.addWidget(segment, 2 if index == 0 else 1)
    return rail


def _status_metric(icon_name: str, text: str) -> tuple[QWidget, QLabel]:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(2, 0, 2, 0)
    layout.setSpacing(6)
    mark = QLabel()
    mark.setPixmap(ui_icon(icon_name, THEME["muted"], 17).pixmap(17, 17))
    value = QLabel(text)
    value.setObjectName("metricValue")
    layout.addWidget(mark)
    layout.addWidget(value)
    return widget, value


def _set_badge(label: QLabel, text: str, state: str) -> None:
    label.setText(text)
    label.setProperty("state", state)
    label.style().unpolish(label)
    label.style().polish(label)


def _health_label(level: CompatibilityLevel) -> str:
    return {
        CompatibilityLevel.HEALTHY: "Operacional",
        CompatibilityLevel.DEGRADED: "Inconclusivo",
        CompatibilityLevel.FAILED: "Falhou",
    }[level]


def _section_header(title: str, subtitle: str = "") -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    heading = QLabel(title)
    heading.setObjectName("sectionTitle")
    layout.addWidget(heading)
    if subtitle:
        description = QLabel(subtitle)
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(description)
    return widget


def _circular_avatar(path: Path, diameter: int = 104) -> QPixmap:
    """Load and center-crop an avatar into a true antialiased circle."""

    source = QPixmap(str(path))
    if source.isNull():
        return QPixmap()
    scaled = source.scaled(diameter, diameter, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    offset_x = max(0, (scaled.width() - diameter) // 2)
    offset_y = max(0, (scaled.height() - diameter) // 2)
    target = QPixmap(diameter, diameter)
    target.fill(Qt.transparent)
    painter = QPainter(target)
    painter.setRenderHint(QPainter.Antialiasing, True)
    clip = QPainterPath()
    clip.addEllipse(2, 2, diameter - 4, diameter - 4)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, scaled, offset_x, offset_y, diameter, diameter)
    painter.setClipping(False)
    painter.setPen(QPen(QColor(THEME["accent"]), 3))
    painter.drawEllipse(2, 2, diameter - 4, diameter - 4)
    painter.end()
    return target


def _rounded_cover(source: QPixmap, size: QSize, radius: int = 8) -> QPixmap:
    """Center-crop a thumbnail and clip the pixels to the visible card radius."""

    scaled = source.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    offset_x = max(0, (scaled.width() - size.width()) // 2)
    offset_y = max(0, (scaled.height() - size.height()) // 2)
    target = QPixmap(size)
    target.fill(Qt.transparent)
    painter = QPainter(target)
    painter.setRenderHint(QPainter.Antialiasing, True)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, size.width(), size.height(), radius, radius)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, scaled, offset_x, offset_y, size.width(), size.height())
    painter.end()
    return target


def _creator_portrait(source: QPixmap, size: QSize) -> QPixmap:
    """Create the creator hero crop with the visor-derived signature border."""

    target = _rounded_cover(source, size, 15)
    if target.isNull():
        return target
    painter = QPainter(target)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor(THEME["gold"]), 2))
    painter.drawRoundedRect(1, 1, size.width() - 2, size.height() - 2, 15, 15)
    painter.end()
    return target


class EmptyState(QFrame):
    def __init__(
        self,
        icon_name: str,
        title: str,
        message: str,
        parent: QWidget | None = None,
        *,
        action_text: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 40, 28, 40)
        layout.setAlignment(Qt.AlignCenter)
        symbol_label = QLabel()
        symbol_label.setObjectName("emptySymbol")
        symbol_label.setAlignment(Qt.AlignCenter)
        symbol_label.setPixmap(ui_icon(icon_name, THEME["accent"], 34).pixmap(34, 34))
        self.title_label = QLabel(title)
        self.title_label.setObjectName("emptyTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.message_label = QLabel(message)
        self.message_label.setObjectName("muted")
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setWordWrap(True)
        layout.addWidget(symbol_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.message_label)
        self.action_button: QPushButton | None = None
        if action_text:
            self.action_button = _button(action_text, "primary", "download")
            self.action_button.setMaximumWidth(190)
            layout.addSpacing(8)
            layout.addWidget(self.action_button, 0, Qt.AlignHCenter)

    def set_message(self, title: str, message: str) -> None:
        self.title_label.setText(title)
        self.message_label.setText(message)


class QueueTableWidget(QTableWidget):
    order_changed = Signal(object)

    def __init__(self, rows: int, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(rows, columns, parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event: Any) -> None:
        super().dropEvent(event)
        order = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item and item.data(Qt.UserRole):
                order.append(str(item.data(Qt.UserRole)))
        if order:
            self.order_changed.emit(order)


class PlaylistDialog(QDialog):
    def __init__(self, media: MediaInfo, archived_ids: set[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Selecionar itens da playlist")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)
        self.archived_ids = archived_ids or set()
        layout.addWidget(_section_header(media.title, f"{len(media.entries)} itens encontrados. Itens já arquivados ficam desmarcados, mas podem ser selecionados novamente."))

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Pesquisar na playlist…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Pesquisar itens da playlist")
        all_button = _button("Selecionar todos", icon_name="check")
        none_button = _button("Limpar seleção", "ghost", "cancel")
        new_button = _button("Somente novos", "ghost", "download")
        search_row.addWidget(self.search, 1)
        search_row.addWidget(all_button)
        search_row.addWidget(none_button)
        search_row.addWidget(new_button)
        layout.addLayout(search_row)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for index, entry in enumerate(media.entries, 1):
            duration = format_duration(entry.duration)
            item = QListWidgetItem(f"{index:02d}  {entry.title}   ·   {duration}")
            item.setData(Qt.UserRole, str(entry.playlist_index or index))
            item.setData(Qt.UserRole + 1, entry.title.casefold())
            item.setData(Qt.UserRole + 2, entry.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if entry.id in self.archived_ids else Qt.Checked)
            if entry.id in self.archived_ids:
                item.setText(item.text() + "   ·   já baixado")
                item.setForeground(QColor(THEME["muted"]))
            self.list.addItem(item)
        layout.addWidget(self.list, 1)

        footer = QHBoxLayout()
        self.selection_label = QLabel()
        self.selection_label.setObjectName("muted")
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.button(QDialogButtonBox.Ok).setText("Adicionar selecionados")
        buttons.button(QDialogButtonBox.Ok).setProperty("role", "primary")
        buttons.button(QDialogButtonBox.Ok).setIcon(ui_icon("download", "#FFFFFF", 18))
        footer.addWidget(self.selection_label)
        footer.addStretch()
        footer.addWidget(buttons)
        layout.addLayout(footer)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        all_button.clicked.connect(lambda: self._check_visible(Qt.Checked))
        none_button.clicked.connect(lambda: self._check_visible(Qt.Unchecked))
        new_button.clicked.connect(self._select_new)
        self.search.textChanged.connect(self._filter)
        self.list.itemChanged.connect(self._update_count)
        self._update_count()

    def _check_visible(self, state: Qt.CheckState) -> None:
        self.list.blockSignals(True)
        for index in range(self.list.count()):
            item = self.list.item(index)
            if not item.isHidden():
                item.setCheckState(state)
        self.list.blockSignals(False)
        self._update_count()

    def _filter(self, text: str) -> None:
        query = text.strip().casefold()
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(bool(query and query not in item.data(Qt.UserRole + 1)))

    def _select_new(self) -> None:
        self.list.blockSignals(True)
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setCheckState(Qt.Unchecked if item.data(Qt.UserRole + 2) in self.archived_ids else Qt.Checked)
        self.list.blockSignals(False)
        self._update_count()

    def _update_count(self, *_args: Any) -> None:
        selected = sum(self.list.item(i).checkState() == Qt.Checked for i in range(self.list.count()))
        self.selection_label.setText(f"{selected} de {self.list.count()} selecionados")

    def selected(self) -> list[str]:
        return [
            self.list.item(index).data(Qt.UserRole)
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.Checked
        ]

    def selected_media_ids(self) -> list[str]:
        return [
            str(self.list.item(index).data(Qt.UserRole + 2) or "")
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.Checked
        ]


class ChapterDialog(QDialog):
    def __init__(self, media: MediaInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Prévia dos capítulos")
        self.resize(660, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)
        layout.addWidget(
            _section_header(
                "Dividir por capítulos",
                f"{len(media.chapters)} arquivos serão criados. Confira os nomes e intervalos antes de continuar.",
            )
        )
        self.chapter_list = QListWidget()
        self.chapter_list.setAccessibleName("Selecionar capítulos")
        for index, chapter in enumerate(media.chapters, 1):
            start = format_duration(round(chapter.start_time))
            end = format_duration(round(chapter.end_time)) if chapter.end_time is not None else "fim"
            item = QListWidgetItem(f"{index:02d}  {chapter.title}   ·   {start} — {end}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, {"title": chapter.title, "start": str(chapter.start_time), "end": str(chapter.end_time or "")})
            self.chapter_list.addItem(item)
        layout.addWidget(self.chapter_list, 1)
        notice = QLabel("Os capítulos serão separados pelo FFmpeg após o download. O arquivo principal não será apresentado como concluído até o processamento terminar.")
        notice.setObjectName("inlineNotice")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Ok)
        buttons.button(QDialogButtonBox.Cancel).setText("Voltar")
        buttons.button(QDialogButtonBox.Ok).setText("Confirmar divisão")
        buttons.button(QDialogButtonBox.Ok).setProperty("role", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self) -> list[dict[str, Any]]:
        return [
            self.chapter_list.item(index).data(Qt.UserRole)
            for index in range(self.chapter_list.count())
            if self.chapter_list.item(index).checkState() == Qt.Checked
        ]


class MetadataEditorDialog(QDialog):
    def __init__(self, media: MediaInfo, values: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        values = values or {}
        self.setWindowTitle("Nome, metadados e capa")
        self.resize(650, 470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        layout.addWidget(_section_header("Identidade do arquivo", "Personalize antes do download. O FFmpeg só publica o arquivo depois de aplicar as alterações."))
        form = QFormLayout()
        form.setSpacing(12)
        self.filename = QLineEdit(str(values.get("custom_filename", "")))
        self.filename.setPlaceholderText("Vazio para usar o modelo automático")
        self.title = QLineEdit(str(values.get("custom_title", media.title)))
        self.artist = QLineEdit(str(values.get("custom_artist", media.uploader)))
        self.album = QLineEdit(str(values.get("custom_album", "")))
        cover_row = QWidget()
        cover_layout = QHBoxLayout(cover_row)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        self.cover = QLineEdit(str(values.get("custom_cover", "")))
        self.cover.setPlaceholderText("Usar miniatura original")
        cover_button = _button("Escolher imagem…", icon_name="folder")
        cover_layout.addWidget(self.cover, 1)
        cover_layout.addWidget(cover_button)
        form.addRow("Nome do arquivo", self.filename)
        form.addRow("Título interno", self.title)
        form.addRow("Artista / canal", self.artist)
        form.addRow("Álbum / coleção", self.album)
        form.addRow("Capa personalizada", cover_row)
        layout.addLayout(form)
        note = QLabel("Capas personalizadas são incorporadas em MP3, M4A e MP4. Em outros formatos, título, artista e álbum continuam sendo aplicados.")
        note.setObjectName("inlineNotice")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.button(QDialogButtonBox.Save).setText("Aplicar")
        buttons.button(QDialogButtonBox.Save).setProperty("role", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        cover_button.clicked.connect(self._browse_cover)
        layout.addWidget(buttons)

    def _browse_cover(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(self, "Escolher capa", self.cover.text(), "Imagens (*.jpg *.jpeg *.png *.webp)")
        if selected:
            self.cover.setText(selected)

    def values(self) -> dict[str, str]:
        return {
            "custom_filename": self.filename.text().strip(),
            "custom_title": self.title.text().strip(),
            "custom_artist": self.artist.text().strip(),
            "custom_album": self.album.text().strip(),
            "custom_cover": self.cover.text().strip(),
        }


class BatchEditDialog(QDialog):
    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Editar itens da fila")
        self.resize(580, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        layout.addWidget(_section_header(f"Editar {count} itens", "Somente campos ativados serão alterados. Downloads em andamento são preservados."))
        form = QFormLayout()
        self.destination = QLineEdit()
        choose = _button("Escolher…", icon_name="folder")
        destination_row = QWidget()
        destination_layout = QHBoxLayout(destination_row)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        destination_layout.addWidget(self.destination, 1)
        destination_layout.addWidget(choose)
        self.quality = QComboBox()
        for label, data in (("Não alterar", None), ("Automática", "auto"), ("2160p", "2160"), ("1440p", "1440"), ("1080p", "1080"), ("720p", "720"), ("480p", "480")):
            self.quality.addItem(label, data)
        self.priority = QComboBox()
        for label, data in (("Não alterar", None), ("Alta", 2), ("Normal", 1), ("Baixa", 0)):
            self.priority.addItem(label, data)
        self.schedule_enabled = QCheckBox("Agendar início")
        self.schedule = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.schedule.setCalendarPopup(True)
        self.schedule.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.schedule.setEnabled(False)
        self.metered = QComboBox()
        self.metered.addItem("Não alterar", None)
        self.metered.addItem("Pausar em conexão limitada", True)
        self.metered.addItem("Permitir conexão limitada", False)
        self.rate_limit = QLineEdit()
        self.rate_limit.setPlaceholderText("Não alterar · exemplo 5M")
        form.addRow("Nova pasta", destination_row)
        form.addRow("Qualidade", self.quality)
        form.addRow("Prioridade", self.priority)
        form.addRow(self.schedule_enabled, self.schedule)
        form.addRow("Política de rede", self.metered)
        form.addRow("Limite de velocidade", self.rate_limit)
        layout.addLayout(form)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.button(QDialogButtonBox.Save).setText("Aplicar em lote")
        buttons.button(QDialogButtonBox.Save).setProperty("role", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.schedule_enabled.toggled.connect(self.schedule.setEnabled)
        choose.clicked.connect(self._browse)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Escolher pasta", self.destination.text())
        if selected:
            self.destination.setText(selected)

    def changes(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.destination.text().strip():
            values["destination"] = self.destination.text().strip()
        if self.quality.currentData() is not None:
            values["quality"] = self.quality.currentData()
        if self.priority.currentData() is not None:
            values["priority"] = self.priority.currentData()
        if self.schedule_enabled.isChecked():
            values["scheduled_at"] = self.schedule.dateTime().toUTC().toString(Qt.ISODate)
        if self.metered.currentData() is not None:
            values["pause_on_metered"] = self.metered.currentData()
        if self.rate_limit.text().strip():
            values["rate_limit"] = self.rate_limit.text().strip()
        return values


class AdvancedOptionsDialog(QDialog):
    """UI for yt-dlp options already represented by DownloadRequest."""

    def __init__(self, values: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Opções avançadas")
        self.resize(650, 610)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 22, 22, 22)
        outer.addWidget(_section_header("Controle fino", "Estas opções ficam salvas para os próximos downloads."))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0, 10, 8, 10)
        form.setSpacing(12)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.audio_quality = QComboBox()
        for label, data in (("Melhor qualidade", "0"), ("Muito alta · 320 kbps", "320K"), ("Alta · 256 kbps", "256K"), ("Equilibrada · 192 kbps", "192K"), ("Compacta · 128 kbps", "128K")):
            self.audio_quality.addItem(label, data)
        index = self.audio_quality.findData(str(values.get("audio_quality", "0")))
        self.audio_quality.setCurrentIndex(max(0, index))

        self.subtitle_languages = QLineEdit(str(values.get("subtitle_languages", "pt.*,en.*")))
        self.subtitle_languages.setPlaceholderText("pt.*,en.*")
        self.subtitle_languages.setToolTip("Padrões de idioma do yt-dlp separados por vírgula")
        self.embed_subtitles = QCheckBox("Incorporar legendas no arquivo quando o formato permitir")
        self.embed_subtitles.setChecked(bool(values.get("embed_subtitles", True)))

        self.filename_template = QLineEdit(str(values.get("filename_template", "%(title).180B [%(id)s].%(ext)s")))
        self.filename_template.setToolTip("Template de saída compatível com yt-dlp")
        self.concurrent_fragments = QSpinBox()
        self.concurrent_fragments.setRange(1, 16)
        self.concurrent_fragments.setValue(int(values.get("concurrent_fragments", 4)))
        self.concurrent_fragments.setSuffix(" fragmentos")
        self.retries = QSpinBox()
        self.retries.setRange(0, 50)
        self.retries.setValue(int(values.get("retries", 10)))
        self.rate_limit = QLineEdit(str(values.get("rate_limit", "")))
        self.rate_limit.setPlaceholderText("Sem limite · exemplo: 5M")

        trim_widget = QWidget()
        trim_layout = QHBoxLayout(trim_widget)
        trim_layout.setContentsMargins(0, 0, 0, 0)
        self.trim_start = QLineEdit(str(values.get("trim_start", "")))
        self.trim_start.setPlaceholderText("Início 00:00:00")
        self.trim_end = QLineEdit(str(values.get("trim_end", "")))
        self.trim_end.setPlaceholderText("Fim 00:00:00")
        trim_layout.addWidget(self.trim_start)
        trim_layout.addWidget(self.trim_end)

        extras = QWidget()
        extras_layout = QGridLayout(extras)
        extras_layout.setContentsMargins(0, 0, 0, 0)
        self.write_description = QCheckBox("Salvar descrição (.description)")
        self.write_info_json = QCheckBox("Salvar informações (.info.json)")
        self.sponsorblock = QCheckBox("Remover segmentos SponsorBlock")
        self.playlist_reverse = QCheckBox("Playlist em ordem inversa")
        self.overwrite = QCheckBox("Sobrescrever arquivos existentes")
        self.write_description.setChecked(bool(values.get("write_description", False)))
        self.write_info_json.setChecked(bool(values.get("write_info_json", False)))
        self.sponsorblock.setChecked(bool(values.get("sponsorblock", False)))
        self.playlist_reverse.setChecked(bool(values.get("playlist_reverse", False)))
        self.overwrite.setChecked(bool(values.get("overwrite", False)))
        extras_layout.addWidget(self.write_description, 0, 0)
        extras_layout.addWidget(self.write_info_json, 0, 1)
        extras_layout.addWidget(self.sponsorblock, 1, 0)
        extras_layout.addWidget(self.playlist_reverse, 1, 1)
        extras_layout.addWidget(self.overwrite, 2, 0, 1, 2)

        form.addRow("Qualidade do áudio", self.audio_quality)
        form.addRow("Idiomas das legendas", self.subtitle_languages)
        form.addRow("", self.embed_subtitles)
        form.addRow("Nome dos arquivos", self.filename_template)
        form.addRow("Partes simultâneas", self.concurrent_fragments)
        form.addRow("Tentativas em falhas", self.retries)
        form.addRow("Limite de velocidade", self.rate_limit)
        form.addRow("Recortar trecho", trim_widget)
        form.addRow("Arquivos e comportamento", extras)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        warning = QLabel("Recortes e remoção de segmentos exigem FFmpeg. SponsorBlock depende da disponibilidade dos dados para o vídeo.")
        warning.setObjectName("inlineNotice")
        warning.setWordWrap(True)
        outer.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.button(QDialogButtonBox.Save).setText("Salvar")
        buttons.button(QDialogButtonBox.Save).setProperty("role", "primary")
        buttons.button(QDialogButtonBox.Save).setIcon(ui_icon("check", "#FFFFFF", 18))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def values(self) -> dict[str, Any]:
        return {
            "audio_quality": self.audio_quality.currentData(),
            "subtitle_languages": self.subtitle_languages.text().strip() or "all",
            "embed_subtitles": self.embed_subtitles.isChecked(),
            "filename_template": self.filename_template.text().strip() or "%(title).180B [%(id)s].%(ext)s",
            "concurrent_fragments": self.concurrent_fragments.value(),
            "retries": self.retries.value(),
            "rate_limit": self.rate_limit.text().strip(),
            "write_description": self.write_description.isChecked(),
            "write_info_json": self.write_info_json.isChecked(),
            "sponsorblock": self.sponsorblock.isChecked(),
            "trim_start": self.trim_start.text().strip(),
            "trim_end": self.trim_end.text().strip(),
            "playlist_reverse": self.playlist_reverse.isChecked(),
            "overwrite": self.overwrite.isChecked(),
        }


class SettingsDialog(QDialog):
    def __init__(self, values: dict[str, Any], parent: QWidget | None = None, *, initial_tab: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Configurações")
        self.resize(720, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)
        layout.addWidget(_section_header("Preferências do aplicativo", "Downloads, rede, compatibilidade, atualizações e acessibilidade em um único lugar."))
        tabs = QTabWidget()
        self.tabs = tabs
        tabs.setAccessibleName("Categorias das configurações")

        general = QWidget()
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(16, 16, 16, 16)
        general_layout.setSpacing(14)
        form = QFormLayout()
        form.setSpacing(12)
        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.destination = QLineEdit(str(values.get("destination", Path.home() / "Downloads")))
        browse = _button("Escolher…", icon_name="folder")
        folder_layout.addWidget(self.destination, 1)
        folder_layout.addWidget(browse)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 4)
        self.concurrency.setValue(int(values.get("concurrency", 2)))
        self.concurrency.setToolTip("Mais downloads simultâneos usam mais rede e processamento")
        form.addRow("Pasta padrão", folder_row)
        form.addRow("Downloads simultâneos", self.concurrency)
        general_layout.addLayout(form)

        behavior = QGroupBox("Ao concluir")
        behavior_layout = QVBoxLayout(behavior)
        self.completion_action = QComboBox()
        self.completion_action.addItem("Somente avisar", "notify")
        self.completion_action.addItem("Abrir a pasta da fila", "open_folder")
        self.completion_action.addItem("Nenhuma ação", "none")
        self.completion_action.addItem("Perguntar antes de suspender o computador", "sleep")
        self.completion_action.addItem("Perguntar antes de desligar o computador", "shutdown")
        saved_action = str(values.get("completion_action", "open_folder" if values.get("open_after") else "notify"))
        action_index = self.completion_action.findData(saved_action)
        self.completion_action.setCurrentIndex(max(0, action_index))
        self.interface_animations = QCheckBox("Usar animações e transições suaves")
        self.interface_animations.setChecked(bool(values.get("interface_animations", True)))
        self.interface_animations.setToolTip("Desative para reduzir movimento na interface")
        self.auto_updates = QCheckBox("Verificar automaticamente atualizações das ferramentas")
        self.auto_updates.setChecked(bool(values.get("auto_tool_updates", values.get("auto_update_tools", True))))
        self.close_to_tray = QCheckBox("Fechar para a bandeja quando houver downloads")
        self.close_to_tray.setChecked(bool(values.get("close_to_tray", True)))
        self.native_notifications = QCheckBox("Exibir notificações nativas")
        self.native_notifications.setChecked(bool(values.get("native_notifications", True)))
        behavior_layout.addWidget(QLabel("Quando toda a fila terminar"))
        behavior_layout.addWidget(self.completion_action)
        behavior_layout.addWidget(self.interface_animations)
        behavior_layout.addWidget(self.auto_updates)
        behavior_layout.addWidget(self.close_to_tray)
        behavior_layout.addWidget(self.native_notifications)
        general_layout.addWidget(behavior)
        general_layout.addStretch()
        tabs.addTab(general, "Geral")

        network_page = QWidget()
        network_layout = QVBoxLayout(network_page)
        network_layout.setContentsMargins(16, 16, 16, 16)
        network_form = QFormLayout()
        self.pause_on_metered = QCheckBox("Não iniciar downloads em conexão limitada")
        self.pause_on_metered.setChecked(bool(values.get("pause_on_metered", False)))
        self.bandwidth_day = QLineEdit(str(values.get("bandwidth_day", "")))
        self.bandwidth_day.setPlaceholderText("Sem limite · exemplo 8M")
        self.bandwidth_night = QLineEdit(str(values.get("bandwidth_night", "")))
        self.bandwidth_night.setPlaceholderText("Sem limite · exemplo 20M")
        self.night_start = QSpinBox()
        self.night_start.setRange(0, 23)
        self.night_start.setValue(int(values.get("bandwidth_night_start", 22)))
        self.night_start.setSuffix(" h")
        self.night_end = QSpinBox()
        self.night_end.setRange(0, 23)
        self.night_end.setValue(int(values.get("bandwidth_night_end", 7)))
        self.night_end.setSuffix(" h")
        hours = QWidget()
        hours_layout = QHBoxLayout(hours)
        hours_layout.setContentsMargins(0, 0, 0, 0)
        hours_layout.addWidget(QLabel("De"))
        hours_layout.addWidget(self.night_start)
        hours_layout.addWidget(QLabel("até"))
        hours_layout.addWidget(self.night_end)
        hours_layout.addStretch()
        network_form.addRow("Conexão limitada", self.pause_on_metered)
        network_form.addRow("Limite durante o dia", self.bandwidth_day)
        network_form.addRow("Limite durante a noite", self.bandwidth_night)
        network_form.addRow("Horário noturno", hours)
        network_layout.addLayout(network_form)
        network_layout.addWidget(QLabel("Os limites são aplicados aos novos itens. A edição em lote permite ajustar itens já colocados na fila."))
        network_layout.addStretch()
        tabs.addTab(network_page, "Rede")

        session_page = QWidget()
        session_layout = QVBoxLayout(session_page)
        session_layout.setContentsMargins(16, 16, 16, 16)
        session_layout.setSpacing(14)
        session_layout.addWidget(_section_header(
            "Sessão autenticada opcional",
            "Reutiliza cookies locais somente quando o YouTube solicitar uma sessão. O BraXYTDow não recebe sua senha.",
        ))
        session_form = QFormLayout()
        session_form.setSpacing(12)
        self.cookie_mode = QComboBox()
        self.cookie_mode.addItem("Desativado · acesso público", "off")
        self.cookie_mode.addItem("Usar sessão de um navegador", "browser")
        self.cookie_mode.addItem("Usar arquivo cookies.txt", "file")
        self.cookie_mode.setCurrentIndex(max(0, self.cookie_mode.findData(str(values.get("cookie_mode", "off")))))
        self.cookie_browser = QComboBox()
        for browser_id, browser_name in SUPPORTED_BROWSERS:
            self.cookie_browser.addItem(browser_name, browser_id)
        self.cookie_browser.setCurrentIndex(max(0, self.cookie_browser.findData(str(values.get("cookie_browser", "edge")))))
        self.cookie_profile = QLineEdit(str(values.get("cookie_profile", "")))
        self.cookie_profile.setPlaceholderText("Opcional · exemplo: Default ou Profile 1")
        self.cookie_profile.setClearButtonEnabled(True)
        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.cookie_file = QLineEdit(str(values.get("cookie_file", "")))
        self.cookie_file.setPlaceholderText("Arquivo Mozilla/Netscape cookies.txt")
        self.cookie_file.setClearButtonEnabled(True)
        self.cookie_browse = _button("Escolher…", icon_name="folder")
        file_layout.addWidget(self.cookie_file, 1)
        file_layout.addWidget(self.cookie_browse)
        session_form.addRow("Origem", self.cookie_mode)
        session_form.addRow("Navegador", self.cookie_browser)
        session_form.addRow("Perfil", self.cookie_profile)
        session_form.addRow("Arquivo", file_row)
        session_layout.addLayout(session_form)
        self.cookie_consent = QCheckBox(
            "Confirmo que usarei a sessão somente para conteúdo próprio, em domínio público ou para o qual tenho autorização."
        )
        self.cookie_consent.setChecked(bool(values.get("cookie_consent", False)))
        session_layout.addWidget(self.cookie_consent)
        cookie_warning = QLabel(
            "Proteja sua conta: cookies equivalem a uma sessão conectada. Não compartilhe o arquivo, use somente quando necessário e evite downloads excessivos. "
            "O recurso não remove DRM, não concede direitos sobre obras e pode estar sujeito aos Termos do YouTube e às leis de direitos autorais."
        )
        cookie_warning.setObjectName("cookieWarning")
        cookie_warning.setWordWrap(True)
        session_layout.addWidget(cookie_warning)
        privacy_note = QLabel(
            "Privacidade: o BraXYTDow passa a origem diretamente ao processo local do yt-dlp. O conteúdo dos cookies não é copiado para o banco, histórico, logs ou diagnóstico. "
            "Se um provedor PO Token estiver ativo, ele será carregado nesse mesmo processo externo."
        )
        privacy_note.setObjectName("inlineNotice")
        privacy_note.setWordWrap(True)
        session_layout.addWidget(privacy_note)
        session_layout.addStretch()
        self.session_tab_index = tabs.addTab(session_page, "Sessão")

        updates_page = QWidget()
        updates_layout = QVBoxLayout(updates_page)
        updates_layout.setContentsMargins(16, 16, 16, 16)
        updates_form = QFormLayout()
        self.update_channel = QComboBox()
        self.update_channel.addItem("Nightly · correções mais rápidas", "nightly")
        self.update_channel.addItem("Stable · mudanças mensais", "stable")
        channel_index = self.update_channel.findData(str(values.get("update_channel", "nightly")))
        self.update_channel.setCurrentIndex(max(0, channel_index))
        self.app_manifest = QLineEdit(str(values.get("app_update_manifest_url", "")))
        self.app_manifest.setPlaceholderText("https://seu-dominio/latest.json")
        self.app_manifest.setToolTip("Manifesto HTTPS assinado pelo criador, com tamanho, SHA-256 e certificado Authenticode")
        self.app_channel = QComboBox()
        self.app_channel.addItem("Estável", "stable")
        self.app_channel.addItem("Experimental", "experimental")
        self.app_channel.setCurrentIndex(max(0, self.app_channel.findData(str(values.get("app_update_channel", "stable")))))
        self.po_token_provider = QCheckBox("Ativar provedor de PO Token verificado (opcional)")
        self.po_token_provider.setChecked(bool(values.get("po_token_provider", False)))
        self.po_token_manifest = QLineEdit(str(values.get("po_token_manifest_url", "")))
        self.po_token_manifest.setPlaceholderText("Manifesto HTTPS do provedor permitido")
        self.remote_ejs = QCheckBox("Permitir EJS oficial sob demanda após falha local")
        self.remote_ejs.setChecked(bool(values.get("remote_ejs_fallback", True)))
        updates_form.addRow("Canal do yt-dlp", self.update_channel)
        updates_form.addRow("Manifesto do BraXYTDow", self.app_manifest)
        updates_form.addRow("Canal do aplicativo", self.app_channel)
        updates_form.addRow("AutoCura EJS", self.remote_ejs)
        updates_form.addRow("PO Token", self.po_token_provider)
        updates_form.addRow("Manifesto do provedor", self.po_token_manifest)
        updates_layout.addLayout(updates_form)
        security_note = QLabel("Plugins executam somente no processo separado do yt-dlp. O BraXYTDow exige lista permitida, HTTPS, SHA-256, quarentena e teste funcional antes da ativação.")
        security_note.setObjectName("inlineNotice")
        security_note.setWordWrap(True)
        updates_layout.addWidget(security_note)
        updates_layout.addStretch()
        tabs.addTab(updates_page, "Atualizações")

        access_page = QWidget()
        access_layout = QVBoxLayout(access_page)
        access_layout.setContentsMargins(16, 16, 16, 16)
        self.large_text = QCheckBox("Texto ampliado")
        self.large_text.setChecked(bool(values.get("large_text", False)))
        self.high_contrast = QCheckBox("Contraste reforçado")
        self.high_contrast.setChecked(bool(values.get("high_contrast", False)))
        self.reduce_motion = QCheckBox("Reduzir movimento")
        self.reduce_motion.setChecked(not bool(values.get("interface_animations", True)))
        access_layout.addWidget(self.large_text)
        access_layout.addWidget(self.high_contrast)
        access_layout.addWidget(self.reduce_motion)
        access_layout.addWidget(QLabel("Atalhos: Ctrl+L foca o link, Ctrl+J abre a fila, Ctrl+1…4 alterna seções e Ctrl+Shift+C ativa o modo compacto."))
        access_layout.addStretch()
        tabs.addTab(access_page, "Acessibilidade")
        if initial_tab == "session":
            tabs.setCurrentIndex(self.session_tab_index)
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.button(QDialogButtonBox.Save).setText("Salvar")
        buttons.button(QDialogButtonBox.Save).setProperty("role", "primary")
        buttons.button(QDialogButtonBox.Save).setIcon(ui_icon("check", "#FFFFFF", 18))
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        browse.clicked.connect(self._browse)
        self.cookie_browse.clicked.connect(self._browse_cookie_file)
        self.cookie_mode.currentIndexChanged.connect(self._sync_cookie_controls)
        self._sync_cookie_controls()

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Escolher pasta", self.destination.text())
        if selected:
            self.destination.setText(selected)

    def _browse_cookie_file(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Selecionar cookies exportados",
            self.cookie_file.text() or str(Path.home()),
            "Cookies Netscape (*.txt);;Todos os arquivos (*)",
        )
        if selected:
            self.cookie_file.setText(selected)

    def _sync_cookie_controls(self) -> None:
        mode = str(self.cookie_mode.currentData())
        browser_mode = mode == "browser"
        file_mode = mode == "file"
        self.cookie_browser.setEnabled(browser_mode)
        self.cookie_profile.setEnabled(browser_mode)
        self.cookie_file.setEnabled(file_mode)
        self.cookie_browse.setEnabled(file_mode)
        self.cookie_consent.setEnabled(mode != "off")

    def _validate_and_accept(self) -> None:
        config = CookieConfig(
            mode=str(self.cookie_mode.currentData()),
            browser=str(self.cookie_browser.currentData()),
            profile=self.cookie_profile.text().strip(),
            file_path=self.cookie_file.text().strip(),
            consent=self.cookie_consent.isChecked(),
        )
        try:
            config.arguments()
        except ValueError as exc:
            QMessageBox.warning(self, "Sessão não ativada", str(exc))
            self.tabs.setCurrentIndex(self.session_tab_index)
            return
        self.accept()

    def values(self) -> dict[str, Any]:
        return {
            "destination": self.destination.text().strip(),
            "concurrency": self.concurrency.value(),
            "completion_action": self.completion_action.currentData(),
            "open_after": False,
            "interface_animations": self.interface_animations.isChecked() and not self.reduce_motion.isChecked(),
            "auto_tool_updates": self.auto_updates.isChecked(),
            "auto_update_tools": self.auto_updates.isChecked(),
            "update_channel": self.update_channel.currentData(),
            "app_update_manifest_url": self.app_manifest.text().strip(),
            "app_update_channel": self.app_channel.currentData(),
            "po_token_provider": self.po_token_provider.isChecked(),
            "po_token_manifest_url": self.po_token_manifest.text().strip(),
            "cookie_mode": self.cookie_mode.currentData(),
            "cookie_browser": self.cookie_browser.currentData(),
            "cookie_profile": self.cookie_profile.text().strip(),
            "cookie_file": self.cookie_file.text().strip(),
            "cookie_consent": self.cookie_consent.isChecked() if self.cookie_mode.currentData() != "off" else False,
            "remote_ejs_fallback": self.remote_ejs.isChecked(),
            "pause_on_metered": self.pause_on_metered.isChecked(),
            "bandwidth_day": self.bandwidth_day.text().strip(),
            "bandwidth_night": self.bandwidth_night.text().strip(),
            "bandwidth_night_start": self.night_start.value(),
            "bandwidth_night_end": self.night_end.value(),
            "close_to_tray": self.close_to_tray.isChecked(),
            "native_notifications": self.native_notifications.isChecked(),
            "large_text": self.large_text.isChecked(),
            "high_contrast": self.high_contrast.isChecked(),
        }


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aboutDialog")
        self.setWindowTitle(f"Créditos e sobre o {APP_NAME}")
        self.resize(900, 720)
        self.setMinimumSize(620, 540)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("aboutScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("aboutCanvas")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        masthead = QFrame()
        masthead.setObjectName("aboutMasthead")
        masthead_layout = QHBoxLayout(masthead)
        masthead_layout.setContentsMargins(18, 15, 18, 15)
        masthead_layout.setSpacing(12)
        brand_mark = QLabel()
        brand_mark.setObjectName("aboutBrandMark")
        brand_mark.setFixedSize(46, 46)
        brand_mark.setAlignment(Qt.AlignCenter)
        brand_mark.setPixmap(ui_icon("brand", THEME["cyan"], 27).pixmap(27, 27))
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("aboutBrandName")
        subtitle = QLabel("Mídia pública, controle local e autoria responsável")
        subtitle.setObjectName("aboutTagline")
        brand_copy.addWidget(title)
        brand_copy.addWidget(subtitle)
        version = QLabel(f"VERSÃO {__version__}")
        version.setObjectName("aboutVersion")
        masthead_layout.addWidget(brand_mark)
        masthead_layout.addLayout(brand_copy, 1)
        masthead_layout.addWidget(version, 0, Qt.AlignVCenter)
        layout.addWidget(masthead)

        creator_card = QFrame()
        creator_card.setObjectName("creatorCard")
        self.creator_layout = QGridLayout(creator_card)
        self.creator_layout.setContentsMargins(18, 18, 18, 18)
        self.creator_layout.setHorizontalSpacing(24)
        self.creator_layout.setVerticalSpacing(18)

        self.creator_portrait_panel = QFrame()
        self.creator_portrait_panel.setObjectName("creatorPortraitPanel")
        portrait_layout = QVBoxLayout(self.creator_portrait_panel)
        portrait_layout.setContentsMargins(0, 0, 0, 0)
        portrait_layout.setSpacing(8)

        self.creator_avatar = QLabel("RI")
        self.creator_avatar.setObjectName("creatorAvatar")
        self.creator_avatar.setAccessibleName(f"Foto do criador {CREATOR_NAME}")
        self.creator_avatar.setAlignment(Qt.AlignCenter)
        self.creator_avatar.setFixedSize(270, 310)
        portrait = QPixmap(str(resource_dir() / "assets" / CREATOR_AVATAR))
        if not portrait.isNull():
            self.creator_avatar.setPixmap(_creator_portrait(portrait, self.creator_avatar.size()))
        portrait_caption = QLabel("RETRATO DO CRIADOR  ·  IDENTIDADE OFICIAL")
        portrait_caption.setObjectName("portraitCaption")
        portrait_caption.setAlignment(Qt.AlignCenter)
        portrait_layout.addWidget(self.creator_avatar, 0, Qt.AlignCenter)
        portrait_layout.addWidget(portrait_caption)

        self.creator_info_panel = QWidget()
        creator_info = QVBoxLayout(self.creator_info_panel)
        creator_info.setContentsMargins(0, 2, 0, 0)
        creator_info.setSpacing(7)
        badge = QLabel("ASSINATURA DO CRIADOR")
        badge.setObjectName("creatorBadge")
        self.creator_name = QLabel(CREATOR_NAME)
        self.creator_name.setObjectName("creatorName")
        role = QLabel(f"{CREATOR_ROLE} do {APP_NAME}")
        role.setObjectName("creatorRole")
        creator_info.addWidget(badge)
        creator_info.addWidget(self.creator_name)
        creator_info.addWidget(role)

        gold_line = QFrame()
        gold_line.setObjectName("creatorGoldLine")
        gold_line.setFixedHeight(2)
        creator_info.addWidget(gold_line)
        statement = QLabel(
            "Responsável pela criação, desenvolvimento e evolução do BraXYTDow — uma ferramenta pensada para unir clareza, controle e uso responsável de mídia."
        )
        statement.setObjectName("creatorStatement")
        statement.setWordWrap(True)
        creator_info.addWidget(statement)

        credit_tags = QHBoxLayout()
        credit_tags.setSpacing(7)
        for value in ("AUTORIA", "DESENVOLVIMENTO", "PRODUTO"):
            tag = QLabel(value)
            tag.setObjectName("creditTag")
            tag.setAlignment(Qt.AlignCenter)
            credit_tags.addWidget(tag)
        credit_tags.addStretch()
        creator_info.addLayout(credit_tags)
        creator_info.addSpacing(5)

        social_label = QLabel("CANAIS OFICIAIS")
        social_label.setObjectName("socialLabel")
        creator_info.addWidget(social_label)

        social_row = QHBoxLayout()
        social_row.setSpacing(8)
        self.instagram_button = _button(f"Instagram   @{INSTAGRAM_HANDLE}", "instagram")
        self.instagram_button.setObjectName("instagramButton")
        self.instagram_button.setMinimumHeight(48)
        self.instagram_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.instagram_button.setAccessibleName(f"Abrir Instagram de {CREATOR_NAME}")
        self.instagram_button.setProperty("url", INSTAGRAM_URL)
        instagram_icon = resource_dir() / "assets" / "instagram.svg"
        if instagram_icon.is_file():
            self.instagram_button.setIcon(QIcon(str(instagram_icon)))
            self.instagram_button.setIconSize(QSize(20, 20))
        self.discord_button = _button(f"Discord   {DISCORD_HANDLE}", "discord")
        self.discord_button.setObjectName("discordButton")
        self.discord_button.setMinimumHeight(48)
        self.discord_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.discord_button.setAccessibleName(f"Copiar Discord {DISCORD_HANDLE}")
        self.discord_button.setToolTip("Clique para copiar o usuário do Discord")
        social_row.addWidget(self.instagram_button, 1)
        social_row.addWidget(self.discord_button, 1)
        creator_info.addLayout(social_row)
        self.discord_feedback = QLabel("Instagram abre no navegador  ·  Discord copia o usuário")
        self.discord_feedback.setObjectName("creatorHint")
        self._discord_timer = QTimer(self)
        self._discord_timer.setSingleShot(True)
        self._discord_timer.timeout.connect(
            lambda: self.discord_feedback.setText("Instagram abre no navegador  ·  Discord copia o usuário")
        )
        creator_info.addWidget(self.discord_feedback)

        project_note = QFrame()
        project_note.setObjectName("creatorManifesto")
        project_note_layout = QVBoxLayout(project_note)
        project_note_layout.setContentsMargins(12, 10, 12, 10)
        project_note_layout.setSpacing(2)
        project_note_title = QLabel("BRAxYTDow, FEITO COM PROPÓSITO")
        project_note_title.setObjectName("manifestoTitle")
        project_note_text = QLabel("Downloads organizados, ferramentas atualizáveis e uma experiência Windows criada para permanecer clara mesmo quando a tecnologia muda.")
        project_note_text.setObjectName("manifestoText")
        project_note_text.setWordWrap(True)
        project_note_layout.addWidget(project_note_title)
        project_note_layout.addWidget(project_note_text)
        creator_info.addWidget(project_note)
        creator_info.addStretch()

        self._layout_creator(False)
        layout.addWidget(creator_card)

        responsible = QLabel(
            "Use somente conteúdo próprio, em domínio público ou para o qual você tenha autorização. Cookies opcionais reutilizam uma sessão legítima, "
            "mas não concedem direitos e o aplicativo não remove DRM."
        )
        responsible.setObjectName("usageNote")
        responsible.setWordWrap(True)
        layout.addWidget(responsible)

        diagnostic = (
            f"{APP_NAME}: {__version__}\n"
            f"Criador: {CREATOR_NAME}\n"
            f"Python: {platform.python_version()}\n"
            f"Sistema: {platform.platform()}\n"
            f"yt-dlp: {active_binary('yt-dlp') or 'não encontrado'}\n"
            f"FFmpeg: {active_binary('ffmpeg') or 'não encontrado'}\n"
            f"FFprobe: {active_binary('ffprobe') or 'não encontrado'}\n"
            f"Deno: {active_binary('deno') or 'não encontrado'}"
        )

        diagnostic_card = QFrame()
        diagnostic_card.setObjectName("diagnosticCard")
        diagnostic_layout = QVBoxLayout(diagnostic_card)
        diagnostic_layout.setContentsMargins(14, 12, 14, 12)
        diagnostic_layout.setSpacing(9)
        diagnostic_header = QHBoxLayout()
        diagnostic_copy = QVBoxLayout()
        diagnostic_copy.setSpacing(2)
        diagnostic_title = QLabel("Diagnóstico técnico")
        diagnostic_title.setObjectName("diagnosticTitle")
        diagnostic_subtitle = QLabel("Versões locais e caminhos das ferramentas, ocultos por padrão.")
        diagnostic_subtitle.setObjectName("diagnosticSubtitle")
        diagnostic_copy.addWidget(diagnostic_title)
        diagnostic_copy.addWidget(diagnostic_subtitle)
        self.diagnostic_toggle = _button("Mostrar detalhes", "ghost", "tools")
        self.diagnostic_toggle.setCheckable(True)
        diagnostic_header.addLayout(diagnostic_copy, 1)
        diagnostic_header.addWidget(self.diagnostic_toggle)
        diagnostic_layout.addLayout(diagnostic_header)
        self.diagnostic_panel = QFrame()
        self.diagnostic_panel.setObjectName("diagnosticPanel")
        panel_layout = QVBoxLayout(self.diagnostic_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)
        self.diagnostic_text = QTextEdit()
        self.diagnostic_text.setObjectName("diagnosticText")
        self.diagnostic_text.setPlainText(diagnostic)
        self.diagnostic_text.setReadOnly(True)
        self.diagnostic_text.setAccessibleName("Informações de diagnóstico")
        self.diagnostic_text.setMinimumHeight(135)
        self.diagnostic_text.setMaximumHeight(160)
        panel_actions = QHBoxLayout()
        copy = _button("Copiar diagnóstico", icon_name="paste")
        panel_actions.addStretch()
        panel_actions.addWidget(copy)
        panel_layout.addWidget(self.diagnostic_text)
        panel_layout.addLayout(panel_actions)
        self.diagnostic_panel.hide()
        diagnostic_layout.addWidget(self.diagnostic_panel)
        layout.addWidget(diagnostic_card)

        row = QHBoxLayout()
        footer = QLabel(f"{APP_NAME} {__version__}  ·  Criado por {CREATOR_NAME}")
        footer.setObjectName("aboutFooter")
        close = _button("Fechar", "primary", "check")
        row.addWidget(footer)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        self.instagram_button.clicked.connect(lambda _checked=False: QDesktopServices.openUrl(QUrl(INSTAGRAM_URL)))
        self.discord_button.clicked.connect(self._copy_discord)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(diagnostic))
        copy.clicked.connect(self._diagnostic_copied)
        self.diagnostic_toggle.toggled.connect(self._toggle_diagnostic)
        close.clicked.connect(self.accept)

    def _layout_creator(self, compact: bool) -> None:
        layout = self.creator_layout
        layout.removeWidget(self.creator_portrait_panel)
        layout.removeWidget(self.creator_info_panel)
        if compact:
            layout.addWidget(self.creator_portrait_panel, 0, 0, 1, 1, Qt.AlignHCenter)
            layout.addWidget(self.creator_info_panel, 1, 0)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 0)
        else:
            layout.addWidget(self.creator_portrait_panel, 0, 0, 1, 1, Qt.AlignTop)
            layout.addWidget(self.creator_info_panel, 0, 1)
            layout.setColumnStretch(0, 0)
            layout.setColumnStretch(1, 1)
        self._creator_compact = compact

    def _toggle_diagnostic(self, visible: bool) -> None:
        self.diagnostic_panel.setVisible(visible)
        self.diagnostic_toggle.setText("Ocultar detalhes" if visible else "Mostrar detalhes")

    def _diagnostic_copied(self) -> None:
        self.diagnostic_toggle.setText("Diagnóstico copiado")
        QTimer.singleShot(
            1800,
            lambda: self.diagnostic_toggle.setText(
                "Ocultar detalhes" if self.diagnostic_panel.isVisible() else "Mostrar detalhes"
            ),
        )

    def _copy_discord(self) -> None:
        QApplication.clipboard().setText(DISCORD_HANDLE)
        self.discord_feedback.setText(f"Discord copiado: {DISCORD_HANDLE}")
        self._discord_timer.start(2600)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        compact = self.width() < 760
        if getattr(self, "_creator_compact", None) != compact:
            self._layout_creator(compact)


class HealthNode(QFrame):
    """Compact stage in the AutoCura media-toolchain health line."""

    def __init__(self, key: str, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setObjectName("healthNode")
        self.setProperty("state", "idle")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        header = QHBoxLayout()
        header.setSpacing(7)
        self.mark = QLabel()
        self.mark.setPixmap(ui_icon("tools", THEME["muted"], 17).pixmap(17, 17))
        self.title = QLabel(title)
        self.title.setObjectName("healthTitle")
        header.addWidget(self.mark)
        header.addWidget(self.title, 1)
        self.status = QLabel("Aguardando")
        self.status.setObjectName("healthStatus")
        layout.addLayout(header)
        layout.addWidget(self.status)
        self.detail = QLabel(subtitle)
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

    def set_state(self, state: str, status: str, detail: str = "") -> None:
        color = {
            "healthy": THEME["success"],
            "degraded": THEME["warning"],
            "failed": THEME["danger"],
            "busy": THEME["cyan"],
        }.get(state, THEME["muted"])
        icon_name = "check" if state == "healthy" else "warning" if state in {"degraded", "failed"} else "tools"
        self.mark.setPixmap(ui_icon(icon_name, color, 17).pixmap(17, 17))
        self.status.setText(status)
        if detail:
            self.detail.setText(detail)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)


class ToolCard(QFrame):
    def __init__(self, tool: str, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tool = tool
        self.setObjectName("toolCard")
        layout = QGridLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setHorizontalSpacing(16)
        # Tool discovery may copy and hash the packaged FFmpeg distribution.
        # Keep construction cheap; the asynchronous bootstrap supplies the
        # authoritative state immediately after the window becomes responsive.
        self.icon = QLabel()
        self.icon.setObjectName("toolIcon")
        self.icon.setPixmap(ui_icon("tools", THEME["cyan"], 20).pixmap(20, 20))
        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        self.description = QLabel(description)
        self.description.setObjectName("muted")
        self.description.setWordWrap(True)
        self.version = QLabel("Preparando…")
        self.version.setObjectName("toolVersion")
        self.path = QLabel("Verificando a instalação em segundo plano…")
        self.path.setObjectName("muted")
        self.path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path.setWordWrap(True)
        self.progress = AnimatedProgressBar()
        self.progress.set_animations_enabled(True)
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.update_button = _button("Atualizar", icon_name="retry")
        self.update_button.setAccessibleName(f"Atualizar {title}")
        self.rollback_button = _button("Versão anterior", "ghost", "history")
        self.rollback_button.setAccessibleName(f"Restaurar versão anterior de {title}")
        self.rollback_button.setToolTip("Valida e ativa a cópia anterior preservada pelo AutoCura")
        layout.addWidget(self.icon, 0, 0, 3, 1)
        layout.addWidget(self.title, 0, 1)
        layout.addWidget(self.version, 0, 2, 1, 2, Qt.AlignRight)
        layout.addWidget(self.description, 1, 1, 1, 3)
        layout.addWidget(self.path, 2, 1)
        layout.addWidget(self.rollback_button, 2, 2)
        layout.addWidget(self.update_button, 2, 3)
        layout.addWidget(self.progress, 3, 1, 1, 3)
        layout.setColumnStretch(1, 1)

    def set_state(self, version: str, path: str, detail: str = "") -> None:
        installed = bool(path)
        state_icon = "check" if installed else "warning"
        state_color = THEME["success"] if installed else THEME["danger"]
        self.icon.setPixmap(ui_icon(state_icon, state_color, 20).pixmap(20, 20))
        self.icon.setProperty("ok", installed)
        self.icon.style().unpolish(self.icon)
        self.icon.style().polish(self.icon)
        self.version.setText(version or ("Instalado" if installed else "Não encontrado"))
        raw_detail = detail or path or "Ferramenta ainda não instalada"
        self.path.setToolTip(raw_detail if installed else "")
        if installed and (len(raw_detail) > 96 or ":\\" in raw_detail or raw_detail.startswith("/")):
            source_note = raw_detail.partition(" · origem: ")[2]
            managed = f"Instalação gerenciada · {Path(path).name}"
            self.path.setText(f"{managed} · origem: {source_note}" if source_note else managed)
        else:
            self.path.setText(raw_detail)

    def set_progress(self, percent: int, message: str) -> None:
        self.progress.show()
        self.progress.setValue(max(0, min(100, percent)))
        if message:
            self.path.setText(message)

    def finish_progress(self) -> None:
        self.progress.hide()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 840)
        self.setMinimumSize(760, 620)
        self.setAcceptDrops(True)

        self.storage = Storage()
        self.settings = self.storage.settings()
        set_remote_ejs_enabled(bool(self.settings.get("remote_ejs_fallback", True)))
        self.analyzer = Analyzer(
            self,
            po_token_provider=bool(self.settings.get("po_token_provider", False)),
            cookie_config=CookieConfig.from_mapping(self.settings),
        )
        self.manager = DownloadManager(
            self.storage,
            int(self.settings.get("concurrency", 2)),
            self,
            settings=self.settings,
        )
        self.media: MediaInfo | None = None
        self._media_url = ""
        self._thumbnail_source = QPixmap()
        self.rows: dict[str, int] = {}
        self.output_paths: dict[str, str] = {}
        self.queue_progress: dict[str, DownloadProgress] = {}
        self.history_entries: list[HistoryEntry] = []
        self.network = QNetworkAccessManager(self)
        self.tool_updater: Any | None = None
        self.app_updater: AppUpdateManager | None = None
        self.po_plugin_controller: Any | None = None
        self._tool_bootstrap_pending = False
        self._compatibility_repair_running = False
        self._compatibility_reason = ""
        self._pending_analysis_repair = False
        self._pending_download_repairs: set[str] = set()
        self._analysis_repair_attempted_url = ""
        self._automatic_analysis_retry = False
        self._download_repair_attempted: set[str] = set()
        self._compact_navigation = False
        self._animations_enabled = bool(self.settings.get("interface_animations", True))
        self._applying_preset = False
        self._queue_completion_handled = False
        self._compact_mode = False
        self._normal_geometry: Any | None = None
        self._advanced = self._advanced_from_settings()
        self._metadata_overrides: dict[str, str] = {}
        self._selected_chapters: list[dict[str, Any]] = []
        self._last_notification_path = ""
        self._force_close = False
        self.tray: QSystemTrayIcon | None = None

        self._build_ui()
        self._sync_cookie_badge()
        self._connect()
        self._apply_accessibility()
        self._setup_tray()
        self._load_history()
        self._setup_tool_updates()
        self._setup_po_plugin()
        self._setup_app_updates()
        self._apply_responsive_layout()

    def _advanced_from_settings(self) -> dict[str, Any]:
        defaults = {
            "subtitle_languages": "pt.*,en.*",
            "embed_subtitles": True,
            "audio_quality": "0",
            "filename_template": "%(title).180B [%(id)s].%(ext)s",
            "concurrent_fragments": 4,
            "retries": 10,
            "rate_limit": "",
            "write_description": False,
            "write_info_json": False,
            "sponsorblock": False,
            "trim_start": "",
            "trim_end": "",
            "playlist_reverse": False,
            "overwrite": False,
        }
        return {key: self.settings.get(key, value) for key, value in defaults.items()}

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(228)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(12, 22, 12, 12)
        side.setSpacing(8)

        brand_block = QWidget()
        brand_block.setObjectName("brandBlock")
        brand_row = QHBoxLayout(brand_block)
        brand_row.setContentsMargins(9, 0, 7, 0)
        brand_row.setSpacing(10)
        self.brand_mark = QLabel()
        self.brand_mark.setObjectName("brandMark")
        self.brand_mark.setFixedSize(34, 34)
        self.brand_mark.setAlignment(Qt.AlignCenter)
        self.brand_mark.setPixmap(ui_icon("brand", THEME["accent"], 22).pixmap(22, 22))
        self.brand_text_block = QWidget()
        brand_text_layout = QVBoxLayout(self.brand_text_block)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(0)
        self.brand = QLabel(APP_NAME)
        self.brand.setObjectName("brand")
        self.brand_subtitle = QLabel(APP_TAGLINE)
        self.brand_subtitle.setObjectName("brandSubtitle")
        brand_text_layout.addWidget(self.brand)
        brand_text_layout.addWidget(self.brand_subtitle)
        brand_row.addWidget(self.brand_mark)
        brand_row.addWidget(self.brand_text_block, 1)
        side.addWidget(brand_block)
        side.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, (icon_name, label) in enumerate((("download", "Baixar"), ("queue", "Fila"), ("history", "Biblioteca"), ("tools", "Ferramentas"))):
            button = _button(label, icon_name=icon_name)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setProperty("iconName", icon_name)
            button.setProperty("label", label)
            button.setAccessibleName(label)
            button.setIconSize(QSize(20, 20))
            button.setMinimumHeight(44)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        side.addStretch()
        side.addWidget(_transfer_divider(completed=2))
        side.addSpacing(10)
        self.settings_button = _button("Configurações", "ghost", "settings")
        self.settings_button.setAccessibleName("Abrir configurações")
        self.about_button = _button("Créditos", "ghost", "credits")
        self.about_button.setObjectName("creditsButton")
        self.about_button.setIcon(ui_icon("credits", THEME["gold"], 18))
        self.about_button.setAccessibleName(f"Créditos e sobre o {APP_NAME}")
        side.addWidget(self.settings_button)
        side.addWidget(self.about_button)
        self.sidebar_version = QLabel(f"versão {__version__}")
        self.sidebar_version.setObjectName("sidebarVersion")
        side.addWidget(self.sidebar_version)
        shell.addWidget(self.sidebar)

        main = QWidget()
        main.setObjectName("mainSurface")
        main_layout = QVBoxLayout(main)
        self.main_layout = main_layout
        main_layout.setContentsMargins(28, 24, 28, 16)
        main_layout.setSpacing(12)
        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        self.page_title = QLabel(PAGE_META[0][0])
        self.page_title.setObjectName("pageTitle")
        self.page_title.setMinimumWidth(0)
        self.page_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.page_description = QLabel(PAGE_META[0][1])
        self.page_description.setObjectName("muted")
        self.page_description.setWordWrap(True)
        self.page_description.setMinimumWidth(0)
        self.page_description.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title_block.addWidget(self.page_title)
        title_block.addWidget(self.page_description)
        self.activity_pill = QFrame()
        self.activity_pill.setObjectName("activityPill")
        activity_layout = QHBoxLayout(self.activity_pill)
        activity_layout.setContentsMargins(12, 6, 12, 6)
        activity_layout.setSpacing(7)
        self.activity_dot = QLabel("●")
        self.activity_dot.setObjectName("activityDot")
        self.activity_label = QLabel("Pronto")
        self.activity_label.setObjectName("activityText")
        activity_layout.addWidget(self.activity_dot)
        activity_layout.addWidget(self.activity_label)
        header.addLayout(title_block, 1)
        header.addWidget(self.activity_pill, 0, Qt.AlignTop)
        main_layout.addLayout(header)
        self.flow_rail = TransferRail(stages=("Link", "Prévia", "Fila", "Ferramentas"))
        self.flow_rail.set_stage(0, completed=0)
        self.flow_rail.set_animations_enabled(self._animations_enabled)
        main_layout.addWidget(self.flow_rail)

        self.pages = AnimatedStackedWidget()
        self.pages.set_animations_enabled(self._animations_enabled)
        self.pages.addWidget(self._build_download_page())
        self.pages.addWidget(self._build_queue_page())
        self.pages.addWidget(self._build_history_page())
        self.pages.addWidget(self._build_tools_page())
        main_layout.addWidget(self.pages, 1)
        shell.addWidget(main, 1)

        self.statusBar().setObjectName("operationBar")
        self.statusBar().showMessage("Pronto")
        self.statusBar().setSizeGripEnabled(False)
        speed_widget, self.footer_speed_label = _status_metric("download", "0 B/s")
        queue_widget, self.footer_queue_label = _status_metric("queue", "0 na fila")
        tools_widget, self.footer_tools_label = _status_metric("tools", "Ferramentas")
        self.statusBar().addPermanentWidget(speed_widget)
        for metric in (queue_widget, tools_widget):
            divider = QFrame()
            divider.setObjectName("metricDivider")
            self.statusBar().addPermanentWidget(divider)
            self.statusBar().addPermanentWidget(metric)
        for card in self.tool_cards.values():
            card.progress.set_animations_enabled(self._animations_enabled)
        self._restore_download_defaults()
        self._install_shortcuts()

    def _build_download_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 2, 8, 10)
        layout.setSpacing(12)

        link_card = QFrame()
        link_card.setObjectName("primaryCard")
        link_layout = QVBoxLayout(link_card)
        link_layout.setContentsMargins(18, 17, 18, 17)
        link_layout.setSpacing(10)
        link_layout.addWidget(_section_header("Link do YouTube", "Vídeo, Shorts ou playlist pública."))
        url_row = QHBoxLayout()
        url_row.setSpacing(9)
        self.url = QLineEdit()
        self.url.setPlaceholderText("Cole uma URL do YouTube…")
        self.url.setClearButtonEnabled(True)
        self.url.setAccessibleName("URL do YouTube")
        self.url.setProperty("primary", True)
        _add_line_icon(self.url, "link")
        self.paste_button = _button("Colar", icon_name="paste")
        self.analyze_button = _button("Analisar", "primary", "search")
        self.analyze_button.setDefault(True)
        self.analyze_button.setAccessibleDescription("Obtém título, miniatura e formatos disponíveis")
        url_row.addWidget(self.url, 1)
        url_row.addWidget(self.paste_button)
        url_row.addWidget(self.analyze_button)
        link_layout.addLayout(url_row)
        self.cookie_strip = QFrame()
        self.cookie_strip.setObjectName("sessionStrip")
        cookie_layout = QHBoxLayout(self.cookie_strip)
        cookie_layout.setContentsMargins(11, 7, 8, 7)
        cookie_layout.setSpacing(8)
        self.cookie_state_icon = QLabel()
        self.cookie_state_icon.setFixedSize(20, 20)
        self.cookie_state_icon.setAlignment(Qt.AlignCenter)
        self.cookie_state_label = QLabel("Sessão desativada · acesso público")
        self.cookie_state_label.setObjectName("sessionState")
        self.cookie_state_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.cookie_settings_button = _button("Configurar sessão", "ghost", "settings")
        self.cookie_settings_button.setAccessibleDescription(
            "Configura cookies locais para conteúdo que exige uma sessão autorizada"
        )
        cookie_layout.addWidget(self.cookie_state_icon)
        cookie_layout.addWidget(self.cookie_state_label, 1)
        cookie_layout.addWidget(self.cookie_settings_button)
        link_layout.addWidget(self.cookie_strip)
        layout.addWidget(link_card)

        self.preview = QFrame()
        self.preview.setObjectName("card")
        preview_layout = QGridLayout(self.preview)
        preview_layout.setContentsMargins(14, 14, 14, 14)
        preview_layout.setHorizontalSpacing(18)
        preview_layout.setVerticalSpacing(7)
        self.thumbnail = QLabel()
        self.thumbnail.setObjectName("thumbnail")
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setFixedSize(208, 117)
        self.thumbnail.setPixmap(ui_icon("play", THEME["faint"], 42).pixmap(42, 42))
        self._thumbnail_source = QPixmap()
        self.thumbnail.setAccessibleName("Miniatura do conteúdo analisado")
        self.preview_badge = QLabel("Aguardando link")
        self.preview_badge.setObjectName("statusPill")
        self.preview_badge.setProperty("state", "idle")
        self.title = QLabel("Cole uma URL para ver os detalhes")
        self.title.setObjectName("mediaTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("Título, canal e duração aparecerão aqui")
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        self.kind = QLabel("A análise não inicia o download.")
        self.kind.setObjectName("muted")
        self.kind.setWordWrap(True)
        preview_layout.addWidget(self.thumbnail, 0, 0, 4, 1)
        preview_layout.addWidget(self.preview_badge, 0, 1, Qt.AlignLeft)
        preview_layout.addWidget(self.title, 1, 1)
        preview_layout.addWidget(self.meta, 2, 1)
        preview_layout.addWidget(self.kind, 3, 1)
        preview_layout.setColumnStretch(1, 1)
        layout.addWidget(self.preview)
        layout.addWidget(_transfer_divider(completed=1))

        options_card = QFrame()
        options_card.setObjectName("card")
        options_layout = QVBoxLayout(options_card)
        options_layout.setContentsMargins(16, 14, 16, 15)
        options_layout.setSpacing(10)
        options_header = QHBoxLayout()
        options_header.addWidget(_section_header("Opções de saída", "Defina formato, qualidade, destino e itens extras."), 1)
        self.metadata_editor_button = _button("Nome e capa", "ghost", "video")
        self.advanced_button = _button("Opções avançadas", "ghost", "settings")
        options_header.addWidget(self.metadata_editor_button, 0, Qt.AlignTop)
        options_header.addWidget(self.advanced_button, 0, Qt.AlignTop)
        options_layout.addLayout(options_header)

        preset_row = QHBoxLayout()
        preset_label = QLabel("Preset")
        preset_label.setObjectName("fieldLabel")
        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName("Preset de download")
        self.save_preset_button = _button("Salvar atual", "ghost", "check")
        self.delete_preset_button = _button("Excluir", "ghost", "trash")
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.save_preset_button)
        preset_row.addWidget(self.delete_preset_button)
        options_layout.addLayout(preset_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        self.media_type = QComboBox()
        self.media_type.addItem(ui_icon("video", THEME["text_soft"], 17), "Vídeo + áudio", "video")
        self.media_type.addItem(ui_icon("audio", THEME["text_soft"], 17), "Somente áudio", "audio")
        self.format = QComboBox()
        self.quality = QComboBox()
        self.destination = QLineEdit(str(self.settings.get("destination", Path.home() / "Downloads")))
        self.destination.setAccessibleName("Pasta de destino")
        _add_line_icon(self.destination, "folder")
        self.destination_browse = _button("Escolher…", icon_name="folder")
        for column, (label_text, widget) in enumerate((("Tipo", self.media_type), ("Formato", self.format), ("Qualidade", self.quality))):
            label = QLabel(label_text)
            label.setObjectName("fieldLabel")
            label.setBuddy(widget)
            grid.addWidget(label, 0, column)
            grid.addWidget(widget, 1, column)
        destination_label = QLabel("Pasta de destino")
        destination_label.setObjectName("fieldLabel")
        destination_label.setBuddy(self.destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(self.destination_browse)
        grid.addWidget(destination_label, 2, 0, 1, 3)
        grid.addLayout(destination_row, 3, 0, 1, 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        options_layout.addLayout(grid)

        self.extras_grid = QGridLayout()
        self.extras_grid.setHorizontalSpacing(18)
        self.extras_grid.setVerticalSpacing(8)
        self.thumbnail_option = QCheckBox("Incorporar miniatura")
        self.metadata_option = QCheckBox("Incorporar metadados")
        self.subtitle_option = QCheckBox("Baixar legendas")
        self.split_chapters_option = QCheckBox("Dividir por capítulos")
        self.split_chapters_option.setEnabled(False)
        self.split_chapters_option.setToolTip("Disponível quando a análise encontrar capítulos")
        self.extra_options = (self.thumbnail_option, self.metadata_option, self.subtitle_option, self.split_chapters_option)
        for column, option in enumerate(self.extra_options):
            self.extras_grid.addWidget(option, 0, column)
        self.extras_grid.setColumnStretch(3, 1)
        options_layout.addLayout(self.extras_grid)

        action_row = QHBoxLayout()
        summary_block = QVBoxLayout()
        summary_block.setSpacing(2)
        self.option_summary = QLabel("Padrão seguro e compatível selecionado")
        self.option_summary.setObjectName("muted")
        self.output_preview = QLabel("Analise um link para estimar o arquivo de saída")
        self.output_preview.setObjectName("outputPreview")
        self.output_preview.setWordWrap(True)
        self.add_button = _button("Adicionar à fila", "primary", "download")
        self.add_button.setEnabled(False)
        self.add_button.setMinimumWidth(170)
        summary_block.addWidget(self.option_summary)
        summary_block.addWidget(self.output_preview)
        action_row.addLayout(summary_block, 1)
        action_row.addWidget(self.add_button)
        options_layout.addLayout(action_row)
        layout.addWidget(options_card)
        layout.addWidget(_transfer_divider(completed=2))
        notice = QLabel(
            "Uso responsável: baixe somente conteúdo próprio, em domínio público ou autorizado. Cookies opcionais apenas reutilizam sua sessão local; "
            "não concedem direitos, não removem DRM e não devem ser usados para violar direitos autorais ou controles de acesso."
        )
        notice.setObjectName("usageNote")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        layout.addStretch()
        page.setWidget(content)
        return page

    def _build_queue_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(12)
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 11, 12, 11)
        toolbar_layout.setSpacing(8)
        filter_row = QHBoxLayout()
        self.queue_search = QLineEdit()
        self.queue_search.setPlaceholderText("Pesquisar na fila…")
        self.queue_search.setClearButtonEnabled(True)
        self.queue_search.setAccessibleName("Pesquisar na fila")
        _add_line_icon(self.queue_search, "search")
        self.queue_filter = QComboBox()
        self.queue_filter.addItem("Todos os estados", "")
        for status in DownloadStatus:
            self.queue_filter.addItem(STATUS_LABELS[status], status.value)
        self.queue_count = QLabel("Fila vazia")
        self.queue_count.setObjectName("counterPill")
        self.compact_mode_button = _button("Modo compacto", "ghost", "queue")
        self.compact_mode_button.setCheckable(True)
        self.compact_mode_button.setToolTip("Mantém uma fila menor e sempre visível")
        filter_row.addWidget(self.queue_search, 1)
        filter_row.addWidget(self.queue_filter)
        filter_row.addWidget(self.queue_count)
        filter_row.addWidget(self.compact_mode_button)
        toolbar_layout.addLayout(filter_row)

        actions = QGridLayout()
        actions.setHorizontalSpacing(7)
        actions.setVerticalSpacing(7)
        self.queue_actions_layout = actions
        self.pause_button = _button("Pausar", icon_name="pause")
        self.resume_button = _button("Retomar", icon_name="retry")
        self.cancel_button = _button("Cancelar", "danger", "cancel", THEME["danger"])
        self.remove_button = _button("Remover", "ghost", "trash")
        self.open_button = _button("Abrir arquivo", icon_name="external")
        self.batch_edit_button = _button("Editar selecionados", "ghost", "settings")
        self.clear_completed_button = _button("Limpar concluídos", "ghost", "trash")
        self.queue_action_buttons = (
            self.pause_button,
            self.resume_button,
            self.cancel_button,
            self.remove_button,
            self.open_button,
            self.batch_edit_button,
            self.clear_completed_button,
        )
        for column, button in enumerate(self.queue_action_buttons[:-1]):
            button.setEnabled(False)
            actions.addWidget(button, 0, column)
        actions.setColumnStretch(5, 1)
        actions.addWidget(self.clear_completed_button, 0, 6)
        toolbar_layout.addLayout(actions)
        layout.addWidget(toolbar)

        self.queue_stack = AnimatedStackedWidget(transition_duration=140)
        self.queue_stack.set_animations_enabled(self._animations_enabled)
        self.table = QueueTableWidget(0, 7)
        self.table.setObjectName("dataTable")
        self.table.setAccessibleName("Fila de downloads")
        self.table.setHorizontalHeaderLabels(["Título", "Status", "Progresso", "Formato", "Velocidade", "ETA", "Detalhes"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(54)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_empty = EmptyState(
            "queue",
            "Sua fila está vazia",
            "Analise um link na página Baixar e adicione o conteúdo aqui.",
            action_text="Ir para Baixar",
        )
        self.queue_stack.addWidget(self.table)
        self.queue_stack.addWidget(self.queue_empty)
        self.queue_stack.setCurrentWidget(self.queue_empty)
        layout.addWidget(self.queue_stack, 1)
        return page

    def _build_history_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(12)
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 11, 12, 11)
        toolbar_layout.setSpacing(8)
        filter_row = QGridLayout()
        filter_row.setHorizontalSpacing(8)
        filter_row.setVerticalSpacing(8)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Pesquisar título, canal, URL ou destino…")
        self.history_search.setClearButtonEnabled(True)
        self.history_search.setAccessibleName("Pesquisar no histórico")
        _add_line_icon(self.history_search, "search")
        self.history_filter = QComboBox()
        self.history_filter.addItem("Todos", "")
        self.history_filter.addItem("Concluídos", DownloadStatus.COMPLETED.value)
        self.history_filter.addItem("Com erro", DownloadStatus.ERROR.value)
        self.history_format_filter = QComboBox()
        self.history_format_filter.addItem("Todos os formatos", "")
        for value in ("mp4", "webm", "mp3", "m4a", "opus", "wav"):
            self.history_format_filter.addItem(value.upper(), value)
        self.history_duration_filter = QComboBox()
        self.history_duration_filter.addItem("Qualquer duração", "")
        self.history_duration_filter.addItem("Até 5 min", "short")
        self.history_duration_filter.addItem("5 a 20 min", "medium")
        self.history_duration_filter.addItem("Mais de 20 min", "long")
        self.history_date_filter = QComboBox()
        self.history_date_filter.addItem("Qualquer data", 0)
        self.history_date_filter.addItem("Últimos 7 dias", 7)
        self.history_date_filter.addItem("Últimos 30 dias", 30)
        self.history_file_filter = QComboBox()
        self.history_file_filter.addItem("Todos os arquivos", "")
        self.history_file_filter.addItem("Disponíveis", "present")
        self.history_file_filter.addItem("Movidos ou apagados", "missing")
        self.history_count = QLabel()
        self.history_count.setObjectName("counterPill")
        filter_row.addWidget(self.history_search, 0, 0, 1, 3)
        filter_row.addWidget(self.history_count, 0, 3)
        filter_row.addWidget(self.history_filter, 1, 0)
        filter_row.addWidget(self.history_format_filter, 1, 1)
        filter_row.addWidget(self.history_duration_filter, 1, 2)
        filter_row.addWidget(self.history_date_filter, 1, 3)
        filter_row.addWidget(self.history_file_filter, 2, 0, 1, 4)
        for column in range(4):
            filter_row.setColumnStretch(column, 1)
        toolbar_layout.addLayout(filter_row)

        actions = QGridLayout()
        actions.setHorizontalSpacing(7)
        actions.setVerticalSpacing(7)
        self.history_open_button = _button("Abrir local", icon_name="external")
        self.history_copy_button = _button("Copiar link", "ghost", "link")
        self.history_redownload_button = _button("Baixar novamente", "ghost", "retry")
        self.history_locate_button = _button("Localizar arquivo", "ghost", "folder")
        self.library_cleanup_button = _button("Limpeza segura", "ghost", "trash")
        self.history_refresh_button = _button("Atualizar", "ghost", "retry")
        self.history_open_button.setEnabled(False)
        self.history_copy_button.setEnabled(False)
        self.history_redownload_button.setEnabled(False)
        self.history_locate_button.setEnabled(False)
        history_actions = (
            self.history_open_button,
            self.history_copy_button,
            self.history_redownload_button,
            self.history_locate_button,
            self.library_cleanup_button,
            self.history_refresh_button,
        )
        for index, button in enumerate(history_actions):
            actions.addWidget(button, index // 2, index % 2)
        for column in range(2):
            actions.setColumnStretch(column, 1)
        toolbar_layout.addLayout(actions)
        layout.addWidget(toolbar)

        self.history_stack = AnimatedStackedWidget(transition_duration=140)
        self.history_stack.set_animations_enabled(self._animations_enabled)
        self.history_table = QTableWidget(0, 7)
        self.history_table.setObjectName("dataTable")
        self.history_table.setAccessibleName("Histórico de downloads")
        self.history_table.setHorizontalHeaderLabels(["Título", "Canal", "Formato", "Duração", "Status", "Data", "Destino"])
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setShowGrid(False)
        self.history_table.verticalHeader().hide()
        self.history_table.verticalHeader().setDefaultSectionSize(46)
        history_header = self.history_table.horizontalHeader()
        history_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4, 5):
            history_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.history_empty = EmptyState("history", "Sua biblioteca está vazia", "Downloads concluídos e falhas aparecerão aqui com verificação do arquivo.")
        self.history_stack.addWidget(self.history_table)
        self.history_stack.addWidget(self.history_empty)
        layout.addWidget(self.history_stack, 1)
        return page

    def _build_tools_page(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 2, 8, 10)
        layout.setSpacing(12)
        banner = QLabel(f"O YouTube muda com frequência. O {APP_NAME} pode verificar e atualizar seus mecanismos sem reinstalar o aplicativo.")
        banner.setObjectName("usageNote")
        banner.setWordWrap(True)
        layout.addWidget(banner)

        # Intent: the user needs one calm, legible answer—whether the complete
        # media chain works—and one decisive recovery action.
        # Palette: cobalt action, cyan active checks, mint/amber/coral health.
        # Depth: quiet borders and tonal shifts, matching the existing studio.
        # Surfaces: one raised AutoCura bay above the individual tool cards.
        # Typography: compact labels with strong status text and tabular versions.
        # Spacing: 4 px rhythm; 12 px nodes; 18 px bay padding.
        compatibility = QFrame()
        compatibility.setObjectName("compatibilityHero")
        compatibility_layout = QVBoxLayout(compatibility)
        compatibility_layout.setContentsMargins(18, 16, 18, 16)
        compatibility_layout.setSpacing(12)
        compatibility_header = QHBoxLayout()
        compatibility_copy = QVBoxLayout()
        compatibility_copy.setSpacing(3)
        title_row = QHBoxLayout()
        autocure_title = QLabel("AutoCura")
        autocure_title.setObjectName("sectionTitle")
        self.compatibility_badge = QLabel("Proteção ativa")
        self.compatibility_badge.setObjectName("statusPill")
        self.compatibility_badge.setProperty("state", "success")
        title_row.addWidget(autocure_title)
        title_row.addWidget(self.compatibility_badge)
        title_row.addStretch()
        self.compatibility_summary = QLabel("Teste a cadeia completa antes de precisar dela. Versões problemáticas são isoladas e podem ser revertidas.")
        self.compatibility_summary.setObjectName("muted")
        self.compatibility_summary.setWordWrap(True)
        compatibility_copy.addLayout(title_row)
        compatibility_copy.addWidget(self.compatibility_summary)
        self.health_check_button = _button("Testar cadeia", icon_name="search")
        self.autocure_button = _button("Executar AutoCura", "primary", "tools")
        compatibility_header.addLayout(compatibility_copy, 1)
        compatibility_header.addWidget(self.health_check_button, 0, Qt.AlignTop)
        compatibility_header.addWidget(self.autocure_button, 0, Qt.AlignTop)
        compatibility_layout.addLayout(compatibility_header)

        pipeline = QHBoxLayout()
        pipeline.setSpacing(7)
        self.health_nodes = {
            "youtube": HealthNode("youtube", "YouTube", "Acesso público"),
            "yt-dlp": HealthNode("yt-dlp", "yt-dlp", "Extração e formatos"),
            "deno": HealthNode("deno", "Deno / EJS", "Desafios JavaScript"),
            "ffmpeg": HealthNode("ffmpeg", "FFmpeg", "Conversão e união"),
            "file": HealthNode("file", "Arquivo", "Saída final íntegra"),
        }
        for index, node in enumerate(self.health_nodes.values()):
            pipeline.addWidget(node, 1)
            if index < len(self.health_nodes) - 1:
                connector = QFrame()
                connector.setObjectName("healthConnector")
                connector.setFixedSize(10, 2)
                pipeline.addWidget(connector, 0, Qt.AlignVCenter)
        compatibility_layout.addLayout(pipeline)
        layout.addWidget(compatibility)

        tools_toolbar = QFrame()
        tools_toolbar.setObjectName("toolbar")
        action_row = QHBoxLayout(tools_toolbar)
        action_row.setContentsMargins(12, 10, 12, 10)
        self.auto_update_option = QCheckBox("Verificação automática")
        self.auto_update_option.setChecked(bool(self.settings.get("auto_tool_updates", self.settings.get("auto_update_tools", True))))
        self.tools_status_label = QLabel("Carregando ferramentas…")
        self.tools_status_label.setObjectName("muted")
        self.check_updates_button = _button("Verificar atualizações", icon_name="search")
        self.update_all_button = _button("Atualizar tudo", "primary", "retry")
        action_row.addWidget(self.auto_update_option)
        action_row.addWidget(self.tools_status_label)
        action_row.addStretch()
        action_row.addWidget(self.check_updates_button)
        action_row.addWidget(self.update_all_button)
        layout.addWidget(tools_toolbar)

        self.tool_cards = {
            "yt-dlp": ToolCard("yt-dlp", "yt-dlp", "Compatibilidade com páginas, formatos e mudanças do YouTube."),
            "ffmpeg": ToolCard("ffmpeg", "FFmpeg", "Conversão, união de áudio e vídeo, recortes e metadados."),
            "deno": ToolCard("deno", "Deno", "Runtime opcional usado por extratores modernos do YouTube."),
        }
        for card in self.tool_cards.values():
            layout.addWidget(card)

        self.po_provider_card = ToolCard(
            "po-token",
            "PO Token Provider · opcional",
            "Plugin isolado para conteúdo público quando o YouTube exigir prova de origem. Nunca usa login ou cookies.",
        )
        self.po_provider_card.update_button.setText("Instalar / verificar")
        self.po_provider_card.rollback_button.setText("Desativar")
        layout.addWidget(self.po_provider_card)

        layout.addWidget(_section_header("Aplicativo", "Atualize o BraXYTDow por um manifesto HTTPS publicado pelo criador."))
        self.app_update_card = ToolCard(
            "app",
            APP_NAME,
            "Valida HTTPS, tamanho, SHA-256 e assinatura Authenticode antes de oferecer atualização ou rollback.",
        )
        self.app_update_card.rollback_button.setText("Restaurar anterior")
        self.app_update_card.rollback_button.setToolTip("Procura uma versão anterior já verificada e inicia a restauração")
        self.app_update_card.set_state(__version__, QApplication.applicationFilePath(), "Aplicativo instalado · canal independente das ferramentas")
        self.app_update_card.update_button.setText("Verificar aplicativo")
        layout.addWidget(self.app_update_card)

        log_header = QHBoxLayout()
        log_header.addWidget(_section_header("Atividade", "Mensagens das verificações e atualizações."), 1)
        self.clear_tool_log_button = _button("Limpar", "ghost", "trash")
        log_header.addWidget(self.clear_tool_log_button, 0, Qt.AlignBottom)
        layout.addLayout(log_header)
        self.tool_log = QTextEdit()
        self.tool_log.setReadOnly(True)
        self.tool_log.setPlaceholderText("Nenhuma operação executada nesta sessão.")
        self.tool_log.setMaximumHeight(150)
        self.tool_log.setAccessibleName("Registro de atualizações")
        layout.addWidget(self.tool_log)
        layout.addStretch()
        page.setWidget(content)
        return page

    def _restore_download_defaults(self) -> None:
        self._update_formats()
        saved_format = self.format.findData(self.settings.get("format", "mp4"))
        if saved_format >= 0:
            self.format.setCurrentIndex(saved_format)
        self._populate_quality_choices()
        saved_quality = self.quality.findData(self.settings.get("quality", "auto"))
        if saved_quality >= 0:
            self.quality.setCurrentIndex(saved_quality)
        self.thumbnail_option.setChecked(bool(self.settings.get("thumbnail", True)))
        self.metadata_option.setChecked(bool(self.settings.get("metadata", True)))
        self.subtitle_option.setChecked(bool(self.settings.get("subtitles", False)))
        self.split_chapters_option.setChecked(bool(self.settings.get("split_chapters", False)))
        self._load_presets()
        self._update_output_preview()

    def _load_presets(self, selected_key: str | None = None) -> None:
        builtins = (
            ("builtin:balanced", "Equilibrado · MP4 automático", {"media_type": "video", "format": "mp4", "quality": "auto", "thumbnail": True, "metadata": True}),
            ("builtin:fullhd", "Full HD · MP4 1080p", {"media_type": "video", "format": "mp4", "quality": "1080", "thumbnail": True, "metadata": True}),
            ("builtin:compact", "Compacto · MP4 720p", {"media_type": "video", "format": "mp4", "quality": "720", "thumbnail": True, "metadata": True}),
            ("builtin:audio", "Música · MP3", {"media_type": "audio", "format": "mp3", "quality": "auto", "thumbnail": True, "metadata": True, "audio_quality": "0"}),
            ("builtin:webm", "WebM · melhor qualidade", {"media_type": "video", "format": "webm", "quality": "auto", "thumbnail": True, "metadata": True}),
        )
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Personalizado", "manual")
        self.preset_combo.setItemData(0, {}, Qt.UserRole + 1)
        for key, label, values in builtins:
            self.preset_combo.addItem(label, key)
            self.preset_combo.setItemData(self.preset_combo.count() - 1, values, Qt.UserRole + 1)
        custom = self.settings.get("custom_presets", [])
        if isinstance(custom, list):
            for item in custom:
                if not isinstance(item, dict) or not isinstance(item.get("values"), dict):
                    continue
                name = str(item.get("name", "")).strip()[:48]
                if not name:
                    continue
                key = f"custom:{name.casefold()}"
                self.preset_combo.addItem(f"Meu preset · {name}", key)
                self.preset_combo.setItemData(self.preset_combo.count() - 1, item["values"], Qt.UserRole + 1)
        wanted = selected_key or str(self.settings.get("preferred_preset", "manual"))
        index = self.preset_combo.findData(wanted)
        self.preset_combo.setCurrentIndex(max(0, index))
        self.preset_combo.blockSignals(False)
        self._preset_buttons()

    def _apply_preset(self, index: int) -> None:
        values = self.preset_combo.itemData(index, Qt.UserRole + 1)
        if not isinstance(values, dict) or not values:
            self._preset_buttons()
            return
        self._applying_preset = True
        try:
            media_index = self.media_type.findData(values.get("media_type", "video"))
            self.media_type.setCurrentIndex(max(0, media_index))
            self._update_formats()
            format_index = self.format.findData(values.get("format", "mp4"))
            self.format.setCurrentIndex(max(0, format_index))
            quality_index = self.quality.findData(values.get("quality", "auto"))
            self.quality.setCurrentIndex(max(0, quality_index))
            self.thumbnail_option.setChecked(bool(values.get("thumbnail", True)))
            self.metadata_option.setChecked(bool(values.get("metadata", True)))
            self.subtitle_option.setChecked(bool(values.get("subtitles", False)))
            self.split_chapters_option.setChecked(bool(values.get("split_chapters", False)) and self.split_chapters_option.isEnabled())
            for key in self._advanced:
                if key in values:
                    self._advanced[key] = values[key]
            self.settings["preferred_preset"] = self.preset_combo.currentData()
            self.storage.save_settings(self.settings)
        finally:
            self._applying_preset = False
        self._preset_buttons()
        self._update_option_summary()

    def _current_preset_values(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type.currentData(),
            "format": self.format.currentData(),
            "quality": self.quality.currentData(),
            "thumbnail": self.thumbnail_option.isChecked(),
            "metadata": self.metadata_option.isChecked(),
            "subtitles": self.subtitle_option.isChecked(),
            "split_chapters": self.split_chapters_option.isChecked(),
            **self._advanced,
        }

    def _save_current_preset(self) -> None:
        name, accepted = QInputDialog.getText(self, "Salvar preset", "Nome do novo preset:")
        name = name.strip()[:48]
        if not accepted or not name:
            return
        custom = [item for item in self.settings.get("custom_presets", []) if isinstance(item, dict)]
        custom = [item for item in custom if str(item.get("name", "")).casefold() != name.casefold()]
        custom.append({"name": name, "values": self._current_preset_values()})
        self.settings["custom_presets"] = custom[-20:]
        key = f"custom:{name.casefold()}"
        self.settings["preferred_preset"] = key
        self.storage.save_settings(self.settings)
        self._load_presets(key)
        self.statusBar().showMessage(f"Preset “{name}” salvo", 3000)

    def _delete_current_preset(self) -> None:
        key = str(self.preset_combo.currentData() or "")
        if not key.startswith("custom:"):
            return
        name = self.preset_combo.currentText().partition("·")[2].strip()
        custom = [
            item
            for item in self.settings.get("custom_presets", [])
            if isinstance(item, dict) and str(item.get("name", "")).casefold() != name.casefold()
        ]
        self.settings["custom_presets"] = custom
        self.settings["preferred_preset"] = "manual"
        self.storage.save_settings(self.settings)
        self._load_presets("manual")

    def _preset_buttons(self) -> None:
        self.delete_preset_button.setEnabled(str(self.preset_combo.currentData() or "").startswith("custom:"))

    def _install_shortcuts(self) -> None:
        focus_url = QAction("Focar URL", self)
        focus_url.setShortcut(QKeySequence("Ctrl+L"))
        focus_url.triggered.connect(self._focus_url)
        self.addAction(focus_url)
        focus_queue = QAction("Abrir fila", self)
        focus_queue.setShortcut(QKeySequence("Ctrl+J"))
        focus_queue.triggered.connect(lambda: self._show_page(1))
        self.addAction(focus_queue)
        compact_queue = QAction("Alternar modo compacto", self)
        compact_queue.setShortcut(QKeySequence("Ctrl+Shift+C"))
        compact_queue.triggered.connect(lambda: self.compact_mode_button.toggle())
        self.addAction(compact_queue)
        for index in range(4):
            page_action = QAction(f"Abrir seção {index + 1}", self)
            page_action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            page_action.triggered.connect(lambda _checked=False, page=index: self._show_page(page))
            self.addAction(page_action)

    def _apply_accessibility(self) -> None:
        application = QApplication.instance()
        if not application:
            return
        font = QFont("Segoe UI Variable")
        font.setPointSize(11 if self.settings.get("large_text", False) else 9)
        application.setFont(font)
        contrast = ""
        if self.settings.get("high_contrast", False):
            contrast = "\nQWidget { color: #FFFFFF; } QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableWidget { border: 1px solid #6F86A6; } QPushButton:focus, QLineEdit:focus, QComboBox:focus { border: 2px solid #66C7FF; }"
        application.setStyleSheet(STYLE + contrast)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray_icon = resource_dir() / "assets" / "baixatube-icon.png"
        self.tray = QSystemTrayIcon(QIcon(str(tray_icon)), self)
        self.tray.setToolTip(f"{APP_NAME} · fila e biblioteca")
        menu = QMenu(self)
        show_action = menu.addAction(ui_icon("external", THEME["text_soft"], 17), "Mostrar BraXYTDow")
        open_file_action = menu.addAction(ui_icon("play", THEME["text_soft"], 17), "Abrir último arquivo")
        open_folder_action = menu.addAction(ui_icon("folder", THEME["text_soft"], 17), "Abrir pasta do último arquivo")
        menu.addSeparator()
        exit_action = menu.addAction(ui_icon("cancel", THEME["danger"], 17), "Sair")
        show_action.triggered.connect(self._restore_from_tray)
        open_file_action.triggered.connect(lambda: self._open_notification_target(False))
        open_folder_action.triggered.connect(lambda: self._open_notification_target(True))
        exit_action.triggered.connect(self._exit_from_tray)
        self.tray.messageClicked.connect(lambda: self._open_notification_target(False))
        self.tray.activated.connect(lambda reason: self._restore_from_tray() if reason in {QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger} else None)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_from_tray(self) -> None:
        self._force_close = True
        self.close()

    def _open_notification_target(self, folder: bool) -> None:
        if not self._last_notification_path:
            return
        path = Path(self._last_notification_path)
        target = path.parent if folder and path.suffix else path
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _notify(self, title: str, message: str, path: str = "") -> None:
        if path:
            self._last_notification_path = path
        native_started = False
        if self.settings.get("native_notifications", True):
            command = toast_command(title, message, path)
            if command:
                native_started = QProcess.startDetached(command[0], command[1])
        if self.tray and self.settings.get("native_notifications", True) and not native_started:
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 8000)
        else:
            QApplication.beep()

    def _connect(self) -> None:
        self.nav_group.idClicked.connect(self._show_page)
        if self.queue_empty.action_button:
            self.queue_empty.action_button.clicked.connect(lambda: self._show_page(0))
        self.analyze_button.clicked.connect(self._analyze)
        self.url.returnPressed.connect(self._analyze)
        self.url.textChanged.connect(self._url_changed)
        self.paste_button.clicked.connect(self._paste_url)
        self.cookie_settings_button.clicked.connect(self._open_cookie_settings)
        self.destination_browse.clicked.connect(self._browse_destination)
        self.analyzer.completed.connect(self._analyzed)
        self.analyzer.failed.connect(self._analysis_failed)
        self.analyzer.compatibility_issue.connect(self._repair_after_analysis_failure)
        self.media_type.currentIndexChanged.connect(self._update_formats)
        self.media_type.currentIndexChanged.connect(self._update_option_summary)
        self.format.currentIndexChanged.connect(self._update_option_summary)
        self.quality.currentIndexChanged.connect(self._update_option_summary)
        for option in self.extra_options:
            option.toggled.connect(self._update_option_summary)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        self.save_preset_button.clicked.connect(self._save_current_preset)
        self.delete_preset_button.clicked.connect(self._delete_current_preset)
        self.advanced_button.clicked.connect(self._advanced_options)
        self.metadata_editor_button.clicked.connect(self._metadata_editor)
        self.add_button.clicked.connect(self._add)
        self.manager.updated.connect(self._update_row)
        self.manager.queue_changed.connect(self._sync_rows)
        self.manager.compatibility_issue.connect(self._repair_after_download_failure)
        self.manager.queue_idle.connect(self._queue_idle_action)
        self.pause_button.clicked.connect(lambda: self._selected_action(self.manager.pause))
        self.resume_button.clicked.connect(self._resume_selected)
        self.cancel_button.clicked.connect(lambda: self._selected_action(self.manager.cancel))
        self.remove_button.clicked.connect(self._remove_selected)
        self.open_button.clicked.connect(self._open_selected)
        self.clear_completed_button.clicked.connect(self._clear_completed)
        self.batch_edit_button.clicked.connect(self._batch_edit_selected)
        self.queue_search.textChanged.connect(self._filter_queue)
        self.queue_filter.currentIndexChanged.connect(self._filter_queue)
        self.compact_mode_button.toggled.connect(self._toggle_compact_mode)
        self.table.itemSelectionChanged.connect(self._update_queue_actions)
        self.table.itemDoubleClicked.connect(lambda *_: self._open_selected())
        self.table.customContextMenuRequested.connect(self._queue_context_menu)
        self.table.order_changed.connect(self._queue_reordered)
        self.history_search.textChanged.connect(self._filter_history)
        self.history_filter.currentIndexChanged.connect(self._filter_history)
        self.history_format_filter.currentIndexChanged.connect(self._filter_history)
        self.history_duration_filter.currentIndexChanged.connect(self._filter_history)
        self.history_date_filter.currentIndexChanged.connect(self._filter_history)
        self.history_file_filter.currentIndexChanged.connect(self._filter_history)
        self.history_table.itemSelectionChanged.connect(self._update_history_actions)
        self.history_table.itemDoubleClicked.connect(lambda *_: self._open_history())
        self.history_open_button.clicked.connect(self._open_history)
        self.history_copy_button.clicked.connect(self._copy_history_url)
        self.history_redownload_button.clicked.connect(self._redownload_history)
        self.history_locate_button.clicked.connect(self._locate_history_file)
        self.library_cleanup_button.clicked.connect(self._library_cleanup)
        self.history_refresh_button.clicked.connect(self._load_history)
        self.settings_button.clicked.connect(self._settings)
        self.about_button.clicked.connect(lambda: AboutDialog(self).exec())
        self.auto_update_option.toggled.connect(self._save_auto_update_setting)
        self.check_updates_button.clicked.connect(self._check_tool_updates)
        self.update_all_button.clicked.connect(lambda: self._update_tools(None))
        self.health_check_button.clicked.connect(self._run_health_check)
        self.autocure_button.clicked.connect(
            lambda: self._run_autocure(
                "Verificação manual solicitada",
                tools=("yt-dlp", "ffmpeg", "deno"),
                report_failure=False,
            )
        )
        self.app_update_card.update_button.clicked.connect(self._app_update_action)
        self.app_update_card.rollback_button.clicked.connect(self._app_rollback_action)
        self.po_provider_card.update_button.clicked.connect(self._install_po_provider)
        self.po_provider_card.rollback_button.clicked.connect(self._disable_po_provider)
        self.clear_tool_log_button.clicked.connect(self.tool_log.clear)
        for tool, card in self.tool_cards.items():
            card.update_button.clicked.connect(lambda _checked=False, name=tool: self._update_tools([name]))
            card.rollback_button.clicked.connect(lambda _checked=False, name=tool: self._rollback_tool(name))

    def _set_activity(self, text: str, state: str = "idle") -> None:
        colors = {
            "idle": THEME["success"],
            "busy": THEME["cyan"],
            "success": THEME["success"],
            "warning": THEME["warning"],
            "error": THEME["danger"],
        }
        self.activity_label.setText(text)
        self.activity_dot.setStyleSheet(f"color: {colors.get(state, THEME['muted'])};")
        self.activity_pill.setProperty("state", state)
        self.activity_pill.setAccessibleName(f"Estado do aplicativo: {text}")

    def _cookie_config(self) -> CookieConfig:
        return CookieConfig.from_mapping(self.settings)

    def _sync_cookie_badge(self) -> None:
        if not hasattr(self, "cookie_strip"):
            return
        config = self._cookie_config()
        state = "idle"
        color = THEME["faint"]
        icon_name = "check"
        text = "Sessão desativada · acesso público"
        tooltip = "Downloads públicos sem usar cookies do navegador."
        if config.mode != "off":
            try:
                config.arguments()
                state = "ready"
                color = THEME["success"]
                text = f"{config.label()} · credencial local protegida"
                tooltip = "Cookies ativos para análise e downloads. O conteúdo da sessão não aparece em logs ou diagnóstico."
            except ValueError as exc:
                state = "error"
                color = THEME["danger"]
                icon_name = "warning"
                text = f"Sessão requer atenção · {exc}"
                tooltip = str(exc)
        self.cookie_state_icon.setPixmap(ui_icon(icon_name, color, 18).pixmap(18, 18))
        self.cookie_state_label.setText(text)
        self.cookie_strip.setToolTip(tooltip)
        self.cookie_strip.setProperty("state", state)
        self.cookie_strip.style().unpolish(self.cookie_strip)
        self.cookie_strip.style().polish(self.cookie_strip)

    def _set_flow_stage(self, stage: int, completed: int | None = None) -> None:
        self.flow_rail.set_stage(stage, completed=stage if completed is None else completed)

    def _url_changed(self, value: str) -> None:
        if not self.media or value.strip() == self._media_url:
            return
        self.media = None
        self._metadata_overrides = {}
        self._selected_chapters = []
        self._media_url = ""
        self.add_button.setEnabled(False)
        self.analyze_button.setText("Analisar")
        _set_badge(self.preview_badge, "Link alterado", "warning")
        self.title.setText("Analise o novo endereço para atualizar a prévia")
        self.meta.setText("Título, canal e duração aparecerão aqui")
        self.kind.setText("A análise não inicia o download.")
        self.thumbnail.setPixmap(ui_icon("play", THEME["faint"], 42).pixmap(42, 42))
        self.split_chapters_option.setEnabled(False)
        self.split_chapters_option.setChecked(False)
        self._update_output_preview()
        self._set_flow_stage(0, 0)
        self._set_activity("Pronto", "idle")

    def _show_page(self, index: int) -> None:
        if not 0 <= index < self.pages.count():
            return
        self.pages.setCurrentIndex(index, animate=self._animations_enabled)
        self.nav_buttons[index].setChecked(True)
        self.page_title.setText(PAGE_META[index][0])
        self.page_description.setText(PAGE_META[index][1])
        stage = (1 if self.media else 0) if index == 0 else (2 if index in {1, 2} else 3)
        self._set_flow_stage(stage)
        if index == 2:
            self._load_history()

    def _focus_url(self) -> None:
        self._show_page(0)
        self.url.setFocus()
        self.url.selectAll()

    def _paste_url(self) -> None:
        value = QApplication.clipboard().text().strip()
        if value:
            self.url.setText(value)
            self.url.setFocus()

    def _browse_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Escolher pasta de destino", self.destination.text())
        if selected:
            self.destination.setText(selected)

    def _update_formats(self, *_args: Any) -> None:
        current = self.format.currentData()
        self.format.blockSignals(True)
        self.format.clear()
        values = (("MP4 · compatível", "mp4"), ("WebM · aberto", "webm")) if self.media_type.currentData() == "video" else (("MP3", "mp3"), ("M4A", "m4a"), ("Opus", "opus"), ("WAV · sem compressão", "wav"))
        for label, value in values:
            self.format.addItem(label, value)
        index = self.format.findData(current)
        self.format.setCurrentIndex(index if index >= 0 else 0)
        self.format.blockSignals(False)
        self.quality.setEnabled(self.media_type.currentData() == "video")
        self._update_option_summary()

    def _populate_quality_choices(self, media: MediaInfo | None = None) -> None:
        current = self.quality.currentData() if self.quality.count() else self.settings.get("quality", "auto")
        heights = sorted({item.height for item in (media.formats if media else []) if item.height}, reverse=True)
        if not heights:
            heights = [2160, 1440, 1080, 720, 480, 360, 240]
        self.quality.blockSignals(True)
        self.quality.clear()
        self.quality.addItem("Melhor disponível", "auto")
        for height in heights:
            self.quality.addItem(f"Até {height}p", str(height))
        index = self.quality.findData(current)
        self.quality.setCurrentIndex(index if index >= 0 else 0)
        self.quality.blockSignals(False)

    def _update_option_summary(self, *_args: Any) -> None:
        if not hasattr(self, "option_summary"):
            return
        if hasattr(self, "preset_combo") and not self._applying_preset and self.sender() in {
            self.media_type,
            self.format,
            self.quality,
            *self.extra_options,
        }:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(0)
            self.preset_combo.blockSignals(False)
            self._preset_buttons()
        kind = "vídeo" if self.media_type.currentData() == "video" else "áudio"
        quality = self.quality.currentText() if self.media_type.currentData() == "video" else "qualidade de áudio configurada"
        self.option_summary.setText(f"{kind.capitalize()} · {self.format.currentText()} · {quality}")
        self._update_output_preview()

    def _update_output_preview(self) -> None:
        if not hasattr(self, "output_preview"):
            return
        if not self.media:
            self.output_preview.setText("Analise um link para estimar o arquivo de saída")
            return
        extension = str(self.format.currentData() or "mp4")
        estimate = estimate_output_bytes(
            self.media,
            str(self.media_type.currentData() or "video"),
            extension,
            str(self.quality.currentData() or "auto"),
        )
        if self.media.is_playlist:
            self.output_preview.setText(f"Saída: {len(self.media.entries)} itens · tamanho calculado durante os downloads")
            return
        filename = preview_output_name(
            self.media.title,
            self.media.id,
            extension,
            str(self._advanced.get("filename_template", "")),
        )
        custom_name = self._metadata_overrides.get("custom_filename", "").strip()
        if custom_name:
            from .command_builder import normalize_custom_filename

            filename = f"{normalize_custom_filename(custom_name)}.{extension}"
        chapter_note = f" · {len(self.media.chapters)} capítulos" if self.split_chapters_option.isChecked() and self.media.chapters else ""
        self.output_preview.setText(f"Prévia: {filename} · estimativa ≈ {format_bytes(estimate)}{chapter_note}")

    def _advanced_options(self) -> None:
        dialog = AdvancedOptionsDialog(self._advanced, self)
        if dialog.exec() == QDialog.Accepted:
            self._advanced.update(dialog.values())
            self.settings.update(self._advanced)
            self.storage.save_settings(self.settings)
            self._update_output_preview()
            self.statusBar().showMessage("Opções avançadas salvas", 3000)

    def _metadata_editor(self) -> None:
        if not self.media:
            QMessageBox.information(self, "Nome e capa", "Analise um vídeo antes de personalizar o arquivo.")
            return
        dialog = MetadataEditorDialog(self.media, self._metadata_overrides, self)
        if dialog.exec() == QDialog.Accepted:
            self._metadata_overrides = dialog.values()
            self._update_output_preview()
            self.statusBar().showMessage("Nome e metadados personalizados", 3000)

    def _analyze(self) -> None:
        if self._tool_bootstrap_pending:
            self.statusBar().showMessage("Aguarde a preparação inicial das ferramentas…", 3000)
            return
        value = self.url.text().strip()
        if not self._automatic_analysis_retry:
            self._analysis_repair_attempted_url = ""
        self._automatic_analysis_retry = False
        if not is_supported_url(value):
            QMessageBox.warning(self, "URL inválida", "Informe uma URL válida do YouTube.")
            self.url.setFocus()
            return
        cookie = self._cookie_config()
        try:
            cookie.arguments()
        except ValueError as exc:
            QMessageBox.warning(self, "Sessão requer atenção", str(exc))
            self._open_cookie_settings()
            return
        self.media = None
        self._media_url = ""
        self.add_button.setEnabled(False)
        self.analyze_button.setEnabled(False)
        self.analyze_button.setText("Analisando…")
        self.analyze_button.setProperty("busy", True)
        self.analyze_button.style().unpolish(self.analyze_button)
        self.analyze_button.style().polish(self.analyze_button)
        _set_badge(self.preview_badge, "Analisando", "loading")
        access_label = "com sua sessão local" if cookie.enabled else "em acesso público"
        self.title.setText(f"Consultando informações {access_label}…")
        self.meta.setText("Isso costuma levar apenas alguns segundos. Nenhuma senha é lida pelo aplicativo.")
        self.kind.setText("")
        self._set_activity("Analisando", "busy")
        self._set_flow_stage(0, 0)
        self.statusBar().showMessage(f"Consultando informações {access_label}…")
        self.analyzer.analyze(value)

    def _analyzed(self, media: MediaInfo) -> None:
        self._analysis_repair_attempted_url = ""
        self.media = media
        self._media_url = self.url.text().strip()
        self.analyze_button.setEnabled(True)
        self.analyze_button.setText("Analisar novamente")
        self.analyze_button.setProperty("busy", False)
        self.analyze_button.style().unpolish(self.analyze_button)
        self.analyze_button.style().polish(self.analyze_button)
        self.add_button.setEnabled(True)
        _set_badge(self.preview_badge, "Pronto para adicionar", "success")
        self.title.setText(media.title)
        self.meta.setText(f"{media.uploader or 'Canal desconhecido'}   ·   {format_duration(media.duration)}")
        if media.is_playlist:
            self.kind.setText(f"Playlist · {len(media.entries)} itens · você poderá escolher os vídeos")
            self.split_chapters_option.setEnabled(False)
            self.split_chapters_option.setChecked(False)
        else:
            subtitle_note = f" · {len(media.subtitles)} idiomas de legenda" if media.subtitles else ""
            chapter_note = f" · {len(media.chapters)} capítulos" if media.chapters else ""
            self.kind.setText(f"Vídeo · {len(media.formats)} formatos detectados{subtitle_note}{chapter_note}")
            self.split_chapters_option.setEnabled(bool(media.chapters))
            if not media.chapters:
                self.split_chapters_option.setChecked(False)
            self._populate_quality_choices(media)
        self._update_output_preview()
        self._set_activity("Análise concluída", "success")
        self._set_flow_stage(1, 1)
        self.statusBar().showMessage("Análise concluída", 4000)
        if media.thumbnail:
            reply = self.network.get(QNetworkRequest(QUrl(media.thumbnail)))
            reply.finished.connect(lambda current=reply, url=self._media_url: self._thumbnail_ready(current, url))

    def _thumbnail_ready(self, reply: QNetworkReply, expected_url: str) -> None:
        if expected_url == self._media_url and reply.error() == QNetworkReply.NoError:
            pixmap = QPixmap()
            if pixmap.loadFromData(reply.readAll()):
                self._thumbnail_source = pixmap
                self.thumbnail.setPixmap(_rounded_cover(pixmap, self.thumbnail.size()))
        reply.deleteLater()

    def _analysis_failed(self, message: str) -> None:
        self.analyze_button.setEnabled(True)
        self.analyze_button.setText("Tentar novamente")
        self.analyze_button.setProperty("busy", False)
        self.analyze_button.style().unpolish(self.analyze_button)
        self.analyze_button.style().polish(self.analyze_button)
        _set_badge(self.preview_badge, "Falha na análise", "error")
        self.title.setText("Não foi possível analisar este link")
        self.meta.setText(message)
        self.kind.setText("Verifique o link e, em Ferramentas, procure atualizações do yt-dlp.")
        self._set_activity("Atenção necessária", "error")
        self._set_flow_stage(0, 0)
        self.statusBar().showMessage("Falha na análise")
        if not self._pending_analysis_repair:
            QMessageBox.critical(self, "Falha na análise", message)

    def _add(self) -> None:
        if not self.media:
            return
        destination = self.destination.text().strip()
        if not destination:
            QMessageBox.warning(self, "Pasta necessária", "Escolha a pasta em que os arquivos serão salvos.")
            return
        if not self.media.is_playlist:
            queued = next(
                (
                    request
                    for request in self.manager.requests.values()
                    if (self.media.id and request.media_id == self.media.id) or request.url == self._media_url
                ),
                None,
            )
            duplicate = queued or self.storage.completed_duplicate(self.media.id, self._media_url)
            if duplicate:
                location = getattr(duplicate, "output_path", "") or getattr(duplicate, "destination", "")
                message = "Este vídeo já está na fila."
                if not queued:
                    message = "Este vídeo já foi concluído anteriormente."
                if location:
                    message += f"\n\nLocal: {location}"
                message += "\n\nDeseja adicionar outra cópia mesmo assim?"
                if QMessageBox.question(self, "Download duplicado", message) != QMessageBox.Yes:
                    return
        items: list[str] = []
        playlist_media_ids: list[str] = []
        if self.media.is_playlist:
            archived = self.storage.archived_media_ids([entry.id for entry in self.media.entries], self.media.playlist_id)
            dialog = PlaylistDialog(self.media, archived, self)
            if dialog.exec() != QDialog.Accepted:
                return
            items = dialog.selected()
            playlist_media_ids = dialog.selected_media_ids()
            if not items:
                QMessageBox.information(self, "Playlist", "Selecione pelo menos um item.")
                return
        selected_chapters: list[dict[str, Any]] = []
        split_all_chapters = False
        if self.split_chapters_option.isChecked() and self.media.chapters:
            chapter_dialog = ChapterDialog(self.media, self)
            if chapter_dialog.exec() != QDialog.Accepted:
                return
            selected_chapters = chapter_dialog.selected()
            if not selected_chapters:
                QMessageBox.information(self, "Capítulos", "Selecione pelo menos um capítulo.")
                return
            split_all_chapters = len(selected_chapters) == len(self.media.chapters)
            if split_all_chapters:
                selected_chapters = []
        advanced = dict(self._advanced)
        if not str(advanced.get("rate_limit", "")).strip():
            advanced["rate_limit"] = scheduled_rate_limit(self.settings)
        cookie = self._cookie_config()
        try:
            cookie.arguments()
        except ValueError as exc:
            QMessageBox.warning(self, "Sessão requer atenção", str(exc))
            self._open_cookie_settings()
            return
        request = DownloadRequest(
            id=str(uuid.uuid4()),
            url=self.url.text().strip(),
            title=self.media.title,
            destination=destination,
            media_type=self.media_type.currentData(),
            output_format=self.format.currentData(),
            quality=self.quality.currentData(),
            embed_thumbnail=self.thumbnail_option.isChecked(),
            embed_metadata=self.metadata_option.isChecked(),
            download_subtitles=self.subtitle_option.isChecked(),
            playlist_items=items,
            playlist_media_ids=playlist_media_ids,
            media_id=self.media.id,
            split_chapters=split_all_chapters,
            selected_chapters=selected_chapters,
            pause_on_metered=bool(self.settings.get("pause_on_metered", False)),
            po_token_provider=bool(self.settings.get("po_token_provider", False)),
            cookie_mode=cookie.mode,
            cookie_browser=cookie.browser,
            cookie_profile=cookie.profile,
            cookie_file=cookie.file_path,
            cookie_consent=cookie.consent,
            uploader=self.media.uploader,
            duration=self.media.duration,
            upload_date=self.media.upload_date,
            playlist_id=self.media.playlist_id,
            playlist_title=self.media.playlist_title,
            created_at=datetime.now(timezone.utc).isoformat(),
            **self._metadata_overrides,
            **advanced,
        )
        self.settings.update(
            {
                "destination": destination,
                "format": request.output_format,
                "quality": request.quality,
                "thumbnail": request.embed_thumbnail,
                "metadata": request.embed_metadata,
                "subtitles": request.download_subtitles,
                "split_chapters": bool(request.split_chapters or request.selected_chapters),
                **self._advanced,
            }
        )
        self.storage.save_settings(self.settings)
        self._queue_completion_handled = False
        self.manager.add(request)
        self._show_page(1)
        self._set_activity("Download adicionado", "success")
        self._set_flow_stage(2, 2)
        self.statusBar().showMessage("Adicionado à fila", 3000)

    def _sync_rows(self) -> None:
        stale = sorted(((row, request_id) for request_id, row in self.rows.items() if request_id not in self.manager.requests), reverse=True)
        for row, request_id in stale:
            self.table.removeRow(row)
            self.rows.pop(request_id, None)
            self.queue_progress.pop(request_id, None)
            self.output_paths.pop(request_id, None)
        if stale:
            self._reindex_rows()
        for request_id, request in self.manager.requests.items():
            if request_id in self.rows:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.rows[request_id] = row
            title = QTableWidgetItem(request.title)
            title.setData(Qt.UserRole, request_id)
            title.setToolTip(request.url)
            self.table.setItem(row, 0, title)
            status_item = QTableWidgetItem(STATUS_LABELS[DownloadStatus.WAITING])
            status_item.setData(Qt.UserRole, DownloadStatus.WAITING.value)
            status_item.setForeground(STATUS_COLORS[DownloadStatus.WAITING])
            status_item.setIcon(ui_icon(STATUS_ICONS[DownloadStatus.WAITING], STATUS_COLORS[DownloadStatus.WAITING].name(), 16))
            self.table.setItem(row, 1, status_item)
            bar = AnimatedProgressBar(animation_duration=180)
            bar.set_animations_enabled(self._animations_enabled)
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setFormat("%p%")
            self.table.setCellWidget(row, 2, bar)
            priority = {2: "Alta", 1: "Normal", 0: "Baixa"}.get(int(request.priority), "Normal")
            self.table.setItem(row, 3, QTableWidgetItem(f"{request.output_format.upper()} · {priority}"))
            for column in (4, 5, 6):
                self.table.setItem(row, column, QTableWidgetItem("—"))
            if request.scheduled_at:
                self.table.item(row, 6).setText(f"Agendado · {request.scheduled_at.replace('T', ' ')[:16]}")
        self._filter_queue()
        self._refresh_queue_summary()

    def _reindex_rows(self) -> None:
        self.rows.clear()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                self.rows[item.data(Qt.UserRole)] = row

    def _update_row(self, request_id: str, progress: DownloadProgress) -> None:
        self._sync_rows()
        row = self.rows.get(request_id)
        if row is None:
            return
        self.queue_progress[request_id] = progress
        status_item = self.table.item(row, 1)
        status_item.setText(STATUS_LABELS[progress.status])
        status_item.setData(Qt.UserRole, progress.status.value)
        status_item.setForeground(STATUS_COLORS[progress.status])
        status_item.setIcon(ui_icon(STATUS_ICONS[progress.status], STATUS_COLORS[progress.status].name(), 16))
        bar = self.table.cellWidget(row, 2)
        if isinstance(bar, QProgressBar):
            if progress.status in {DownloadStatus.ANALYZING, DownloadStatus.CONVERTING} and progress.percent <= 0:
                bar.setRange(0, 0)
            else:
                bar.setRange(0, 1000)
                if isinstance(bar, AnimatedProgressBar):
                    bar.setValue(round(progress.percent * 10), animate=self._animations_enabled)
                else:
                    bar.setValue(round(progress.percent * 10))
            if progress.status == DownloadStatus.COMPLETED:
                bar.setRange(0, 1000)
                if isinstance(bar, AnimatedProgressBar):
                    bar.setValue(1000, animate=self._animations_enabled)
                else:
                    bar.setValue(1000)
        self.table.item(row, 4).setText(progress.speed or "—")
        self.table.item(row, 5).setText(progress.eta or "—")
        detail = progress.message or (f"{progress.downloaded} / {progress.total}" if progress.downloaded else "—")
        request = self.manager.requests.get(request_id)
        if request:
            priority = {2: "Alta", 1: "Normal", 0: "Baixa"}.get(int(request.priority), "Normal")
            self.table.item(row, 3).setText(f"{request.output_format.upper()} · {priority}")
            if detail == "—" and progress.status == DownloadStatus.WAITING and request.scheduled_at:
                detail = f"Agendado · {request.scheduled_at.replace('T', ' ')[:16]}"
        self.table.item(row, 6).setText(detail)
        self.table.item(row, 6).setToolTip(detail)
        if progress.status in {DownloadStatus.DOWNLOADING, DownloadStatus.CONVERTING}:
            self.footer_speed_label.setText(progress.speed or "0 B/s")
            label = "Convertendo" if progress.status == DownloadStatus.CONVERTING else "Baixando"
            self._set_activity(label, "busy")
            self._set_flow_stage(2, 2)
        elif progress.status == DownloadStatus.ERROR:
            self._set_activity("Atenção necessária", "error")
        elif progress.status == DownloadStatus.CANCELLED:
            self._set_activity("Download cancelado", "warning")
        if progress.status == DownloadStatus.COMPLETED:
            self._download_repair_attempted.discard(request_id)
            self.output_paths[request_id] = progress.message
            self._load_history()
            request = self.manager.requests.get(request_id)
            self._notify("Download concluído", request.title if request else "Arquivo pronto", progress.message)
            if self.settings.get("open_after") and progress.message:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(progress.message).parent)))
        self._filter_queue()
        self._refresh_queue_summary()
        self._update_queue_actions()

    def _filter_queue(self, *_args: Any) -> None:
        query = self.queue_search.text().strip().casefold()
        wanted = self.queue_filter.currentData()
        visible = 0
        for row in range(self.table.rowCount()):
            text = " ".join(self.table.item(row, column).text() for column in (0, 1, 3, 6) if self.table.item(row, column)).casefold()
            status = self.table.item(row, 1).data(Qt.UserRole) if self.table.item(row, 1) else ""
            show = (not query or query in text) and (not wanted or wanted == status)
            self.table.setRowHidden(row, not show)
            visible += int(show)
        if self.table.rowCount() == 0:
            self.queue_empty.set_message("Sua fila está vazia", "Analise um link na página Baixar e adicione o conteúdo aqui.")
            if self.queue_empty.action_button:
                self.queue_empty.action_button.show()
            self.queue_stack.setCurrentWidget(self.queue_empty)
        elif visible == 0:
            self.queue_empty.set_message("Nenhum resultado", "Ajuste a pesquisa ou o filtro de estado.")
            if self.queue_empty.action_button:
                self.queue_empty.action_button.hide()
            self.queue_stack.setCurrentWidget(self.queue_empty)
        else:
            self.queue_stack.setCurrentWidget(self.table)

    def _refresh_queue_summary(self) -> None:
        total = len(self.manager.requests)
        active = sum(status in {DownloadStatus.DOWNLOADING, DownloadStatus.CONVERTING, DownloadStatus.ANALYZING} for status in self.manager.statuses.values())
        waiting = sum(status == DownloadStatus.WAITING for status in self.manager.statuses.values())
        self.queue_count.setText("Fila vazia" if not total else f"{total} itens · {active} ativos · {waiting} aguardando")
        self.footer_queue_label.setText("0 na fila" if not total else f"{total} na fila")
        if not active:
            self.footer_speed_label.setText("0 B/s")
        statuses = tuple(self.manager.statuses.values())
        if statuses and all(status == DownloadStatus.COMPLETED for status in statuses):
            self.flow_rail.complete()
            self._set_activity("Tudo concluído", "success")
        self.clear_completed_button.setEnabled(any(status == DownloadStatus.COMPLETED for status in self.manager.statuses.values()))
        label = "Fila" if not total else f"Fila ({total})"
        self.nav_buttons[1].setProperty("label", label)
        self._set_nav_text(self._compact_navigation)

    def _queue_idle_action(self) -> None:
        if self._queue_completion_handled or not self.manager.statuses:
            return
        if not all(status == DownloadStatus.COMPLETED for status in self.manager.statuses.values()):
            return
        self._queue_completion_handled = True
        action = str(self.settings.get("completion_action", "notify"))
        self._notify("Fila concluída", "Todos os arquivos foram processados. Use a bandeja para abrir o último resultado.", self._last_notification_path)
        self.statusBar().showMessage("Toda a fila foi concluída", 8000)
        if action == "open_folder":
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.get("destination", Path.home() / "Downloads"))))
        elif action in {"sleep", "shutdown"}:
            verb = "suspender" if action == "sleep" else "desligar"
            detail = "O desligamento terá uma espera de 60 segundos e poderá ser cancelado com “shutdown /a”." if action == "shutdown" else "Downloads e conversões já foram encerrados."
            if QMessageBox.question(
                self,
                f"{verb.capitalize()} o computador",
                f"A fila terminou. Deseja {verb} o computador agora?\n\n{detail}",
            ) != QMessageBox.Yes:
                return
            if action == "shutdown":
                QProcess.startDetached("shutdown.exe", ["/s", "/t", "60", "/c", "Fila do BraXYTDow concluída"])
            else:
                QProcess.startDetached("rundll32.exe", ["powrprof.dll,SetSuspendState", "0,1,0"])

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _selected_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        result = []
        for row in rows:
            item = self.table.item(row, 0)
            if item and item.data(Qt.UserRole):
                result.append(str(item.data(Qt.UserRole)))
        return result

    def _selected_action(self, action: Any) -> None:
        for request_id in self._selected_ids():
            action(request_id)

    def _resume_selected(self) -> None:
        for request_id in self._selected_ids():
            self._download_repair_attempted.discard(request_id)
            self.manager.resume(request_id)

    def _remove_selected(self) -> None:
        for request_id in self._selected_ids():
            self._download_repair_attempted.discard(request_id)
            self.manager.remove(request_id)

    def _update_queue_actions(self) -> None:
        selected = self._selected_ids()
        request_id = selected[0] if selected else None
        statuses = [self.manager.statuses.get(key) for key in selected]
        self.pause_button.setEnabled(any(status in {DownloadStatus.DOWNLOADING, DownloadStatus.CONVERTING} and key not in self.manager.paused for key, status in zip(selected, statuses)))
        self.resume_button.setEnabled(any(key in self.manager.paused or status in {DownloadStatus.ERROR, DownloadStatus.CANCELLED} for key, status in zip(selected, statuses)))
        self.cancel_button.setEnabled(any(status in {DownloadStatus.WAITING, DownloadStatus.DOWNLOADING, DownloadStatus.CONVERTING} for status in statuses))
        self.remove_button.setEnabled(bool(selected))
        self.batch_edit_button.setEnabled(bool(selected) and all(key not in self.manager.jobs for key in selected))
        self.open_button.setEnabled(len(selected) == 1)
        if request_id and self.output_paths.get(request_id):
            self.open_button.setText("Abrir arquivo")
        else:
            self.open_button.setText("Abrir pasta")

    def _batch_edit_selected(self) -> None:
        selected = self._selected_ids()
        if not selected:
            return
        dialog = BatchEditDialog(len(selected), self)
        if dialog.exec() == QDialog.Accepted:
            changes = dialog.changes()
            if changes:
                self.manager.update_requests(selected, **changes)

    def _queue_reordered(self, order: object) -> None:
        if not isinstance(order, list):
            return
        self._reindex_rows()
        self.manager.reorder([str(item) for item in order])

    def _open_selected(self) -> None:
        request_id = self._selected_id()
        if not request_id:
            return
        path = self.output_paths.get(request_id, "")
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif request_id in self.manager.requests:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.manager.requests[request_id].destination))

    def _queue_context_menu(self, position: Any) -> None:
        if not self._selected_id():
            return
        self._update_queue_actions()
        menu = QMenu(self)
        for button in (self.open_button, self.pause_button, self.resume_button, self.cancel_button, self.remove_button):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(button.click)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _clear_completed(self) -> None:
        completed = [request_id for request_id, status in self.manager.statuses.items() if status == DownloadStatus.COMPLETED]
        if not completed:
            return
        if QMessageBox.question(self, "Limpar concluídos", f"Remover {len(completed)} itens concluídos da fila? Os arquivos baixados não serão apagados.") != QMessageBox.Yes:
            return
        for request_id in completed:
            self.manager.remove(request_id)

    def _load_history(self) -> None:
        self.storage.reconcile_library_files()
        self.history_entries = self.storage.history()
        self.history_table.setRowCount(len(self.history_entries))
        for row, entry in enumerate(self.history_entries):
            title = QTableWidgetItem(entry.title)
            title.setData(Qt.UserRole, entry)
            title.setToolTip(entry.url)
            channel = QTableWidgetItem(entry.uploader or "—")
            format_item = QTableWidgetItem(entry.output_format.upper() or "—")
            duration = QTableWidgetItem(format_duration(entry.duration))
            status = QTableWidgetItem(entry.status.capitalize())
            status_enum = _status_from_value(entry.status)
            if status_enum:
                status.setForeground(STATUS_COLORS[status_enum])
                status.setText(STATUS_LABELS[status_enum])
                status.setIcon(ui_icon(STATUS_ICONS[status_enum], STATUS_COLORS[status_enum].name(), 16))
            destination = QTableWidgetItem(entry.output_path or "—")
            destination.setToolTip(entry.output_path or "Nenhum arquivo gerado")
            if entry.file_state == "missing" and entry.output_path:
                destination.setText("Não encontrado · " + Path(entry.output_path).name)
                destination.setForeground(QColor(THEME["warning"]))
            date = QTableWidgetItem(entry.created_at.replace("T", " ")[:19])
            date.setToolTip(entry.created_at)
            values = (title, channel, format_item, duration, status, date, destination)
            for column, value in enumerate(values):
                self.history_table.setItem(row, column, value)
        self._filter_history()
        self._update_history_actions()

    def _filter_history(self, *_args: Any) -> None:
        query = self.history_search.text().strip().casefold()
        wanted = self.history_filter.currentData()
        wanted_format = self.history_format_filter.currentData()
        wanted_duration = self.history_duration_filter.currentData()
        wanted_file = self.history_file_filter.currentData()
        days = int(self.history_date_filter.currentData() or 0)
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400 if days else 0
        visible = 0
        for row in range(self.history_table.rowCount()):
            entry = self.history_table.item(row, 0).data(Qt.UserRole)
            haystack = f"{entry.title} {entry.uploader} {entry.url} {entry.output_path}".casefold()
            status = _status_from_value(entry.status)
            duration = int(entry.duration or 0)
            duration_ok = (
                not wanted_duration
                or wanted_duration == "short" and duration <= 300
                or wanted_duration == "medium" and 300 < duration <= 1200
                or wanted_duration == "long" and duration > 1200
            )
            try:
                created = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                created = 0
            show = (
                (not query or query in haystack)
                and (not wanted or bool(status and wanted == status.value))
                and (not wanted_format or entry.output_format == wanted_format)
                and duration_ok
                and (not wanted_file or entry.file_state == wanted_file)
                and (not cutoff or created >= cutoff)
            )
            self.history_table.setRowHidden(row, not show)
            visible += int(show)
        self.history_count.setText(f"{visible} de {len(self.history_entries)}")
        if not self.history_entries:
            self.history_empty.set_message("Sua biblioteca está vazia", "Downloads concluídos e falhas aparecerão aqui.")
            self.history_stack.setCurrentWidget(self.history_empty)
        elif not visible:
            self.history_empty.set_message("Nenhum resultado", "Ajuste a pesquisa ou o filtro de status.")
            self.history_stack.setCurrentWidget(self.history_empty)
        else:
            self.history_stack.setCurrentWidget(self.history_table)

    def _selected_history(self) -> HistoryEntry | None:
        row = self.history_table.currentRow()
        item = self.history_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _update_history_actions(self) -> None:
        entry = self._selected_history()
        self.history_copy_button.setEnabled(entry is not None)
        self.history_open_button.setEnabled(bool(entry and entry.output_path))
        self.history_redownload_button.setEnabled(entry is not None)
        self.history_locate_button.setEnabled(bool(entry and entry.output_path))

    def _open_history(self) -> None:
        entry = self._selected_history()
        if not entry or not entry.output_path:
            return
        path = Path(entry.output_path)
        target = path if path.exists() else path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _copy_history_url(self) -> None:
        entry = self._selected_history()
        if entry:
            QApplication.clipboard().setText(entry.url)
            self.statusBar().showMessage("Link copiado", 2500)

    def _redownload_history(self) -> None:
        entry = self._selected_history()
        if not entry:
            return
        self.url.setText(entry.url)
        self._show_page(0)
        self._analyze()

    def _locate_history_file(self) -> None:
        entry = self._selected_history()
        if not entry:
            return
        selected, _filter = QFileDialog.getOpenFileName(self, "Localizar arquivo movido", str(Path(entry.output_path).parent))
        if selected and self.storage.relocate_history(entry.request_id, selected):
            self._load_history()
            self.statusBar().showMessage("Arquivo associado novamente à biblioteca", 3500)

    def _library_cleanup(self) -> None:
        roots = {Path(str(self.settings.get("destination", Path.home() / "Downloads")))}
        roots.update(Path(entry.output_path).parent for entry in self.history_entries if entry.output_path)
        partials = scan_partial_files(sorted(roots, key=str))
        duplicates: list[CleanupCandidate] = []
        for group in self.storage.duplicate_library_groups():
            for entry in group[1:]:
                path = Path(entry.output_path)
                if path.is_file():
                    duplicates.append(CleanupCandidate(str(path), path.stat().st_size, "duplicate", f"Cópia antiga de {entry.media_id}"))
        candidates = partials + duplicates
        if not candidates:
            QMessageBox.information(self, "Limpeza segura", "Nenhum arquivo parcial antigo ou cópia duplicada foi encontrado.")
            return
        total = sum(item.size for item in candidates)
        if QMessageBox.warning(
            self,
            "Confirmar limpeza segura",
            f"Foram encontrados {len(partials)} arquivos parciais antigos e {len(duplicates)} cópias duplicadas, totalizando {format_bytes(total)}.\n\n"
            "Somente os caminhos listados dentro das pastas da biblioteca serão removidos. Deseja continuar?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        removed, bytes_removed = remove_candidates(candidates, sorted(roots, key=str))
        self.storage.reconcile_library_files()
        self._load_history()
        QMessageBox.information(self, "Limpeza concluída", f"{removed} arquivos removidos com segurança · {format_bytes(bytes_removed)} liberados.")

    def _settings(self) -> None:
        self._run_settings_dialog()

    def _open_cookie_settings(self) -> None:
        self._run_settings_dialog("session")

    def _run_settings_dialog(self, initial_tab: str = "") -> None:
        dialog = SettingsDialog(self.settings, self, initial_tab=initial_tab)
        if dialog.exec() == QDialog.Accepted:
            previous_app_channel = (
                str(self.settings.get("app_update_manifest_url", "")),
                str(self.settings.get("app_update_channel", "stable")),
            )
            self.settings.update(dialog.values())
            self.storage.save_settings(self.settings)
            self.destination.setText(str(self.settings["destination"]))
            self.auto_update_option.setChecked(bool(self.settings.get("auto_tool_updates", self.settings.get("auto_update_tools", True))))
            self.manager.concurrency = int(self.settings["concurrency"])
            self.manager.settings = self.settings
            self.analyzer.po_token_provider = bool(self.settings.get("po_token_provider", False))
            self.analyzer.cookie_config = CookieConfig.from_mapping(self.settings)
            self._sync_cookie_badge()
            set_remote_ejs_enabled(bool(self.settings.get("remote_ejs_fallback", True)))
            self._animations_enabled = bool(self.settings.get("interface_animations", True))
            for stack in (self.pages, self.queue_stack, self.history_stack):
                stack.set_animations_enabled(self._animations_enabled)
            self.flow_rail.set_animations_enabled(self._animations_enabled)
            for card in self.tool_cards.values():
                card.progress.set_animations_enabled(self._animations_enabled)
            for row in range(self.table.rowCount()):
                progress = self.table.cellWidget(row, 2)
                if isinstance(progress, AnimatedProgressBar):
                    progress.set_animations_enabled(self._animations_enabled)
            self.manager.pump()
            if self.tool_updater and hasattr(self.tool_updater.updater.provider, "channel"):
                self.tool_updater.updater.provider.channel = self._selected_update_channel()
            self._apply_accessibility()
            current_app_channel = (
                str(self.settings.get("app_update_manifest_url", "")),
                str(self.settings.get("app_update_channel", "stable")),
            )
            if previous_app_channel != current_app_channel:
                self._setup_app_updates()
            self.statusBar().showMessage("Configurações salvas", 3000)

    def _selected_update_channel(self):
        raw = str(self.settings.get("update_channel", "nightly"))
        try:
            return self._UpdateChannel(raw)
        except ValueError:
            self.settings["update_channel"] = "nightly"
            return self._UpdateChannel.NIGHTLY

    def _setup_tool_updates(self) -> None:
        try:
            from .tool_updates import ToolUpdateManager, ToolUpdater, UpdateChannel, UpdateMode, UpdatePreferences
        except (ImportError, AttributeError) as exc:
            self.tools_status_label.setText("Atualizador não disponível nesta compilação")
            self._append_tool_log(f"Atualizador indisponível: {exc}")
            self.check_updates_button.setEnabled(False)
            self.update_all_button.setEnabled(False)
            return
        self._UpdateMode = UpdateMode
        self._UpdatePreferences = UpdatePreferences
        self._UpdateChannel = UpdateChannel
        channel = self._selected_update_channel()
        self.tool_updater = ToolUpdateManager(ToolUpdater(channel=channel), parent=self)
        self.tool_updater.busy_changed.connect(self._tool_busy_changed)
        self.tool_updater.started.connect(lambda text: self._append_tool_log(str(text)))
        self.tool_updater.progress.connect(self._tool_progress)
        self.tool_updater.tool_checked.connect(self._tool_checked)
        self.tool_updater.tool_updated.connect(self._tool_updated)
        self.tool_updater.health_checked.connect(self._health_checked)
        self.tool_updater.rollback_finished.connect(self._rollback_finished)
        self.tool_updater.autocure_finished.connect(self._autocure_finished)
        self.tool_updater.finished.connect(self._tool_finished)
        self.tool_updater.failed.connect(self._tool_failed)
        self.analyze_button.setEnabled(False)
        self.tools_status_label.setText("Preparando ferramentas…")
        self._append_tool_log("Preparando as ferramentas locais em segundo plano…")
        self._tool_bootstrap_pending = self.tool_updater.bootstrap_async()
        if not self._tool_bootstrap_pending:
            self.analyze_button.setEnabled(True)
            self._tool_failed("Não foi possível iniciar a preparação das ferramentas.")

    def _setup_app_updates(self) -> None:
        if self.app_updater:
            self.app_updater.deleteLater()
        manifest_url = str(self.settings.get("app_update_manifest_url", "")).strip()
        self.app_updater = AppUpdateManager(
            AppUpdater(
                manifest_url,
                channel=str(self.settings.get("app_update_channel", "stable")),
                device_id=str(self.settings.get("installation_id", "")),
            ),
            parent=self,
        )
        self.app_updater.checked.connect(self._app_update_checked)
        self.app_updater.downloaded.connect(self._app_update_downloaded)
        self.app_updater.rollback_ready.connect(self._app_rollback_ready)
        self.app_updater.progress.connect(lambda value: self.app_update_card.set_progress(value, "Baixando instalador verificado…"))
        self.app_updater.busy_changed.connect(self._app_update_busy)
        self.app_updater.failed.connect(lambda message: self._append_tool_log(f"Atualização do aplicativo: {message}"))
        if manifest_url:
            channel = str(self.settings.get("app_update_channel", "stable"))
            self.app_update_card.path.setText(f"Canal HTTPS {channel} · integridade e assinatura obrigatórias")
            QTimer.singleShot(4200, self.app_updater.check_async)
        else:
            self.app_update_card.path.setText("Canal ainda não publicado · configure o manifesto HTTPS nas preferências")

    def _setup_po_plugin(self) -> None:
        manager = PoTokenPluginManager()
        self.po_plugin_controller = PoTokenPluginController(manager, self) if PoTokenPluginController else None
        state = manager.state()
        self._render_po_provider(state)
        if self.po_plugin_controller:
            self.po_plugin_controller.finished.connect(self._po_provider_finished)
            self.po_plugin_controller.failed.connect(self._po_provider_failed)
            self.po_plugin_controller.busy_changed.connect(lambda busy: self.po_provider_card.update_button.setEnabled(not busy))

    def _render_po_provider(self, state: PluginState) -> None:
        version = state.version or "Desativado"
        path = state.path or "Nenhum plugin executável carregado no aplicativo"
        self.po_provider_card.set_state(version, path, state.message)
        self.po_provider_card.rollback_button.setEnabled(state.active)

    def _install_po_provider(self) -> None:
        manifest = str(self.settings.get("po_token_manifest_url", "")).strip()
        if not manifest:
            QMessageBox.information(self, "PO Token Provider", "Configure primeiro o manifesto HTTPS do provedor nas configurações.")
            return
        if QMessageBox.question(
            self,
            "Instalar provedor opcional",
            "O pacote será baixado para quarentena, terá o SHA-256 conferido, será inspecionado e executará um teste apenas dentro do processo separado do yt-dlp. Continuar?",
        ) != QMessageBox.Yes:
            return
        if self.po_plugin_controller:
            self.po_provider_card.set_progress(-1, "Baixando para quarentena e executando teste funcional…")
            self.po_plugin_controller.install_async(manifest)

    def _po_provider_finished(self, state: PluginState) -> None:
        self.po_provider_card.finish_progress()
        self.settings["po_token_provider"] = state.active
        self.storage.save_settings(self.settings)
        self.analyzer.po_token_provider = state.active
        self._render_po_provider(state)
        self._append_tool_log(f"PO Token Provider: {state.message}")

    def _po_provider_failed(self, message: str) -> None:
        self.po_provider_card.finish_progress()
        self.po_provider_card.path.setText(f"Quarentena preservou o aplicativo · {message}")
        self._append_tool_log(f"PO Token Provider rejeitado: {message}")

    def _disable_po_provider(self) -> None:
        state = PoTokenPluginManager().disable()
        self.settings["po_token_provider"] = False
        self.storage.save_settings(self.settings)
        self.analyzer.po_token_provider = False
        self._render_po_provider(state)

    def _app_update_action(self) -> None:
        if not self.app_updater:
            return
        if self.app_updater.release:
            self.app_updater.download_async()
        else:
            self.app_updater.check_async()

    def _app_update_busy(self, busy: bool) -> None:
        self.app_update_card.update_button.setEnabled(not busy)
        if not busy:
            self.app_update_card.finish_progress()

    def _app_update_checked(self, result: AppUpdateCheck) -> None:
        self._append_tool_log(result.error or result.message)
        if result.error:
            self.app_update_card.path.setText(result.error)
            self.app_update_card.update_button.setText("Tentar novamente")
        elif result.available and result.release:
            self.app_update_card.version.setText(f"Nova versão: {result.release.version}")
            self.app_update_card.path.setText(result.release.notes or "Release publicada e pronta para baixar")
            self.app_update_card.update_button.setText(f"Atualizar e reiniciar · {result.release.version}")
        else:
            self.app_update_card.version.setText(f"Versão {__version__}")
            self.app_update_card.path.setText(result.message)
            self.app_update_card.update_button.setText("Verificar aplicativo")

    def _app_update_downloaded(self, result: AppUpdateResult) -> None:
        self._append_tool_log(result.error or result.message)
        if result.error or not result.path:
            self.app_update_card.path.setText(result.error or result.message)
            self.app_update_card.update_button.setText("Tentar novamente")
            return
        self.app_update_card.path.setText(result.message)
        active_jobs = any(job.process and job.process.state() != QProcess.NotRunning for job in self.manager.jobs.values())
        if active_jobs:
            QMessageBox.information(
                self,
                "Instalador preparado",
                f"O instalador foi validado e salvo em:\n{result.path}\n\nFinalize os downloads antes de executá-lo.",
            )
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(result.path).parent)))
            return
        if QMessageBox.question(
            self,
            "Instalar atualização",
            f"O instalador da versão {result.version} foi validado. Deseja fechar o {APP_NAME} e iniciar a instalação agora?",
        ) == QMessageBox.Yes:
            if self.app_updater and self.app_updater.release:
                self.app_updater.updater.prepare_install(self.app_updater.release, result.path)
            arguments = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
            if QProcess.startDetached(result.path, arguments):
                self._force_close = True
                self.close()

    def _app_rollback_action(self) -> None:
        if self.app_updater:
            self.app_updater.rollback_async()

    def _app_rollback_ready(self, result: AppUpdateResult) -> None:
        self._append_tool_log(result.error or result.message)
        if not result.path:
            QMessageBox.information(self, "Restaurar versão", result.error or result.message)
            return
        if QMessageBox.question(
            self,
            "Restaurar versão anterior",
            f"A versão {result.version} foi verificada novamente. Deseja fechar o {APP_NAME} e restaurá-la agora?",
        ) != QMessageBox.Yes:
            return
        arguments = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
        if QProcess.startDetached(result.path, arguments):
            self._force_close = True
            self.close()

    def _automatic_tool_update(self) -> None:
        if not self.tool_updater or not self.auto_update_option.isChecked():
            return
        preferences = self._UpdatePreferences(
            mode=self._UpdateMode.AUTOMATIC,
            channel=self._selected_update_channel(),
            check_interval_hours=24,
            update_yt_dlp=True,
            update_ffmpeg=True,
            update_deno=True,
        )
        self.tool_updater.check_and_update_async(preferences=preferences, force=False)

    def _save_auto_update_setting(self, enabled: bool) -> None:
        self.settings["auto_tool_updates"] = enabled
        self.settings["auto_update_tools"] = enabled
        self.storage.save_settings(self.settings)

    def _check_tool_updates(self) -> None:
        if self.tool_updater:
            self._append_tool_log("Verificando atualizações…")
            preferences = self._UpdatePreferences(
                mode=self._UpdateMode.NOTIFY,
                channel=self._selected_update_channel(),
                check_interval_hours=24,
                update_yt_dlp=True,
                update_ffmpeg=True,
                update_deno=True,
            )
            self.tool_updater.check_and_update_async(preferences=preferences, force=True)

    def _run_health_check(self) -> None:
        if not self.tool_updater:
            return
        for node in self.health_nodes.values():
            node.set_state("busy", "Testando…")
        self.compatibility_summary.setText("Executando testes reais sem baixar mídia…")
        self._append_tool_log("AutoCura: iniciando teste funcional da cadeia completa.")
        if not self.tool_updater.health_check_async(deep=True):
            self._append_tool_log("Já existe uma operação de ferramentas em andamento.")

    def _run_autocure(
        self,
        reason: str,
        *,
        tools: tuple[str, ...] = ("yt-dlp", "deno"),
        report_failure: bool = True,
    ) -> bool:
        if not self.tool_updater:
            return False
        for node in self.health_nodes.values():
            node.set_state("busy", "Diagnosticando…")
        self.compatibility_summary.setText("Diagnosticando, atualizando e restaurando automaticamente quando necessário…")
        started = self.tool_updater.autocure_async(reason, tools=tools, report_failure=report_failure)
        if started:
            self._compatibility_repair_running = True
            self._compatibility_reason = reason
            self._show_page(3)
            self._append_tool_log(f"AutoCura iniciado: {reason}")
        return started

    def _rollback_tool(self, tool: str) -> None:
        if not self.tool_updater:
            return
        title = {"yt-dlp": "yt-dlp", "ffmpeg": "FFmpeg", "deno": "Deno"}.get(tool, tool)
        answer = QMessageBox.question(
            self,
            "Restaurar versão anterior",
            f"Validar e restaurar a versão anterior de {title}? A versão atual continuará preservada para diagnóstico.",
        )
        if answer != QMessageBox.Yes:
            return
        self._append_tool_log(f"Rollback manual solicitado para {title}.")
        self.tool_updater.rollback_async(tool, reason="Rollback manual solicitado pelo usuário")

    def _update_tools(self, tools: list[str] | None) -> None:
        if self.tool_updater:
            label = ", ".join(tools) if tools else "todas as ferramentas"
            self._append_tool_log(f"Atualizando {label}…")
            self.tool_updater.update_async(tools=tools)

    def _repair_after_analysis_failure(self, message: str) -> None:
        if not self.auto_update_option.isChecked():
            return
        current_url = self.url.text().strip()
        if current_url and self._analysis_repair_attempted_url == current_url:
            self._append_tool_log("O reparo automático já foi tentado para este link; aguardando ação manual.")
            return
        self._analysis_repair_attempted_url = current_url
        self._pending_analysis_repair = True
        self._compatibility_reason = message
        self._append_tool_log(f"Compatibilidade detectada: {message}")
        self._start_compatibility_repair()

    def _repair_after_download_failure(self, request_id: str, message: str) -> None:
        if not self.auto_update_option.isChecked():
            return
        if request_id in self._download_repair_attempted:
            self._append_tool_log("O reparo automático já foi tentado para este download; use Retomar para tentar novamente.")
            return
        self._download_repair_attempted.add(request_id)
        self._pending_download_repairs.add(request_id)
        self._compatibility_reason = message
        self._append_tool_log(f"Compatibilidade detectada durante download: {message}")
        self._start_compatibility_repair()

    def _start_compatibility_repair(self) -> None:
        if not self.tool_updater:
            self._pending_analysis_repair = False
            self._pending_download_repairs.clear()
            return
        if self._compatibility_repair_running:
            return
        if self.tool_updater.busy:
            QTimer.singleShot(1500, self._start_compatibility_repair)
            return
        self._compatibility_repair_running = self._run_autocure(
            self._compatibility_reason or "Falha de compatibilidade detectada"
        )
        if self._compatibility_repair_running:
            self.tools_status_label.setText("AutoCura reparando compatibilidade com o YouTube…")

    def _tool_busy_changed(self, busy: bool) -> None:
        self.check_updates_button.setEnabled(not busy)
        self.update_all_button.setEnabled(not busy)
        self.health_check_button.setEnabled(not busy)
        self.autocure_button.setEnabled(not busy)
        for card in self.tool_cards.values():
            card.update_button.setEnabled(not busy)
            card.rollback_button.setEnabled(not busy)
            if not busy:
                card.finish_progress()
        self.tools_status_label.setText("Atualização em andamento…" if busy else "Ferramentas prontas")
        self.footer_tools_label.setText("Atualizando…" if busy else "Ferramentas")
        self._set_activity("Atualizando ferramentas" if busy else "Pronto", "busy" if busy else "idle")

    def _tool_progress(self, tool: str, percent: int, message: str) -> None:
        card = self.tool_cards.get(tool)
        if card:
            card.set_progress(percent, message)
        if message:
            self.tools_status_label.setText(message)

    def _render_tool_state(self, state: Any) -> None:
        card = self.tool_cards.get(getattr(state, "tool", ""))
        if card:
            source = getattr(state, "source", "")
            detail = str(getattr(state, "path", "") or "")
            if source and detail:
                detail = f"{detail} · origem: {source}"
            card.set_state(str(getattr(state, "version", "") or ""), str(getattr(state, "path", "") or ""), detail)

    def _tool_checked(self, update: Any) -> None:
        tool = str(getattr(update, "tool", ""))
        current = getattr(update, "current", None)
        if current:
            self._render_tool_state(current)
        available = bool(getattr(update, "available", False))
        quarantined = bool(getattr(update, "quarantined", False))
        version = str(getattr(update, "available_version", "") or "")
        error = str(getattr(update, "error", "") or "")
        quarantine_reason = str(getattr(update, "quarantine_reason", "") or "")
        card = self.tool_cards.get(tool)
        if card:
            card.update_button.setText("Em quarentena" if quarantined else "Instalar atualização" if available else "Atualizar")
            card.update_button.setEnabled(not quarantined and not bool(self.tool_updater and self.tool_updater.busy))
            if available:
                card.version.setText(f"Nova versão: {version}")
        self._append_tool_log(
            error
            or (f"{tool}: versão {version} em quarentena · {quarantine_reason}" if quarantined else "")
            or (f"{tool}: versão {version} disponível" if available else f"{tool}: já está atualizado")
        )

    def _tool_updated(self, result: Any) -> None:
        tool = str(getattr(result, "tool", ""))
        error = str(getattr(result, "error", "") or "")
        version = str(getattr(result, "installed_version", "") or "")
        path = str(getattr(result, "path", "") or "")
        card = self.tool_cards.get(tool)
        if card:
            card.set_state(version, path)
            card.update_button.setText("Atualizar")
        message = str(getattr(result, "message", "") or "")
        self._append_tool_log(error or message or f"{tool}: atualização concluída")

    def _health_checked(self, report: CompatibilityReport) -> None:
        checks = {check.tool: check for check in report.checks}
        for tool in ("yt-dlp", "deno", "ffmpeg"):
            check = checks.get(tool)
            if not check:
                continue
            self.health_nodes[tool].set_state(check.level.value, _health_label(check.level), check.message)
            detail = f"{tool}: {check.message}"
            if getattr(check, "issue", None) and str(check.issue) != "none":
                detail += f" · classificação: {check.issue.value}"
            if getattr(check, "recovered_by", ""):
                detail += f" · recuperado por {check.recovered_by}"
            if check.detail:
                detail += f" · {check.detail[-240:]}"
            self._append_tool_log(detail)

        youtube = checks.get("yt-dlp")
        if youtube:
            self.health_nodes["youtube"].set_state(
                youtube.level.value,
                _health_label(youtube.level),
                "Página pública e metadados acessíveis" if youtube.level == CompatibilityLevel.HEALTHY else youtube.message,
            )
        output_level = report.level
        self.health_nodes["file"].set_state(
            output_level.value,
            _health_label(output_level),
            "Cadeia pronta para gerar arquivos" if output_level == CompatibilityLevel.HEALTHY else "Uma etapa anterior exige atenção",
        )
        badge_state = "success" if output_level == CompatibilityLevel.HEALTHY else "warning" if output_level == CompatibilityLevel.DEGRADED else "error"
        badge_text = "Cadeia saudável" if output_level == CompatibilityLevel.HEALTHY else "Teste inconclusivo" if output_level == CompatibilityLevel.DEGRADED else "Reparo necessário"
        _set_badge(self.compatibility_badge, badge_text, badge_state)
        self.compatibility_summary.setText(
            "YouTube, extração, JavaScript e conversão responderam corretamente."
            if output_level == CompatibilityLevel.HEALTHY
            else "O AutoCura preservará as versões válidas e poderá tentar uma recuperação segura."
        )

    def _rollback_finished(self, result: Any) -> None:
        error = str(getattr(result, "error", "") or "")
        message = str(getattr(result, "message", "") or "")
        self._append_tool_log(error or message or "Rollback concluído.")
        if self.tool_updater:
            for state in self.tool_updater.status():
                self._render_tool_state(state)

    def _autocure_finished(self, result: Any) -> None:
        message = str(getattr(result, "message", "") or "")
        if message:
            self._append_tool_log(message)
            self.compatibility_summary.setText(message)

    def _tool_finished(self, result: Any) -> None:
        if self._tool_bootstrap_pending:
            self._tool_bootstrap_pending = False
            for state in result if isinstance(result, list) else []:
                self._render_tool_state(state)
            self.analyze_button.setEnabled(True)
            self.tools_status_label.setText("Ferramentas prontas")
            self._append_tool_log("Ferramentas locais preparadas.")
            # Restored jobs resolve yt-dlp/FFmpeg as soon as they are pumped.
            # Delay that lookup until managed binaries are active so recovery
            # cannot trigger another synchronous bundled-tool bootstrap.
            self.manager.restore()
            # Checking releases can only begin after bundled tools have been
            # copied, activated and probed.  This also avoids competing with a
            # compatibility repair requested while the app was starting.
            if self.auto_update_option.isChecked():
                QTimer.singleShot(1600, self._automatic_tool_update)
            return
        results = getattr(result, "results", None)
        if results:
            for item in results:
                self._tool_updated(item)
        skipped = str(getattr(result, "skipped_reason", "") or "")
        if skipped:
            self._append_tool_log(skipped)
        self.tools_status_label.setText("Verificação concluída")
        if not isinstance(result, CompatibilityReport) and not hasattr(result, "health_after"):
            QTimer.singleShot(1200, self._run_background_health_check)
        if self._compatibility_repair_running:
            self._compatibility_repair_running = False
            health_after = getattr(result, "health_after", None)
            if health_after is not None:
                succeeded = bool(getattr(health_after, "ok", False))
            else:
                items = result if isinstance(result, list) else getattr(result, "results", [])
                succeeded = bool(items) and not any(str(getattr(item, "error", "") or "") for item in items)
            pending_analysis = self._pending_analysis_repair
            pending_downloads = set(self._pending_download_repairs)
            self._pending_analysis_repair = False
            self._pending_download_repairs.clear()
            if succeeded:
                self._append_tool_log("Ferramentas reparadas; retomando as operações afetadas.")
                if pending_analysis:
                    self._automatic_analysis_retry = True
                    QTimer.singleShot(0, self._analyze)
                for request_id in pending_downloads:
                    if request_id in self.manager.requests:
                        self.manager.resume(request_id)
            else:
                self._append_tool_log("O reparo automático não foi concluído. Tente Atualizar tudo.")

    def _run_background_health_check(self) -> None:
        if not self.tool_updater or self.tool_updater.busy:
            return
        self.tool_updater.health_check_async(deep=True)

    def _tool_failed(self, message: str) -> None:
        bootstrap_failed = self._tool_bootstrap_pending
        self._tool_bootstrap_pending = False
        if bootstrap_failed:
            self.analyze_button.setEnabled(True)
        self.tools_status_label.setText("Falha na atualização")
        self.footer_tools_label.setText("Atenção")
        self._set_activity("Falha na atualização", "error")
        self._append_tool_log(f"Erro: {message}")
        self.statusBar().showMessage("Não foi possível atualizar as ferramentas", 5000)

    def _append_tool_log(self, message: str) -> None:
        if message:
            self.tool_log.append(message)

    def _set_nav_text(self, compact: bool) -> None:
        for button in self.nav_buttons:
            label = str(button.property("label"))
            button.setText("" if compact else label)
            button.setToolTip(label if compact else "")
        self.settings_button.setText("" if compact else "Configurações")
        self.settings_button.setToolTip("Configurações" if compact else "")
        self.about_button.setText("" if compact else "Créditos")
        self.about_button.setToolTip("Créditos e sobre" if compact else "")

    def _toggle_compact_mode(self, enabled: bool) -> None:
        if enabled == self._compact_mode:
            return
        self._compact_mode = enabled
        if enabled:
            self._normal_geometry = self.saveGeometry()
            self._show_page(1)
            self.sidebar.hide()
            self.page_description.hide()
            self.flow_rail.hide()
            self.activity_pill.hide()
            self.main_layout.setContentsMargins(14, 12, 14, 10)
            self.setMinimumSize(560, 360)
            self.resize(720, 460)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.compact_mode_button.setText("Expandir")
            self.compact_mode_button.setToolTip("Voltar para a janela completa")
            self.show()
        else:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            self.sidebar.show()
            self.page_description.show()
            self.flow_rail.show()
            self.activity_pill.show()
            self.main_layout.setContentsMargins(28, 24, 28, 16)
            self.setMinimumSize(760, 620)
            self.compact_mode_button.setText("Modo compacto")
            self.compact_mode_button.setToolTip("Mantém uma fila menor e sempre visível")
            self.show()
            if self._normal_geometry is not None:
                self.restoreGeometry(self._normal_geometry)
                self._normal_geometry = None

    def _layout_queue_actions(self, compact: bool) -> None:
        if getattr(self, "_queue_actions_compact", None) == compact:
            return
        self._queue_actions_compact = compact
        for button in self.queue_action_buttons:
            self.queue_actions_layout.removeWidget(button)
        for column in range(7):
            self.queue_actions_layout.setColumnStretch(column, 0)
        if compact:
            for index, button in enumerate(self.queue_action_buttons):
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                self.queue_actions_layout.addWidget(button, index // 3, index % 3)
            for column in range(3):
                self.queue_actions_layout.setColumnStretch(column, 1)
        else:
            for column, button in enumerate(self.queue_action_buttons[:-1]):
                button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
                self.queue_actions_layout.addWidget(button, 0, column)
            self.queue_actions_layout.setColumnStretch(5, 1)
            self.queue_actions_layout.addWidget(self.clear_completed_button, 0, 6)

    def _apply_responsive_layout(self) -> None:
        compact = self.width() < 980
        if compact != self._compact_navigation:
            self._compact_navigation = compact
            self.sidebar.setFixedWidth(74 if compact else 228)
            self.brand_text_block.setVisible(not compact)
            self.sidebar_version.setVisible(not compact)
            self._set_nav_text(compact)
        # The queue has more operational actions than the other pages; reflow
        # them before navigation itself needs to collapse.
        self._layout_queue_actions(self.width() < 1320)
        self.tools_status_label.setVisible(not compact)
        self.auto_update_option.setText("Automático" if compact else "Verificação automática")
        self.check_updates_button.setText("Verificar" if compact else "Verificar atualizações")
        self.update_all_button.setText("Atualizar tudo")
        self.advanced_button.setText("Avançadas" if compact else "Opções avançadas")
        narrow = self.width() < 1120
        self.table.setColumnHidden(4, narrow)
        self.table.setColumnHidden(5, narrow)
        very_narrow = self.width() < 900
        self.table.setColumnHidden(6, very_narrow)
        self.history_count.setVisible(not very_narrow)
        for button, text in (
            (self.history_open_button, "Abrir local"),
            (self.history_copy_button, "Copiar link"),
            (self.history_refresh_button, "Atualizar"),
        ):
            button.setText("" if very_narrow else text)
            button.setToolTip(text if very_narrow else "")
        self.thumbnail.setFixedSize(156, 88) if very_narrow else self.thumbnail.setFixedSize(208, 117)
        if not self._thumbnail_source.isNull():
            self.thumbnail.setPixmap(_rounded_cover(self._thumbnail_source, self.thumbnail.size()))
        extras_compact = self.width() < 900
        if getattr(self, "_extras_compact", None) != extras_compact:
            self._extras_compact = extras_compact
            for option in self.extra_options:
                self.extras_grid.removeWidget(option)
            for index, option in enumerate(self.extra_options):
                row, column = (divmod(index, 2) if extras_compact else (0, index))
                self.extras_grid.addWidget(option, row, column)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "sidebar"):
            self._apply_responsive_layout()

    def dragEnterEvent(self, event: Any) -> None:
        mime = event.mimeData()
        text = mime.text().strip()
        if text and ("youtube.com" in text.lower() or "youtu.be" in text.lower()):
            event.acceptProposedAction()

    def dropEvent(self, event: Any) -> None:
        value = event.mimeData().text().strip()
        if value:
            self._show_page(0)
            self.url.setText(value.splitlines()[0])
            event.acceptProposedAction()

    def closeEvent(self, event: Any) -> None:
        if self.tool_updater and self.tool_updater.busy and "--smoke-test" not in QApplication.arguments():
            QMessageBox.information(
                self,
                "Atualização em andamento",
                f"Aguarde a atualização das ferramentas terminar antes de fechar o {APP_NAME}.",
            )
            event.ignore()
            return
        active = [job for job in self.manager.jobs.values() if job.process and job.process.state() != job.process.NotRunning]
        if active and self.tray and self.settings.get("close_to_tray", True) and not self._force_close and "--smoke-test" not in QApplication.arguments():
            self.hide()
            self.tray.showMessage(APP_NAME, "O aplicativo continua na bandeja enquanto a fila trabalha.", QSystemTrayIcon.Information, 6000)
            event.ignore()
            return
        if active and QMessageBox.question(self, f"Fechar o {APP_NAME}", "Há downloads ativos. Deseja interrompê-los e fechar? Os arquivos parciais poderão ser retomados depois.") != QMessageBox.Yes:
            event.ignore()
            return
        for job in active:
            job.stop()
        if self.tray:
            self.tray.hide()
        event.accept()


STYLE = r"""
QWidget {
    background: transparent;
    color: ${text};
    font-family: "Segoe UI Variable Text", "Segoe UI";
    font-size: 10pt;
}
QLabel, QCheckBox, QRadioButton { background: transparent; }
QMainWindow, QDialog, QMessageBox { background: ${canvas}; }
QWidget#appRoot, QWidget#mainSurface { background: ${shell}; }

QFrame#sidebar {
    background: ${shell};
    border: 0;
    border-right: 1px solid ${border};
}
QWidget#brandBlock { background: transparent; }
QLabel#brandMark {
    background: ${surface_raised};
    border: 1px solid ${accent};
    border-radius: 7px;
    padding: 4px;
}
QLabel#brand { color: ${text}; font-size: 16pt; font-weight: 700; }
QLabel#brandSubtitle { color: ${faint}; font-size: 8.5pt; }
QLabel#sidebarVersion { color: ${faint}; font-size: 8pt; padding: 3px 9px; }

QLabel#pageTitle { font-size: 22pt; font-weight: 700; color: ${text}; }
QLabel#sectionTitle, QLabel#cardTitle { font-size: 12pt; font-weight: 650; color: ${text}; }
QLabel#dialogTitle { font-size: 20pt; font-weight: 700; color: ${text}; }
QLabel#mediaTitle { font-size: 15pt; font-weight: 650; color: ${text}; }
QLabel#fieldLabel { color: ${text_soft}; font-weight: 600; }
QLabel#muted { color: ${muted}; }
QLabel#eyebrow { color: ${cyan}; font-size: 8pt; font-weight: 700; }

QFrame#activityPill {
    background: ${input};
    border: 1px solid ${border_strong};
    border-radius: 10px;
}
QFrame#activityPill[state="busy"] { border-color: #246C8B; }
QFrame#activityPill[state="warning"] { border-color: #725A2E; }
QFrame#activityPill[state="error"] { border-color: #713A43; }
QLabel#activityDot { color: ${success}; font-size: 12pt; }
QLabel#activityText { color: ${text_soft}; padding-right: 2px; }
QLabel#statusPill, QLabel#counterPill {
    background: #0D2748;
    color: #66B7FF;
    border: 1px solid #1757A0;
    border-radius: 8px;
    padding: 4px 9px;
}
QLabel#counterPill { background: ${surface_raised}; color: ${muted}; border-color: ${border}; }
QLabel#statusPill[state="loading"] { background: #0B2633; color: ${cyan}; border-color: #246C8B; }
QLabel#statusPill[state="success"] { background: #0D2A22; color: #63E39A; border-color: #245A47; }
QLabel#statusPill[state="warning"] { background: #2B2517; color: #FFD17A; border-color: #725A2E; }
QLabel#statusPill[state="error"] { background: #321A20; color: #FF9BA3; border-color: #713A43; }

QLabel#notice, QLabel#inlineNotice, QLabel#usageNote {
    background: #0B1C2B;
    color: ${text_soft};
    border: 1px solid #1D3D59;
    border-left: 3px solid ${cyan};
    border-radius: 8px;
    padding: 10px 12px;
}
QLabel#inlineNotice, QLabel#usageNote {
    background: ${surface};
    color: ${muted};
    border-color: ${border};
    border-left-color: ${border_strong};
}
QLabel#cookieWarning { background: #221E14; color: #FFD17A; border: 1px solid #725A2E; border-radius: 7px; padding: 10px; }

QFrame#card, QFrame#toolCard, QFrame#toolbar, QFrame#dataPanel {
    background: ${surface};
    border: 1px solid ${border};
    border-radius: 11px;
}
QFrame#primaryCard { background: ${surface}; border: 1px solid ${border_strong}; border-radius: 11px; }
QFrame#sessionStrip {
    background: ${input};
    border: 1px solid ${border};
    border-radius: 8px;
}
QFrame#sessionStrip[state="ready"] { background: #0B1E1A; border-color: #245A47; }
QFrame#sessionStrip[state="error"] { background: #28171C; border-color: #713A43; }
QLabel#sessionState { color: ${text_soft}; font-weight: 650; }
QFrame#toolbar { background: ${surface}; border-radius: 9px; }
QFrame#dataPanel { background: ${surface}; }
QFrame#compatibilityHero {
    background: ${surface_raised};
    border: 1px solid ${border_strong};
    border-radius: 12px;
}
QFrame#healthNode {
    background: ${input};
    border: 1px solid ${border};
    border-radius: 8px;
}
QFrame#healthNode[state="healthy"] { border-color: #245A47; background: #0B1E1A; }
QFrame#healthNode[state="degraded"] { border-color: #725A2E; background: #221E14; }
QFrame#healthNode[state="failed"] { border-color: #713A43; background: #28171C; }
QFrame#healthNode[state="busy"] { border-color: #246C8B; background: #0A1C26; }
QLabel#healthTitle { color: ${text_soft}; font-size: 9pt; font-weight: 650; }
QLabel#healthStatus { color: ${text}; font-size: 10pt; font-weight: 700; }
QFrame#healthConnector { background: ${border_strong}; border: 0; }
QFrame#railSegment { background: ${border}; border: 0; border-radius: 1px; min-height: 2px; max-height: 2px; }
QFrame#railSegment[active="true"] { background: ${cyan}; }
QFrame#metricDivider { background: ${border}; min-width: 1px; max-width: 1px; margin: 4px 12px; }

QDialog#aboutDialog, QWidget#aboutCanvas { background: ${canvas}; }
QFrame#aboutMasthead {
    background: ${surface};
    border: 1px solid ${border};
    border-radius: 12px;
}
QLabel#aboutBrandMark {
    background: ${surface_raised};
    border: 1px solid ${border_strong};
    border-radius: 10px;
}
QLabel#aboutBrandName { color: ${text}; font-size: 19pt; font-weight: 750; }
QLabel#aboutTagline { color: ${muted}; font-size: 9pt; }
QLabel#aboutVersion {
    background: #0D2748;
    color: #79C5FF;
    border: 1px solid #1757A0;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 8pt;
    font-weight: 750;
}
QFrame#creatorCard { background: ${surface}; border: 1px solid ${border_strong}; border-radius: 15px; }
QFrame#creatorPortraitPanel { background: transparent; }
QLabel#creatorAvatar { background: ${input}; color: ${gold_soft}; border-radius: 15px; font-size: 22pt; font-weight: 750; }
QLabel#portraitCaption { color: ${gold}; font-size: 7.5pt; font-weight: 700; }
QLabel#creatorBadge { color: ${gold}; font-size: 8pt; font-weight: 750; }
QLabel#creatorName { color: ${text}; font-size: 26pt; font-weight: 750; }
QLabel#creatorRole { color: ${text_soft}; font-size: 10.5pt; }
QFrame#creatorGoldLine { background: ${gold}; border: 0; border-radius: 1px; max-width: 150px; }
QLabel#creatorStatement { color: ${text_soft}; font-size: 10pt; line-height: 1.35; }
QLabel#creditTag {
    background: ${surface_raised};
    color: ${muted};
    border: 1px solid ${border};
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 7.5pt;
    font-weight: 700;
}
QLabel#socialLabel, QLabel#manifestoTitle { color: ${gold}; font-size: 8pt; font-weight: 750; }
QLabel#creatorHint { color: ${faint}; font-size: 8.5pt; }
QFrame#creatorManifesto {
    background: ${gold_surface};
    border: 1px solid ${gold_border};
    border-radius: 9px;
}
QLabel#manifestoText { color: ${text_soft}; font-size: 9pt; }
QFrame#diagnosticCard {
    background: ${surface};
    border: 1px solid ${border};
    border-radius: 11px;
}
QLabel#diagnosticTitle { color: ${text}; font-size: 11pt; font-weight: 650; }
QLabel#diagnosticSubtitle, QLabel#aboutFooter { color: ${muted}; font-size: 8.5pt; }
QTextEdit#diagnosticText { font-family: "Cascadia Mono", "Consolas"; font-size: 8.5pt; }

QFrame#emptyState { background: ${surface}; border: 1px dashed ${border_strong}; border-radius: 11px; }
QLabel#emptySymbol { color: ${accent}; font-size: 28pt; }
QLabel#emptyTitle { color: ${text}; font-size: 13pt; font-weight: 650; }
QLabel#thumbnail {
    background: #05090E;
    color: ${faint};
    border: 1px solid ${border_strong};
    border-radius: 8px;
}
QLabel#toolIcon {
    background: #0D2A22;
    color: ${success};
    border: 1px solid #245A47;
    border-radius: 19px;
    min-width: 38px; min-height: 38px; max-width: 38px; max-height: 38px;
    qproperty-alignment: AlignCenter;
}
QLabel#toolIcon[ok="false"] { background: #321A20; color: ${danger}; border-color: #65313A; }
QLabel#toolVersion { color: #73B7FF; font-weight: 600; }
QLabel#outputPreview { color: ${cyan}; font-size: 9pt; }

QLineEdit, QComboBox, QSpinBox, QTextEdit, QListWidget, QTableWidget {
    background: ${input};
    color: ${text};
    border: 1px solid ${border_strong};
    border-radius: 7px;
    padding: 9px 11px;
    selection-background-color: ${accent};
    selection-color: white;
}
QLineEdit, QComboBox, QSpinBox { min-height: 20px; }
QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: #51647E; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QListWidget:focus, QTableWidget:focus {
    border: 1px solid ${cyan};
}
QLineEdit[primary="true"] { font-size: 10.5pt; padding: 10px 12px; }
QComboBox::drop-down { border: 0; width: 28px; }
QComboBox::down-arrow { image: url(${chevron_path}); width: 10px; height: 10px; }
QComboBox QAbstractItemView { background: ${surface_raised}; border: 1px solid ${border_strong}; selection-background-color: #174B84; }

QPushButton {
    background: ${surface_raised};
    color: ${text_soft};
    border: 1px solid ${border_strong};
    border-radius: 7px;
    padding: 8px 13px;
    min-height: 20px;
}
QPushButton:hover { background: ${surface_hover}; border-color: #536985; color: ${text}; }
QPushButton:pressed { background: ${input}; }
QPushButton:focus { border: 1px solid ${cyan}; }
QPushButton:disabled { background: ${surface}; color: ${faint}; border-color: ${border}; }
QPushButton[role="primary"] { background: ${accent}; border-color: #3892FF; color: white; font-weight: 650; }
QPushButton[role="primary"]:hover { background: ${accent_hover}; border-color: #62AAFF; }
QPushButton[role="primary"]:pressed { background: ${accent_pressed}; }
QPushButton[role="primary"][busy="true"] { background: #146BBA; border-color: ${cyan}; }
QPushButton[role="ghost"] { background: transparent; border-color: transparent; color: ${muted}; }
QPushButton[role="ghost"]:hover { background: ${surface_raised}; color: ${text}; }
QPushButton#creditsButton { color: ${gold_soft}; }
QPushButton#creditsButton:hover { background: ${gold_surface}; border-color: ${gold_border}; color: ${gold_soft}; }
QPushButton[role="danger"] { background: transparent; border-color: #65313A; color: #FF9BA3; }
QPushButton[role="danger"]:hover { background: #2C171C; border-color: ${danger}; }
QPushButton[role="instagram"] { background: #2C172B; color: #FFD9EF; border-color: #934675; font-weight: 600; }
QPushButton[role="instagram"]:hover { background: #3C1C38; border-color: #CF67A7; }
QPushButton[role="discord"] { background: #1D2444; color: #E0E4FF; border-color: #5865F2; font-weight: 600; }
QPushButton[role="discord"]:hover { background: #273057; border-color: #7C86FF; }
QPushButton#instagramButton, QPushButton#discordButton { text-align: left; padding: 10px 14px; border-radius: 9px; }

QPushButton#navButton {
    text-align: left;
    background: transparent;
    border: 0;
    border-left: 3px solid transparent;
    color: ${muted};
    padding: 11px 13px;
    border-radius: 7px;
}
QPushButton#navButton:hover { background: ${surface}; color: ${text_soft}; }
QPushButton#navButton:checked { background: ${surface_raised}; color: #CBE4FF; border-left-color: ${accent}; }

QCheckBox { spacing: 8px; color: ${text_soft}; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #596A80; border-radius: 4px; background: ${input}; }
QCheckBox::indicator:hover { border-color: ${cyan}; }
QCheckBox::indicator:checked { background: ${accent}; border-color: #58A1FF; image: url(${checkmark_path}); }
QCheckBox:focus { color: ${cyan}; }
QGroupBox { border: 1px solid ${border}; border-radius: 9px; margin-top: 10px; padding-top: 12px; color: ${text_soft}; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }

QHeaderView::section {
    background: ${surface_raised};
    color: ${muted};
    border: 0;
    border-bottom: 1px solid ${border_strong};
    padding: 10px 9px;
    font-weight: 600;
}
QTableWidget { border-radius: 10px; alternate-background-color: #0E1722; padding: 0; }
QTableWidget::item { border-bottom: 1px solid #1D2A3A; padding: 8px; }
QTableWidget::item:selected { background: #123C69; color: white; }
QListWidget { alternate-background-color: #0E1722; }
QListWidget::item { padding: 10px; border-bottom: 1px solid #1D2A3A; }
QListWidget::item:selected { background: #123C69; }
QProgressBar { background: #1A2737; color: ${text}; border: 0; border-radius: 4px; text-align: center; min-height: 9px; }
QProgressBar::chunk { background: ${cyan}; border-radius: 3px; }

QScrollArea { background: transparent; border: 0; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #334258; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #465A75; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QMenu { background: ${surface_raised}; border: 1px solid ${border_strong}; padding: 5px; }
QMenu::item { padding: 8px 28px 8px 10px; border-radius: 5px; }
QMenu::item:selected { background: #174B84; }
QStatusBar#operationBar { background: ${shell}; color: ${muted}; border-top: 1px solid ${border}; min-height: 34px; }
QStatusBar#operationBar QLabel { color: ${muted}; }
QLabel#metricValue { color: ${text_soft}; padding: 0 4px; }
QToolTip { background: #1A2635; color: ${text}; border: 1px solid #52647D; padding: 6px; }
"""

for _token_name, _token_value in THEME.items():
    STYLE = STYLE.replace(f"${{{_token_name}}}", _token_value)
STYLE = STYLE.replace("${checkmark_path}", (resource_dir() / "assets" / "ui-check.svg").as_posix())
STYLE = STYLE.replace("${chevron_path}", (resource_dir() / "assets" / "ui-chevron.svg").as_posix())
