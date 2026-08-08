from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


COOKIE_MODES = frozenset({"off", "browser", "file"})
SUPPORTED_BROWSERS: tuple[tuple[str, str], ...] = (
    ("edge", "Microsoft Edge"),
    ("chrome", "Google Chrome"),
    ("firefox", "Mozilla Firefox"),
    ("brave", "Brave"),
    ("chromium", "Chromium"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("whale", "Naver Whale"),
)
SUPPORTED_BROWSER_IDS = frozenset(item[0] for item in SUPPORTED_BROWSERS)
NETSCAPE_HEADERS = (b"# HTTP Cookie File", b"# Netscape HTTP Cookie File")


@dataclass(frozen=True, slots=True)
class CookieConfig:
    mode: str = "off"
    browser: str = "edge"
    profile: str = ""
    file_path: str = ""
    consent: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "CookieConfig":
        return cls(
            mode=str(values.get("cookie_mode", "off")),
            browser=str(values.get("cookie_browser", "edge")),
            profile=str(values.get("cookie_profile", "")),
            file_path=str(values.get("cookie_file", "")),
            consent=bool(values.get("cookie_consent", False)),
        )

    @property
    def enabled(self) -> bool:
        return self.mode in {"browser", "file"} and self.consent

    def arguments(self, *, require_file: bool = True) -> list[str]:
        if self.mode not in COOKIE_MODES:
            raise ValueError("Modo de cookies desconhecido.")
        if self.mode == "off":
            return []
        if not self.consent:
            raise ValueError("Confirme o uso responsável antes de ativar cookies.")
        if self.mode == "browser":
            browser = self.browser.strip().lower()
            if browser not in SUPPORTED_BROWSER_IDS:
                raise ValueError("Selecione um navegador compatível.")
            profile = normalize_browser_profile(self.profile)
            source = f"{browser}:{profile}" if profile else browser
            return ["--cookies-from-browser", source]
        cookie_file = validate_cookie_file(self.file_path, require_exists=require_file)
        return ["--cookies", str(cookie_file)]

    def label(self) -> str:
        if not self.enabled:
            return "Sessão desativada"
        if self.mode == "browser":
            names = dict(SUPPORTED_BROWSERS)
            return f"Sessão pelo {names.get(self.browser, self.browser.title())}"
        name = Path(self.file_path).name or "cookies.txt"
        return f"Sessão por arquivo · {name}"


def normalize_browser_profile(value: str) -> str:
    profile = value.strip()
    if len(profile) > 260 or any(ord(character) < 32 for character in profile):
        raise ValueError("O perfil do navegador contém caracteres inválidos.")
    if "::" in profile:
        raise ValueError("O perfil do navegador não pode conter '::'.")
    return profile


def validate_cookie_file(value: str, *, require_exists: bool = True) -> Path:
    raw = value.strip().strip('"')
    if not raw:
        raise ValueError("Escolha um arquivo de cookies no formato Netscape.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Use um caminho absoluto para o arquivo de cookies.")
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Por segurança, mantenha o arquivo de cookies em um disco local.")
    if not require_exists:
        return path
    if not path.is_file():
        raise ValueError("O arquivo de cookies não foi encontrado.")
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("O arquivo de cookies é maior que o limite seguro de 20 MB.")
        with path.open("rb") as stream:
            first_line = stream.readline(256).strip()
    except OSError as exc:
        raise ValueError("Não foi possível ler o arquivo de cookies.") from exc
    if not any(first_line.startswith(header) for header in NETSCAPE_HEADERS):
        raise ValueError("O arquivo precisa estar no formato Mozilla/Netscape usado pelo yt-dlp.")
    return path


def cookie_arguments(
    *,
    mode: str = "off",
    browser: str = "edge",
    profile: str = "",
    file_path: str = "",
    consent: bool = False,
    require_file: bool = True,
) -> list[str]:
    return CookieConfig(mode, browser, profile, file_path, consent).arguments(require_file=require_file)
