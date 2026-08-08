"""Native Windows toast command with direct file and folder actions."""

from __future__ import annotations

import base64
import os
from pathlib import Path


APP_USER_MODEL_ID = "RichardIttou.BraXYTDow"


def toast_command(title: str, message: str, output_path: str = "") -> tuple[str, list[str]] | None:
    if os.name != "nt":
        return None
    path = Path(output_path) if output_path else None
    file_uri = path.resolve().as_uri() if path and path.exists() else ""
    folder_uri = (path.parent if path and path.suffix else path).resolve().as_uri() if path and path.exists() else ""
    script = r"""
function Decode([string]$value) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value)) }
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$title = [Security.SecurityElement]::Escape((Decode $args[0]))
$message = [Security.SecurityElement]::Escape((Decode $args[1]))
$fileUri = [Security.SecurityElement]::Escape((Decode $args[2]))
$folderUri = [Security.SecurityElement]::Escape((Decode $args[3]))
$actions = ''
if ($fileUri) {
  $actions = "<actions><action content='Abrir arquivo' activationType='protocol' arguments='$fileUri'/><action content='Abrir pasta' activationType='protocol' arguments='$folderUri'/></actions>"
}
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$message</text></binding></visual>$actions</toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('RichardIttou.BraXYTDow').Show($toast)
"""
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")

    def encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    return (
        "powershell.exe",
        ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded_script, encode(title), encode(message), encode(file_uri), encode(folder_uri)],
    )


__all__ = ["APP_USER_MODEL_ID", "toast_command"]
