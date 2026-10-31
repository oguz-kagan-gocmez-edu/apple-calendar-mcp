"""The "Apple Calendar MCP.app" helper that owns the calendar permission.

Since macOS 14, a calendar permission request is refused without a prompt
unless the *responsible* app (the one that started us, e.g. Claude or
Terminal) declares NSCalendarsFullAccessUsageDescription, and neither does.

`apple-calendar-mcp install-app` builds a tiny app bundle that declares it.
Once installed, the CLI re-runs itself through the bundle's launcher, so the
permission belongs to "Apple Calendar MCP" no matter which app starts the
server. See macos_helper/launcher.c for the launcher itself.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__

APP_NAME = "Apple Calendar MCP"
BUNDLE_ID = "io.github.oguz-kagan-gocmez-edu.apple-calendar-mcp"
EXECUTABLE = "apple-calendar-mcp-launcher"
# Set on the relaunched server so it doesn't relaunch again.
VIA_APP_ENV = "APPLE_CALENDAR_MCP_VIA_APP"
# Set to 1 to never use the helper.
NO_APP_ENV = "APPLE_CALENDAR_MCP_NO_APP"

USAGE_TEXT = "Lets Claude read and manage your calendar events through the apple-calendar-mcp server."

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
        }
    )


def launcher_source() -> str:
    return resources.files("apple_calendar_mcp").joinpath("macos_helper/launcher.c").read_text("utf-8")


class AppBuildError(RuntimeError):
    pass


def build_app(app: Path | None = None, run: Runner = subprocess.run) -> Path:
    """Compile the launcher, write Info.plist and ad-hoc sign the bundle."""
    app = app or default_app_path()
    clang = shutil.which("clang")
    if clang is None:
        raise AppBuildError("clang not found. Install the Xcode Command Line Tools: xcode-select --install")
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(info_plist())
    src = contents / "launcher.c"
    src.write_text(launcher_source(), "utf-8")
    try:
        run([clang, "-O2", "-Wall", "-o", str(launcher_path(app)), str(src)], check=True)
        src.unlink()
        run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise AppBuildError(f"building {app} failed: {exc}") from exc
    return app


def relaunch_command(
    argv: Sequence[str],
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    app: Path | None = None,
) -> list[str] | None:
    """The command that re-runs this CLI through the helper app, or None to run as is."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform != "darwin" or env.get(VIA_APP_ENV) or env.get(NO_APP_ENV):
        return None
    launcher = launcher_path(app or default_app_path())
    if not os.access(launcher, os.X_OK):
        return None
    return [str(launcher), sys.executable, "-m", "apple_calendar_mcp", *argv]


def relaunch(cmd: list[str]) -> None:  # pragma: no cover - replaces the process
    os.environ[VIA_APP_ENV] = "1"
    os.execv(cmd[0], cmd)
