"""Prepare reproducible Windows x64 / CPython 3.12 deployment dependencies."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
GEEKBI = "geekbi-temu-research-mcp @ https://github.com/geekbi/geekbi-temu-research-mcp/archive/d3536f77db76b70ae9c6e69bf499f47ad972850e.zip"
MODEL_URL = "https://download.pytorch.org/models/resnet50-11ad3fa6.pth"
MODEL_NAME = "resnet50-11ad3fa6.pth"


def run(command: list[str], *, cwd: Path) -> None:
    print("RUN", Path(command[0]).name, *command[1:])
    subprocess.run(command, cwd=cwd, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_codex(root: Path) -> dict:
    sys.path.insert(0,str(root/'src'))
    from selection_automation.local_agents import find_executable
    source=find_executable(root,'codex')
    if not source or Path(source).suffix.lower()!='.exe':
        raise RuntimeError('Native Codex CLI is required to reproduce this Windows installation')
    source=Path(source)
    version=subprocess.run([str(source),'--version'],capture_output=True,text=True,check=True).stdout.strip()
    target=root/'deployment-assets/tools/codex';target.mkdir(parents=True,exist_ok=True)
    for name in ('codex.exe','codex-code-mode-host.exe','codex-command-runner.exe','codex-windows-sandbox-setup.exe'):
        path=source.parent/name
        if not path.is_file():raise RuntimeError('Codex distribution helper missing: '+name)
        if path.resolve()!=(target/name).resolve():shutil.copy2(path,target/name)
    return {'version':version,'credential_cache_included':False,'entry':'tools/codex/codex.exe'}


def prepare(root: Path, python: Path) -> dict:
    assets = root / "deployment-assets"
    wheelhouse = assets / "wheelhouse"
    modules = assets / "node_modules"
    model = assets / "models" / MODEL_NAME
    wheelhouse.mkdir(parents=True, exist_ok=True)
    modules.mkdir(parents=True, exist_ok=True)
    model.parent.mkdir(parents=True, exist_ok=True)
    codex=bundle_codex(root)

    lock = (root / "requirements.lock.txt").read_text(encoding="utf-8-sig")
    if lock.count(GEEKBI) != 1:
        raise RuntimeError("The pinned GeekBI source archive is missing or ambiguous in requirements.lock.txt")

    with tempfile.TemporaryDirectory(prefix="selection-deployment-assets-") as temporary:
        temporary_root = Path(temporary)
        requirements = temporary_root / "requirements-assets.txt"
        requirements.write_text(lock.replace(GEEKBI + "\n", ""), encoding="utf-8")
        run([str(python), "-m", "pip", "download", "--only-binary=:all:", "--requirement", str(requirements),
             "--dest", str(wheelhouse), "--extra-index-url", "https://download.pytorch.org/whl/cpu",
             "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312"], cwd=root)
        run([str(python), "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheelhouse),
             "pip==25.0.1", "--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312"], cwd=root)
        run([str(python), "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), GEEKBI], cwd=root)

    for name in ("playwright", "playwright-core"):
        source = root / "node_modules" / name
        if not (source / "package.json").is_file():
            raise RuntimeError(f"Locked npm dependency is not installed: {name}")
        target = modules / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

    if not model.is_file():
        print(f"Downloading {MODEL_URL}")
        with urlopen(MODEL_URL, timeout=60) as response, model.open("wb") as output:
            shutil.copyfileobj(response, output)

    files = sorted(path for path in assets.rglob("*") if path.is_file() and path.name != "manifest.json")
    manifest = {
        "target": {"os": "Windows x64", "python": "CPython 3.12.10", "node": "24.19.0", "powershell": "7.6.5", "adspower": "8.7.23", "adspower_firefox_kernel": "153", "node_package": "package-lock.json"},
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "agent_tools":{'codex':codex},
        "files": {path.relative_to(assets).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256(path)} for path in files},
    }
    (assets / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["file_count"] = len(files)
    manifest["total_bytes"] = sum(path.stat().st_size for path in files)
    print(json.dumps({"assets": str(assets), "file_count": len(files), "total_bytes": manifest["total_bytes"]}, ensure_ascii=False, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=ROOT / ".venv" / "Scripts" / "python.exe")
    args = parser.parse_args()
    try:
        prepare(args.root.resolve(), args.python.resolve())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Deployment asset preparation failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
