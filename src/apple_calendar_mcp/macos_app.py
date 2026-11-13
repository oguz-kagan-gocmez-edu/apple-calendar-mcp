"""The "Apple Calendar MCP.app" helper that owns the calendar permission.

Since macOS 14, a calendar permission request is refused without a prompt
unless the *responsible* app (the one that started us, e.g. Claude or
Terminal) declares NSCalendarsFullAccessUsageDescription, and neither does.

`apple-calendar-mcp install-app` builds a tiny app bundle that declares it.
Releases on PyPI carry the launcher prebuilt (a universal binary compiled by
the release workflow from macos_helper/launcher.c), so installing needs no
compiler; from a git checkout it is compiled with clang. Once installed,
the CLI re-runs itself through the bundle's launcher, so the permission
belongs to "Apple Calendar MCP" no matter which app starts the server. See
macos_helper/launcher.c for the launcher itself.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal

from . import __version__

APP_NAME = "Apple Calendar MCP"
BUNDLE_ID = "io.github.oguz-kagan-gocmez-edu.apple-calendar-mcp"
EXECUTABLE = "apple-calendar-mcp-launcher"
# Set on the relaunched server so it doesn't relaunch again.
VIA_APP_ENV = "APPLE_CALENDAR_MCP_VIA_APP"
# Set to 1 to never use the helper.
NO_APP_ENV = "APPLE_CALENDAR_MCP_NO_APP"
# Set to 1 to install (or update) the prebuilt helper on start, as the Claude Desktop
# extension does: its users never see a terminal to run `install-app` in.
INSTALL_APP_ENV = "APPLE_CALENDAR_MCP_INSTALL_APP"

USAGE_TEXT = "Lets Claude read and manage your calendar events through the apple-calendar-mcp server."
REMINDERS_USAGE_TEXT = "Lets Claude read and manage your reminders through the apple-calendar-mcp server."

Runner = Callable[..., Any]


def default_app_path() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def launcher_path(app: Path) -> Path:
    return app / "Contents" / "MacOS" / EXECUTABLE


def info_plist() -> bytes:
    return plistlib.dumps(
        {
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": APP_NAME,
            "CFBundleExecutable": EXECUTABLE,
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": __version__,
            "CFBundleShortVersionString": __version__,
            "LSUIElement": True,  # no Dock icon
            "NSCalendarsFullAccessUsageDescription": USAGE_TEXT,  # macOS 14+
            "NSCalendarsUsageDescription": USAGE_TEXT,  # macOS 13
            "NSRemindersFullAccessUsageDescription": REMINDERS_USAGE_TEXT,  # macOS 14+
            "NSRemindersUsageDescription": REMINDERS_USAGE_TEXT,  # macOS 13
        }
    )


def launcher_source() -> str:
    return resources.files("apple_calendar_mcp").joinpath("macos_helper/launcher.c").read_text("utf-8")


# Mach-O magic numbers: universal ("fat") binaries and 64-bit thin ones.
_MACHO_MAGIC = (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")


def prebuilt_launcher() -> Traversable | None:
    """The launcher shipped in the wheel, or None (e.g. a git checkout)."""
    f = resources.files("apple_calendar_mcp").joinpath(f"macos_helper/{EXECUTABLE}")
    try:
        head = f.read_bytes()[:4] if f.is_file() else b""
    except OSError:
        return None
    return f if head in _MACHO_MAGIC else None


class AppBuildError(RuntimeError):
    pass


Source = Literal["prebuilt", "compiled"]


def build_app(
    app: Path | None = None,
    run: Runner = subprocess.run,
    *,
    from_source: bool = False,
    prebuilt: Traversable | None = None,
) -> tuple[Path, Source]:
    """Put the launcher in place, write Info.plist and ad-hoc sign the bundle.

    Uses the prebuilt launcher when the package has one (unless ``from_source``),
    otherwise compiles ``launcher.c`` with clang. Returns the app and which it used.
    """
    app = app or default_app_path()
    prebuilt = None if from_source else (prebuilt or prebuilt_launcher())
    clang = None if prebuilt else shutil.which("clang")
    if prebuilt is None and clang is None:
        raise AppBuildError(
            "clang not found, and this copy of apple-calendar-mcp has no prebuilt helper (it's from "
            "a git checkout). Either install the Xcode Command Line Tools (xcode-select --install), "
            "or install the same helper ready-made from PyPI: uvx apple-calendar-eventkit-mcp install-app"
        )
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(info_plist())
    exe = launcher_path(app)
    try:
        if prebuilt is not None:
            tmp = exe.with_name(exe.name + ".new")
            tmp.write_bytes(prebuilt.read_bytes())
            tmp.chmod(0o755)
            tmp.replace(exe)  # atomic, even over a running launcher
        else:
            assert clang is not None
            src = contents / "launcher.c"
            src.write_text(launcher_source(), "utf-8")
            run([clang, "-O2", "-Wall", "-o", str(exe), str(src)], check=True)
            src.unlink()
        run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise AppBuildError(f"building {app} failed: {exc}") from exc
    return app, "prebuilt" if prebuilt is not None else "compiled"


# Folders macOS guards per app (TCC). The helper app has no access to them, so
# it can't start a Python that lives there; it fails before our code runs.
PROTECTED_FOLDERS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")


def code_paths() -> list[Path]:
    """Where the running server's Python environment and code live."""
    return [Path(sys.prefix), Path(__file__).resolve().parent]


