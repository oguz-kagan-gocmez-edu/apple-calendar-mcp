"""The helper app bundle: Info.plist, build steps and the relaunch decision."""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apple_calendar_mcp import macos_app


def make_launcher(app: Path) -> Path:
    exe = macos_app.launcher_path(app)
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_info_plist_declares_calendar_access() -> None:
    plist = plistlib.loads(macos_app.info_plist())
    assert plist["CFBundleIdentifier"] == macos_app.BUNDLE_ID
    assert plist["CFBundleExecutable"] == macos_app.EXECUTABLE
    assert plist["NSCalendarsFullAccessUsageDescription"]
    assert plist["NSCalendarsUsageDescription"]
    assert plist["LSUIElement"] is True


def test_launcher_source_is_packaged() -> None:
    src = macos_app.launcher_source()
    assert "responsibility_spawnattrs_setdisclaim" in src
    assert "int main" in src


def test_build_app_compiles_and_signs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool) -> None:
        calls.append(cmd)

    app = macos_app.build_app(tmp_path / "Test.app", run=fake_run)
    assert (app / "Contents" / "Info.plist").exists()
    assert not (app / "Contents" / "launcher.c").exists()  # source removed after compiling
    assert calls[0][0] == "/usr/bin/clang"
    assert calls[0][calls[0].index("-o") + 1] == str(macos_app.launcher_path(app))
    assert calls[1] == ["codesign", "--force", "--sign", "-", str(app)]


def test_build_app_without_clang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(macos_app.AppBuildError, match="xcode-select --install"):
        macos_app.build_app(tmp_path / "Test.app")


def test_build_app_reports_compiler_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/clang")

    def failing_run(cmd: list[str], check: bool) -> Any:
        raise subprocess.CalledProcessError(1, cmd)

    with pytest.raises(macos_app.AppBuildError, match="failed"):
        macos_app.build_app(tmp_path / "Test.app", run=failing_run)


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("clang") is None, reason="needs macOS + clang")
def test_build_real_app_and_run_launcher(tmp_path: Path) -> None:
    app = macos_app.build_app(tmp_path / "Real.app")
    out = subprocess.run(
        [str(macos_app.launcher_path(app)), "/bin/echo", "hello"], capture_output=True, text=True, check=True
    )
    assert out.stdout == "hello\n"
    subprocess.run(["codesign", "--verify", str(app)], check=True)


class TestRelaunchCommand:
    def test_relaunches_through_installed_app(self, tmp_path: Path) -> None:
        exe = make_launcher(tmp_path / "A.app")
        cmd = macos_app.relaunch_command(["doctor"], env={}, platform="darwin", app=tmp_path / "A.app")
        assert cmd == [str(exe), sys.executable, "-m", "apple_calendar_mcp", "doctor"]

    def test_not_installed(self, tmp_path: Path) -> None:
        assert macos_app.relaunch_command([], env={}, platform="darwin", app=tmp_path / "A.app") is None

    def test_not_macos(self, tmp_path: Path) -> None:
        make_launcher(tmp_path / "A.app")
        assert macos_app.relaunch_command([], env={}, platform="linux", app=tmp_path / "A.app") is None

    @pytest.mark.parametrize("var", [macos_app.VIA_APP_ENV, macos_app.NO_APP_ENV])
    def test_env_opt_outs(self, tmp_path: Path, var: str) -> None:
        make_launcher(tmp_path / "A.app")
        cmd = macos_app.relaunch_command([], env={var: "1"}, platform="darwin", app=tmp_path / "A.app")
        assert cmd is None
