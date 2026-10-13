"""Check that this installation can start the complete local selection workflow."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def inspect(root: Path = ROOT) -> dict:
    missing = []
    compatibility=None
    network_preflight=None
    adspower_path=None
    if sys.version_info[:2] != (3, 12):
        missing.append("需要 Python 3.12；请重新运行 deploy.cmd")

    dependencies = ("torch", "torchvision", "openai", "psutil", "xlsxwriter", "openpyxl", "PIL", "geekbi_temu_mcp")
    absent = [name for name in dependencies if importlib.util.find_spec(name) is None]
    if absent:
        missing.append("Python 依赖缺失：" + ", ".join(absent))

    node = shutil.which("node")
    if not node:
        missing.append("Node.js 未就绪；请重新运行 deploy.cmd")
    else:
        try:
            probe = subprocess.run(
                [node, "-e", "const fs=require('fs');const p=require('playwright').chromium.executablePath();process.exit(fs.existsSync(p)?0:1)"],
                cwd=root, capture_output=True, timeout=15,
            )
            if probe.returncode:
                missing.append("Playwright Chromium 未就绪；请重新运行 deploy.cmd")
        except (OSError, subprocess.TimeoutExpired):
            missing.append("无法检查 Playwright Chromium")

    if not shutil.which("pwsh"):
        missing.append("PowerShell 7 未就绪；请重新运行 deploy.cmd")

    company_path = root / "runtime" / "secrets" / "company.dpapi"
    if not company_path.is_file():
        missing.append("缺少公司 AdsPower 和住宅代理配置；请让公司管理员提供 company.local.json，再运行 deploy.cmd -CompanyConfig 文件路径")
    else:
        try:
            from company_setup import ensure_adspower_api, find_adspower_executable
            from selection_automation.secrets import load

            company = load(company_path)
            proxy = company.get("proxy") if isinstance(company, dict) else None
            if not isinstance(proxy, dict) or not all(str(proxy.get(key, "")).strip() and not str(proxy[key]).startswith("REPLACE_WITH_") for key in ("host", "port", "username", "password")):
                missing.append("公司住宅代理配置不完整，请重新导入公司配置")
            if not isinstance(company, dict) or not str(company.get("adspower_api_key", "")).strip() or str(company["adspower_api_key"]).startswith("REPLACE_WITH_"):
                missing.append("公司 AdsPower API Key 缺失，请重新导入公司配置")
            from selection_automation.network_readiness import select_transport,NetworkNotReady
            try:
                _,network_preflight=select_transport(company)
                network_preflight['route_applied']=False
                network_preflight['message']=network_preflight['message'].replace('已检测并选用','检测到可用').replace('已明确选择','配置为')
            except NetworkNotReady as error:
                network_preflight=error.diagnostic
                missing.append(str(error))
            adspower_path=find_adspower_executable(company if isinstance(company,dict) else {})
            if not adspower_path:
                missing.append("AdsPower 程序未找到，请重新运行 deploy.cmd")
            elif not missing:
                try:
                    ensure_adspower_api(company)
                    from selection_automation.adspower_kernel import inventory
                    from replace_proxyshare_worker_environment import api_call
                    kernel=inventory(company.get('adspower_api_url','http://127.0.0.1:50325'),api_call)
                    if not kernel['installed']: missing.append('AdsPower FlowerBrowser Firefox144 missing; rerun deployment kernel preparation')
                except RuntimeError as kernel_error:
                    missing.append("AdsPower kernel/API preparation failed: "+type(kernel_error).__name__)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            missing.append("公司配置不能在当前 Windows 用户下读取或格式无效：" + type(exc).__name__)

    if (root/'config/runtime_baseline.json').exists():
        from selection_automation.environment_check import inspect_environment
        from selection_automation.merge_temu_snapshots import atomic_write
        compatibility=inspect_environment(root,adspower_path)
        missing.extend(compatibility['missing'])
        atomic_write(root/'runtime/deployment-compatibility.json',json.dumps(compatibility,ensure_ascii=False,indent=2))
    result={"ready":not missing,"missing":missing,"network_preflight":network_preflight}
    if compatibility is not None: result['compatibility']=compatibility
    return result


def main() -> int:
    result = inspect()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
