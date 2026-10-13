"""Repair this installation's locked Node dependencies before frontend collection."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from .process_lock import ProcessLock


class NodeRuntimeError(RuntimeError):
    pass


PROBE = """const fs=require('fs');let result;
try { const p=require('playwright').chromium.executablePath();
result={status:fs.existsSync(p)?'READY':'BROWSER_MISSING'};
} catch(e) {result={status:e.code==='MODULE_NOT_FOUND'?'DEPENDENCY_MISSING':'BROKEN'};}
console.log(JSON.stringify(result));"""


def ensure_node_runtime(root, *, run=subprocess.run, which=shutil.which):
    root = Path(root).absolute()
    node = which('node')
    if not node:
        raise NodeRuntimeError('缺少 Node.js；请运行 deploy.cmd 自动安装后重试。')

    def probe():
        try:
            result = run([node, '-e', PROBE], cwd=root, capture_output=True,
                         text=True, encoding='utf-8', timeout=15)
            if result.returncode:
                return 'BROKEN'
            return json.loads(result.stdout)['status']
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            raise NodeRuntimeError('无法检查网页采集依赖；请运行 deploy.cmd 后重试。') from error

    state = probe()
    if state == 'READY':
        return {'ready': True, 'repaired': False}
    if state not in {'DEPENDENCY_MISSING', 'BROWSER_MISSING'}:
        raise NodeRuntimeError('网页采集依赖损坏；请运行 deploy.cmd 修复后重试。')
    (root/'runtime').mkdir(parents=True, exist_ok=True)
    with ProcessLock(root/'runtime/node-dependencies.lock'):
        # Another launcher may have completed the repair while this one waited.
        state = probe()
        if state == 'READY':
            return {'ready': True, 'repaired': False}

        def install(command, label):
            try:
                result = run(command, cwd=root, capture_output=True, text=True,
                             encoding='utf-8', timeout=300)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise NodeRuntimeError(f'{label}未完成；请检查网络后重试，或运行 deploy.cmd。') from error
            if result.returncode:
                raise NodeRuntimeError(f'{label}失败（退出码 {result.returncode}）；请检查网络后重试，或运行 deploy.cmd。')

        if state == 'DEPENDENCY_MISSING':
            npm = Path(node).parent/'node_modules/npm/bin/npm-cli.js'
            if not npm.is_file() or not (root/'package-lock.json').is_file():
                raise NodeRuntimeError('缺少 npm 或项目依赖锁文件；请运行 deploy.cmd 完整安装后重试。')
            install([node, str(npm), 'ci', '--no-audit', '--no-fund'], '安装锁定的网页采集依赖')
            state = probe()
        if state == 'BROWSER_MISSING':
            install([node, str(root/'node_modules/playwright/cli.js'), 'install', 'chromium'], '安装网页采集浏览器')
        if probe() != 'READY':
            raise NodeRuntimeError('网页采集依赖修复后仍未就绪；请运行 deploy.cmd 后重试。')
        return {'ready': True, 'repaired': True}
