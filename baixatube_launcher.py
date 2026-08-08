"""Stable PyInstaller entry point outside the package namespace."""

from baixatube.app import main


if __name__ == "__main__":
    raise SystemExit(main())
