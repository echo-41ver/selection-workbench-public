"""Build two code-only deliveries from a committed Git tree; never read local secrets."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DOCS = {"docs/DEPLOYMENT_GUIDE.md", "docs/DELIVERY_BASELINE.md", "docs/COLLECTION_INTERFACE.md", "docs/deployment-parity.md"}
PRIVATE_PARTS = {".git", ".githooks", ".venv", "node_modules", "runtime", "runs", "dist", "work", "logs", "__pycache__", ".deployment-backup", ".pytest_cache"}
ASSET_ROOT = "deployment-assets"
SECRET_TOKEN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b")
AUTH_URL = re.compile(r"https?://[^\s/:\"']+:[^\s/@\"']+@[^\s\"'<>]+")
SECRET_FIELDS = {"password", "proxy_password", "api_key", "adspower_api_key", "deepseek_api_key", "access_token", "client_secret"}


def included(name: str) -> bool:
    path = PurePosixPath(name)
    is_bundled_asset = path.parts and path.parts[0].lower() == ASSET_ROOT.lower()
    if path.is_absolute() or ".." in path.parts or (not is_bundled_asset and any(p.lower() in PRIVATE_PARTS for p in path.parts)):
        return False
    if any(p.lower().startswith(".env") for p in path.parts):
        return False
    if path.name.lower() in {"company.local.json", "start-web.local.json"} or name == "config/sessions.json":
        return False
    if path.suffix.lower() in {".dpapi", ".sqlite", ".sqlite3", ".db", ".log", ".xlsx", ".zip", ".credential.xml"}:
        return False
    if name.startswith("docs/") and name not in ALLOWED_DOCS:
        return False
    if name == "config/categories/shower_curtain_local_union_20260922.json":
        return False
    return True


def is_placeholder(value):
    return not isinstance(value, str) or not value.strip() or any(
        marker in value.upper() for marker in ("REPLACE_", "TEST_ONLY", "EXAMPLE", "PLACEHOLDER", "${", "<"))


def scan_files(files: dict[str, bytes]) -> list[dict]:
    """Diagnostics contain filenames/rule names only, never the matched credential."""
    issues = []
    def scan_json(value, name, prefix=""):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in SECRET_FIELDS and isinstance(item, str) and not is_placeholder(item):
                    issues.append({"file": name, "rule": "literal-credential", "field": prefix + key})
                scan_json(item, name, prefix + key + ".")
        elif isinstance(value, list):
            for item in value:
                scan_json(item, name, prefix)
    for name, data in files.items():
        if not included(name) and name not in {"BUILD_INFO.json", "README.md"}:
            issues.append({"file": name, "rule": "private-path"})
        try:
            value = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        if SECRET_TOKEN.search(value):
            issues.append({"file": name, "rule": "credential-token"})
        urls = AUTH_URL.findall(value)
        # Exact deliberately invalid user:secret/pass URLs are security-test fixtures.
        unsafe_urls = [url for url in urls if not (name.startswith('tests/') and (
            re.match(r'https?://user:(?:secret|pass)@(?:www\.temu\.com|www\.amazon\.com|m\.media-amazon\.com|fixture)(?:[/\s]|$)', url) or
            re.match(r'https?://TEST_ONLY_USER:TEST_ONLY_PASSWORD@proxy\.example', url)))]
        if unsafe_urls:
            issues.append({"file": name, "rule": "credential-in-url"})
        if name.endswith(".json"):
            try:
                scan_json(json.loads(value), name)
            except ValueError:
                issues.append({"file": name, "rule": "invalid-json"})
    return issues


def committed_files(root: Path, ref="HEAD"):
    commit = subprocess.check_output(["git", "rev-parse", "--verify", ref], cwd=root, text=True).strip()
    raw = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=root)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist()
                 if not item.is_dir() and included(item.filename)}
    return commit, files


def bundled_assets(root: Path) -> dict[str, bytes]:
    """Add generated, non-secret installers assets to both delivery archives."""
    source = root / ASSET_ROOT
    if not source.is_dir():
        return {}
    result = {}
    for path in source.rglob('*'):
        if path.is_file():
            relative = path.relative_to(source).as_posix()
            if relative.lower().endswith(('.credential.xml', '.dpapi')):
                raise RuntimeError(f'部署资产禁止包含配置或密钥文件: {relative}')
            result[f'{ASSET_ROOT}/{relative}'] = path.read_bytes()
    return result


def write_zip(path: Path, files: dict[str, bytes]):
    temporary = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    temporary.replace(path)


def audit_history_paths(root: Path):
    """List sensitive path findings in history without reading any secret object."""
    raw = subprocess.check_output(["git", "log", "--all", "--format=", "--name-only"], cwd=root, text=True, encoding="utf-8")
    names = sorted(set(raw.splitlines()))
    patterns = ("company.local.json", ".dpapi", ".credential.xml", "auth.json", ".env")
    return [name for name in names if any(marker in name.lower() for marker in patterns)]


def build(root: Path, dist: Path, ref="HEAD"):
    commit, files = committed_files(root, ref)
    assets = bundled_assets(root)
    files.update(assets)
    required = {"deploy.cmd", "install.ps1", "start-web.cmd", "web/index.html",
                "scripts/verify_deployment.py", "scripts/package_company_config.ps1",
                "scripts/deployment_guard.ps1", "scripts/prepare_deployment_assets.py", "config/company.example.json"}
    missing = sorted(required - files.keys())
    if missing:
        raise RuntimeError("部署源缺少文件: " + ", ".join(missing))
    findings = scan_files(files)
    if findings:
        raise RuntimeError("源码检查未通过: " + json.dumps(findings, ensure_ascii=False))
    dist.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    names = {"public": f"选品工作台-{commit[:7]}-GitHub展示版.zip",
             "internal": f"选品工作台-{commit[:7]}-内部运行版.zip"}
    report = {"commit": commit, "built_at_utc": now, "contains_company_credentials": False,
              "bundled_assets": {"present": bool(assets), "files": len(assets), "bytes": sum(map(len, assets.values()))},
              "baseline": "11194c8", "matching_policy": "category-identity-policy-v2-effects",
              "matching_source_commit": "01e86d8",
              "disabled_experiments": ["four-stage-verification", "quadrants-overlap-v1", "default-reference-examples", "luna-production-switch"],
              "history_sensitive_paths": audit_history_paths(root), "packages": {}}
    for channel, name in names.items():
        content = dict(files)
        content["BUILD_INFO.json"] = json.dumps({"commit": commit, "built_at_utc": now,
            "channel": channel, "contains_company_credentials": False}, ensure_ascii=False, indent=2).encode()
        readme = "docs/DEPLOYMENT_GUIDE.md" if channel == "internal" else "README.md"
        content["README.md"] = files[readme]
        path = dist / name
        if path.exists():
            # Preserve previously generated archives even when rebuilding the same commit.
            path.rename(path.with_name(path.stem + "-旧-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".zip"))
        write_zip(path, content)
        with zipfile.ZipFile(path) as archive:
            verified = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        issues = scan_files(verified)
        if issues:
            raise RuntimeError("成品 ZIP 检查未通过: " + json.dumps(issues, ensure_ascii=False))
        report["packages"][channel] = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                         "files": len(verified), "secret_scan_findings": issues}
    manifest = {**report, "public_package": names["public"], "public_sha256": report["packages"]["public"]["sha256"],
                "internal_package": names["internal"], "internal_sha256": report["packages"]["internal"]["sha256"]}
    previous = dist / "最新部署包.json"
    if previous.exists():
        previous.rename(dist / ("部署包清单-旧-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json"))
    previous.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # A human-visible native entry. No actual company config is opened here.
    (dist / "package_company_config.ps1").write_bytes(files["scripts/package_company_config.ps1"])
    (dist / "双击生成含公司配置的内部包.cmd").write_bytes(
        b'@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0package_company_config.ps1"\r\nif errorlevel 1 pause\r\n')
    # Local-only shortcut; excluded from both packages. Recipients use their own deploy.cmd.
    (dist / '启动本机稳定工作台.cmd').write_text(
        '@echo off\nchcp 65001 >nul\ncall "' + str(root / 'restart-web.cmd') + '"\n'
        'if errorlevel 1 exit /b 1\nstart "" "http://127.0.0.1:8765/"\n', encoding='utf-8')
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    try:
        result = build(ROOT, args.dist, args.ref)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(str(exc))
        raise SystemExit(1)
