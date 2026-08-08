from __future__ import annotations

import sys
import logging

from PySide6.QtCore import QLockFile, QProcess, QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .branding import APP_NAME
from .app_updates import evaluate_startup_recovery, mark_startup_healthy
from .ui import MainWindow, STYLE
from .diagnostics import configure_logging
from .paths import data_dir, legacy_data_dir, resource_dir


def main() -> int:
    configure_logging()

    def report_exception(kind, value, traceback) -> None:
        logging.getLogger("baixatube").exception("Erro não tratado", exc_info=(kind, value, traceback))
        sys.__excepthook__(kind, value, traceback)

    sys.excepthook = report_exception
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    icon = resource_dir() / "assets" / "baixatube-icon.png"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    recovery = evaluate_startup_recovery()
    if recovery.action == "rollback" and recovery.installer_path:
        arguments = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]
        if QProcess.startDetached(recovery.installer_path, arguments):
            logging.getLogger("baixatube").warning("Rollback automático iniciado: %s", recovery.message)
            return 0

    # The packaging smoke test must be isolated from a user's running instance and
    # from stale locks left by a forcefully terminated test. Normal launches still
    # enforce a single application instance.
    if "--smoke-test" not in sys.argv:
        root = data_dir()
        lock_name = "baixatube-instance.lock" if root.resolve() == legacy_data_dir().resolve() else "braxytdow-instance.lock"
        instance_lock = QLockFile(str(root / lock_name))
        instance_lock.setStaleLockTime(10_000)
        locked = instance_lock.tryLock(150)
        if not locked and instance_lock.removeStaleLockFile():
            locked = instance_lock.tryLock(150)
        if not locked:
            QMessageBox.information(None, f"{APP_NAME} já está aberto", f"Use a janela do {APP_NAME} que já está em execução.")
            return 0
        # Keep the lock alive for exactly the QApplication lifetime.
        app._braxytdow_instance_lock = instance_lock  # type: ignore[attr-defined]
    window = MainWindow()
    window.show()
    # A release só é considerada saudável depois que a janela e os serviços
    # permaneceram ativos. Falhas antes deste marco alimentam o rollback.
    QTimer.singleShot(15_000, mark_startup_healthy)
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(1200, app.quit)
    return app.exec()