def protected_folder(paths: Sequence[Path] | None = None, home: Path | None = None) -> Path | None:
    """The macOS-protected folder holding this server's files, if any (None: the helper can run it)."""
    home = (home or Path.home()).resolve()
    for path in code_paths() if paths is None else paths:
        resolved = path.resolve()
        for name in PROTECTED_FOLDERS:
            folder = home / name
            if resolved == folder or folder in resolved.parents:
                return folder
    return None


def blocked_message(folder: Path) -> str:
    return (
        f"The helper app can't be used: this server runs from {folder}, which macOS doesn't let "
        "the helper read. Move the checkout out of Desktop, Documents and Downloads (e.g. to "
        "~/src), or install from PyPI (`uvx apple-calendar-eventkit-mcp`)."
    )


def relaunch_command(
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    app: Path | None = None,
    paths: Sequence[Path] | None = None,
) -> list[str] | None:
    """The command that re-runs this CLI through the helper app, or None to run as is."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform != "darwin" or env.get(VIA_APP_ENV) or env.get(NO_APP_ENV):
        return None
    launcher = launcher_path(app or default_app_path())
    if not os.access(launcher, os.X_OK):
        return None
    if protected_folder(paths) is not None:
        return None
    return [str(launcher), sys.executable, "-m", "apple_calendar_mcp", *argv]


def helper_installed(app: Path | None = None) -> bool:
    return os.access(launcher_path(app or default_app_path()), os.X_OK)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def app_is_current(app: Path, prebuilt: Traversable) -> bool:
    """Whether ``app`` already is what build_app would write from ``prebuilt``."""
    try:
        return (app / "Contents" / "Info.plist").read_bytes() == info_plist() and launcher_path(
            app
        ).read_bytes() == prebuilt.read_bytes()
    except OSError:
        return False


def ensure_app(
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    app: Path | None = None,
    prebuilt: Traversable | None = None,
    run: Runner = subprocess.run,
) -> Path | None:
    """Install or update the prebuilt helper when INSTALL_APP_ENV asks for it.

    Returns the app if it was (re)written. Rewriting re-signs it, which makes macOS ask
    for access again, so an app that is already current is left alone.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform != "darwin" or not _truthy(env.get(INSTALL_APP_ENV)):
        return None
    if env.get(VIA_APP_ENV) or _truthy(env.get(NO_APP_ENV)) or protected_folder() is not None:
        return None
    prebuilt = prebuilt or prebuilt_launcher()
    app = app or default_app_path()
    if prebuilt is None or app_is_current(app, prebuilt):
        return None
    build_app(app, run, prebuilt=prebuilt)
    return app


def unusable_reason() -> str | None:
    """Why an installed helper app can't run this server, or None."""
    folder = protected_folder()
    return blocked_message(folder) if folder is not None and helper_installed() else None


def relaunch(cmd: list[str]) -> None:  # pragma: no cover - replaces the process
    os.environ[VIA_APP_ENV] = "1"
    os.execv(cmd[0], cmd)
