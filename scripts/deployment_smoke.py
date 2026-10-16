"""Exercise the installed browser, image model and Excel writer without paid analysis."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.runtime_environment import activate
from selection_automation.merge_temu_snapshots import atomic_write


def installation_token(root):
    root = Path(root).resolve()
    inputs = [str(root).casefold(), os.environ.get('COMPUTERNAME', ''), os.environ.get('USERNAME', ''), sys.version.split()[0]]
    for name in ('BUILD_INFO.json', 'requirements.lock.txt', 'package-lock.json'):
        path = root / name
        if path.is_file():
            inputs.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256('\n'.join(inputs).encode('utf-8')).hexdigest()


def verified(root):
    try:
        report = json.loads((Path(root) / 'runtime/deployment-smoke.json').read_text('utf-8'))
        return report.get('ready') is True and report.get('installation') == installation_token(root)
    except (OSError, ValueError):
        return False


def check_model(root):
    import torch
    from torchvision.models import resnet50
    model_path = Path(root) / 'runtime/models/torch/hub/checkpoints/resnet50-11ad3fa6.pth'
    if not model_path.is_file():
        raise RuntimeError('IMAGE_MODEL_FILE_MISSING')
    torch.set_num_threads(2)
    model = resnet50(weights=None).eval()
    model.load_state_dict(torch.load(model_path, map_location='cpu', weights_only=True))
    with torch.inference_mode():
        result = model(torch.zeros(1, 3, 64, 64))
    if tuple(result.shape) != (1, 1000) or not torch.isfinite(result).all().item():
        raise RuntimeError('IMAGE_MODEL_INFERENCE_FAILED')
    return {'ready': True, 'device': 'cpu', 'output_shape': [1, 1000]}


def check_excel(folder):
    from PIL import Image
    from openpyxl import Workbook, load_workbook
    from openpyxl.drawing.image import Image as ExcelImage
    photo = Path(folder) / 'image.png'
    Image.new('RGB', (32, 32), (30, 90, 150)).save(photo)
    path = Path(folder) / '部署验证.xlsx'
    book = Workbook()
    book.active['A1'] = '部署验证'
    book.active['B1'] = 12.5
    book.active.add_image(ExcelImage(photo), 'C1')
    book.save(path)
    reopened = load_workbook(path)
    ready = reopened.active['A1'].value == '部署验证' and reopened.active['B1'].value == 12.5 and len(reopened.active._images) == 1
    reopened.close()
    if not ready:
        raise RuntimeError('EXCEL_ROUNDTRIP_FAILED')
    return {'ready': True, 'chinese_text': True, 'embedded_image': True}


def check_chromium(root):
    script = r"""const {chromium}=require('playwright');
