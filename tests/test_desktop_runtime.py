"""The desktop installer's own Python and install folder are never a bot workspace."""

import sys
from pathlib import Path

from easyagent.store import Store
from easyagent.workspace import bundled_runtime, install_dirs, workspace_forbidden


def test_the_bundled_runtime_is_refused_only_when_it_is_in_use(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    inside = Path(sys.prefix) / "lib"
    monkeypatch.delenv("EASYAGENT_RUNTIME", raising=False)
    assert bundled_runtime() is None
    monkeypatch.setenv("EASYAGENT_RUNTIME", "bundled")
    assert bundled_runtime() == Path(sys.prefix)
    assert workspace_forbidden(store, inside)
    assert workspace_forbidden(store, Path(sys.prefix))
    assert not workspace_forbidden(store, tmp_path / "elsewhere")


def test_the_install_folder_the_window_names_is_refused(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    app = tmp_path / "Programs" / "EasyAgent"
    app.mkdir(parents=True)
    (app / "easyagent-desktop.exe").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EASYAGENT_INSTALL_DIR", str(app))
    assert any(folder == app.resolve() for folder in install_dirs())
    assert workspace_forbidden(store, app / "sub")
    assert not workspace_forbidden(store, tmp_path / "work")
