"""Build the Python runtime that the desktop installers carry.

The desktop window starts `python -m easyagent`. A fresh computer may have no
Python at all, so each installer ships a relocatable CPython from
python-build-standalone with this package and its dependencies installed into
it. The download is pinned by sha256. The result is one tar.gz per target,
unpacked by the window on first start into the per-user app folder.

    python scripts/build_desktop_runtime.py --triple x86_64-unknown-linux-gnu \
        --out desktop/src-tauri/runtime/python-x86_64-unknown-linux-gnu.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "20261009"
PYTHON = "3.12.15"
SHA256 = {
    "aarch64-apple-darwin": "233f8e15255b3e1d1fd47ea5dd5230dfed803f371138e896e54d98c60ae19f6f",
    "x86_64-apple-darwin": "fb3187d95334813b88940db0f1ca57b84cedfd552739c3a5771c3ea0e49988c9",
    "x86_64-pc-windows-msvc": "11ed9f8a6ac64fb1b5a55e40e305a6744a3f77a70863c8f8197a6e4349abc64a",
    "x86_64-unknown-linux-gnu": "ffa8f85f1b56e88b687f08d743d817015524b86a9365fd921243132b80f1a36d",
}
# pip --platform tags, used only when the runtime cannot run on the build machine.
PIP_PLATFORMS = {
    "aarch64-apple-darwin": ["macosx_11_0_arm64", "macosx_11_0_universal2"],
    "x86_64-apple-darwin": ["macosx_10_13_x86_64", "macosx_11_0_x86_64", "macosx_10_9_x86_64", "macosx_10_9_universal2", "macosx_10_12_x86_64", "macosx_10_15_x86_64", "macosx_11_0_universal2", "macosx_12_0_x86_64"],
}
ROOT = Path(__file__).resolve().parents[1]


def url_for(triple: str) -> str:
    name = f"cpython-{PYTHON}+{RELEASE}-{triple}-install_only_stripped.tar.gz"
    return f"https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{name}"


def download(triple: str, folder: Path) -> Path:
    target = folder / f"cpython-{triple}.tar.gz"
    with urllib.request.urlopen(url_for(triple), timeout=300) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != SHA256[triple]:
        raise SystemExit(f"sha256 mismatch for {triple}: {digest}")
    return target


def runtime_python(root: Path, triple: str) -> Path:
    return root / "python.exe" if "windows" in triple else root / "bin" / "python3"


def site_packages(root: Path, triple: str) -> Path:
    if "windows" in triple:
        return root / "Lib" / "site-packages"
    major_minor = ".".join(PYTHON.split(".")[:2])
    return root / "lib" / f"python{major_minor}" / "site-packages"


def runs_here(python: Path) -> bool:
    try:
        return subprocess.run([str(python), "-c", "print(1)"], capture_output=True, timeout=60).returncode == 0
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--triple", required=True, choices=sorted(SHA256))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = download(args.triple, tmp_path)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp_path / "x", filter="tar")
        root = tmp_path / "x" / "python"
        python = runtime_python(root, args.triple)
        native = runs_here(python)
        env = {**os.environ, "PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        wheels = tmp_path / "wheel"
        builder = str(python) if native else sys.executable
        subprocess.check_call([builder, "-m", "pip", "wheel", "--no-deps", "-w", str(wheels), str(ROOT)], env=env)
        wheel = next(wheels.glob("easyagent-*.whl"))
        if native:
            # Wheels only: nothing is compiled from source on the build machine. A
            # dependency without a wheel for this target resolves to its newest
            # version that has one (cryptography has no Intel Mac wheel after 48).
            subprocess.check_call([str(python), "-m", "pip", "install", "--no-warn-script-location", "--only-binary=:all:", str(wheel)], env=env)
            subprocess.check_call([str(python), "-c", "import easyagent, easyagent.app, uvicorn, fastapi; print(easyagent.__version__)"], env=env)
        else:
            command = [
                sys.executable, "-m", "pip", "install", "--no-warn-script-location",
                "--target", str(site_packages(root, args.triple)),
                "--python-version", ".".join(PYTHON.split(".")[:2]),
                "--implementation", "cp", "--abi", f"cp{''.join(PYTHON.split('.')[:2])}",
                "--only-binary=:all:",
            ]
            for tag in PIP_PLATFORMS[args.triple]:
                command += ["--platform", tag]
            subprocess.check_call(command + [str(wheel)], env=env)
        (root / "EASYAGENT_RUNTIME").write_text(f"{wheel.name}\npython {PYTHON}+{RELEASE} {args.triple}\n", encoding="utf-8")
        with tarfile.open(out, "w:gz", compresslevel=6) as tar:
            tar.add(root, arcname="python")
    print(f"{out} {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
