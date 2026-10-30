"""Repair this installation's locked Node dependencies before frontend collection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .process_lock import ProcessLock


class NodeRuntimeError(RuntimeError):
    pass


PROBE = """const fs=require('fs');let result;
try { const p=require('playwright').chromium.executablePath();
result={status:fs.existsSync(p)?'READY':'BROWSER_MISSING',browser:p,
package:require.resolve('playwright/package.json')};
} catch(e) {result={status:e.code==='MODULE_NOT_FOUND'?'DEPENDENCY_MISSING':'BROKEN'};}
console.log(JSON.stringify(result));"""


def ensure_node_runtime(root, *, run=subprocess.run, which=shutil.which, progress=None):
    root = Path(root).absolute()
    node = which('node')
    if not node:
        raise NodeRuntimeError('缺少 Node.js；请运行 deploy.cmd 自动安装后重试。')

    def record(**fields):
        from .merge_temu_snapshots import atomic_write
        path=root/'runtime/node-dependencies-status.json'
        atomic_write(path,json.dumps({'node':node,'checked_at':time.time(),**fields},ensure_ascii=False))

    def report(message):
        if progress:progress(stage='dependencies',message=message)

    def probe():
        try:
            result = run([node, '-e', PROBE], cwd=root, capture_output=True,
                         text=True, encoding='utf-8', timeout=15,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                return 'BROKEN'
            value=json.loads(result.stdout)
            record(**{k:value[k] for k in ('status','browser','package') if isinstance(value.get(k),str)})
            return value['status']
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            raise NodeRuntimeError('无法检查网页采集依赖；请运行 deploy.cmd 后重试。') from error

    state = probe()
    if state == 'READY':
        report('网页采集浏览器已就绪，继续准备规则（无需安装）')
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
            # Scheduled services do not inherit the terminal's download proxy.
            env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
            if not env.get('HTTPS_PROXY') and not env.get('https_proxy'):
                from .browser_transport import amazon_environment
                try:proxy=amazon_environment(root).get('AMAZON_BROWSER_PROXY','')
                except (OSError,ValueError,RuntimeError):proxy=''
                if proxy.startswith('http://'):
                    env['HTTPS_PROXY']=proxy
                    env.setdefault('HTTP_PROXY',proxy)
            proxy_enabled=bool(env.get('HTTPS_PROXY') or env.get('https_proxy'))
            report(label+('（使用已配置 HTTP 下载代理，最长120秒）' if proxy_enabled else '（最长120秒）'))
            record(status='INSTALLING',step=label,download_proxy=proxy_enabled)
            try:
                result = run(command, cwd=root, capture_output=True, text=True,
                             encoding='utf-8', timeout=120,env=env,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except (OSError, subprocess.TimeoutExpired) as error:
                record(status='ERROR',step=label,error=type(error).__name__,download_proxy=proxy_enabled)
                raise NodeRuntimeError(f'{label}未完成；请检查网络后重试，或运行 deploy.cmd。') from error
            if result.returncode:
                record(status='ERROR',step=label,exit_code=result.returncode,download_proxy=proxy_enabled)
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
        report('网页采集浏览器已就绪，继续准备规则')
        return {'ready': True, 'repaired': True}