(async()=>{let browser;try{browser=await chromium.launch({headless:true});
const page=await browser.newPage();await page.setContent('<!doctype html><meta charset="utf-8"><div id="proof">Deployment OK</div>');
if(await page.locator('#proof').innerText()!=='Deployment OK')throw Error('DOM_READ_FAILED');
console.log(JSON.stringify({ready:true,version:browser.version(),dom_read:true}));
}finally{if(browser)await browser.close();}})().catch(()=>process.exit(1));"""
    result = subprocess.run([shutil.which('node') or 'node', '-e', script], cwd=root,
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    if result.returncode:
        raise RuntimeError('CHROMIUM_LAUNCH_FAILED')
    return json.loads(result.stdout)


def check_flower(root):
    from company_setup import ensure_adspower_api, free_block
    from replace_proxyshare_worker_environment import api_call
    from selection_automation.secrets import load
    from selection_automation.adspower_kernel import REQUIRED_KERNEL
    from selection_automation.background_launch import run_background_launcher
    import psutil
    root = Path(root)
    company = load(root / 'runtime/secrets/company.dpapi')
    ensure_adspower_api(company)
    base = company.get('adspower_api_url', 'http://127.0.0.1:50325')
    profile = None
    worker = 'deployment-smoke-' + uuid.uuid4().hex[:12]
    state_folder = root / 'runtime' / worker
    cleanup_errors = []
    result = None
    try:
        created = api_call(base, '/api/v1/user/create', method='POST', body={
            'name': worker, 'group_id': '0', 'user_proxy_config': {'proxy_soft': 'no_proxy'},
            'fingerprint_config': {'browser_kernel_config': dict(REQUIRED_KERNEL), 'automatic_timezone': '1'}}, timeout=30)
        profile = (created.get('data') or {}).get('id')
        if not profile:
            raise RuntimeError('BROWSER_PROFILE_CREATE_UNCONFIRMED')
        opened = api_call(base, '/api/v1/browser/start?' + urlencode({'user_id': profile,
            'ip_tab': '0', 'proxy_detection': '0', 'last_opened_tabs': '0', 'headless': '0'}), timeout=90).get('data') or {}
        driver = opened.get('webdriver')
        if not driver or not Path(driver).is_file():
            raise RuntimeError('FIREFOX_WEBDRIVER_MISSING')
        port = free_block(41000, count=1)[0]
        binding = run_background_launcher([shutil.which('pwsh') or 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(root / 'scripts/bind_flower_worker.ps1'), '-WorkerName', worker,
            '-ProfileId', str(profile), '-WebDriverPort', str(port), '-GeckoDriverPath', str(driver), '-WaitSeconds', '120'],
            cwd=root, timeout=190)
        if binding.returncode:
            raise RuntimeError('FIREFOX_BINDING_FAILED')
        sid = (state_folder / 'flower_session_id.txt').read_text('utf-8').strip()
        transport = build_opener(ProxyHandler({}))
        address = f'http://127.0.0.1:{port}/session/{sid}'
        def request(suffix, body):
            call = Request(address + suffix, data=json.dumps(body).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST')
            with transport.open(call, timeout=15) as response:
                return json.load(response).get('value')
        marker = uuid.uuid4().hex
        request('/url', {'url': 'data:text/html,<title>Deployment</title><div id="proof">' + marker + '</div>'})
        deadline=time.monotonic()+15
        observed=None
        while time.monotonic()<deadline:
            observed = request('/execute/sync', {'script': "return document.getElementById('proof')?.textContent", 'args': []})
            if observed==marker:break
            time.sleep(.25)
        if observed != marker:
            raise RuntimeError('FIREFOX_DOM_READ_FAILED')
        record = json.loads((state_folder / 'binding.json').read_text('utf-8-sig'))
        result = {'ready': True, 'kernel': dict(REQUIRED_KERNEL), 'webdriver': True,
            'bidi': bool((state_folder / 'flower_bidi_url.txt').read_text('utf-8').strip()),
            'foreground': record.get('foreground', {}).get('activated') is True, 'dom_read': True}
        if not result['bidi'] or not result['foreground']:
            raise RuntimeError('FIREFOX_BINDING_PROOF_INCOMPLETE')
    finally:
        if profile:
            for endpoint, method, body in [('/api/v1/browser/stop?' + urlencode({'user_id': profile}), 'GET', None),
                    ('/api/v1/user/delete', 'POST', {'user_ids': [profile]})]:
                try:
                    api_call(base, endpoint, method=method, body=body, timeout=20)
                except RuntimeError:
                    cleanup_errors.append('PROFILE_CLEANUP_FAILED')
        try:
            record = json.loads((state_folder / 'driver.json').read_text('utf-8-sig'))
            process = psutil.Process(record['pid'])
            ticks = round(process.create_time() * 10_000_000) + 621355968000000000
            if abs(ticks - record['started_ticks']) < 20_000_000 and Path(process.exe()).name.lower() == 'geckodriver.exe':
                process.terminate()
                process.wait(timeout=5)
        except (OSError, ValueError, KeyError, psutil.Error):
            pass
    if cleanup_errors:
        raise RuntimeError('DEPLOYMENT_PROFILE_CLEANUP_INCOMPLETE')
    return result


def run(root=ROOT):
    root = Path(root).resolve()
    activate(root)
    report = {'schema': 1, 'ready': False, 'installation': installation_token(root), 'checked_at': time.time(),
              'paid_analysis_requests': 0, 'checks': {}}
    destination = root / 'runtime/deployment-smoke.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = 'image_model'
    try:
        report['checks'][stage] = check_model(root)
        with tempfile.TemporaryDirectory(prefix='deployment-', dir=destination.parent) as folder:
            stage = 'excel'
            report['checks'][stage] = check_excel(folder)
        stage = 'chromium'
        report['checks'][stage] = check_chromium(root)
        stage = 'flower_binding'
        report['checks'][stage] = check_flower(root)
        report['ready'] = True
    except Exception as error:
        report.update(failed_stage=stage, error_type=type(error).__name__)
        if isinstance(error, RuntimeError) and str(error).isupper() and str(error).replace('_', '').isalnum():
            report['error_code'] = str(error)
    atomic_write(destination, json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    report = run()
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report['ready'] else 1)
