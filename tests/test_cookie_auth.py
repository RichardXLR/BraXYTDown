from pathlib import Path

import pytest

from baixatube.cookie_auth import CookieConfig, cookie_arguments, validate_cookie_file


def netscape_cookie_file(path: Path) -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-value\n",
        encoding="utf-8",
    )
    return path


def test_browser_cookie_source_requires_consent_and_is_a_single_argument():
    with pytest.raises(ValueError, match="uso responsável"):
        cookie_arguments(mode="browser", browser="edge", consent=False)

    assert cookie_arguments(
        mode="browser",
        browser="firefox",
        profile="Profile 1",
        consent=True,
    ) == ["--cookies-from-browser", "firefox:Profile 1"]


def test_cookie_file_must_be_local_netscape_format(tmp_path: Path):
    cookie_file = netscape_cookie_file(tmp_path / "cookies.txt")
    assert validate_cookie_file(str(cookie_file)) == cookie_file
    assert cookie_arguments(mode="file", file_path=str(cookie_file), consent=True) == ["--cookies", str(cookie_file)]

    invalid = tmp_path / "invalid.txt"
    invalid.write_text("SID=not-a-netscape-file", encoding="utf-8")
    with pytest.raises(ValueError, match="Mozilla/Netscape"):
        validate_cookie_file(str(invalid))


def test_disabled_cookie_config_never_forwards_stale_values():
    config = CookieConfig(mode="off", browser="unknown", file_path="C:/missing/cookies.txt", consent=True)
    assert config.arguments() == []
    assert not config.enabled


def test_cookie_label_never_contains_cookie_contents(tmp_path: Path):
    cookie_file = netscape_cookie_file(tmp_path / "private-cookies.txt")
    config = CookieConfig(mode="file", file_path=str(cookie_file), consent=True)
    assert config.label() == "Sessão por arquivo · private-cookies.txt"
    assert "secret-value" not in config.label()
