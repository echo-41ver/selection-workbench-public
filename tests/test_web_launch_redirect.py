"""Exercise local launch redirects using fixture scripts, without starting a service."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows launch entry')


def fixture_root(tmp_path, name):
    root = tmp_path / name
    (root / 'scripts').mkdir(parents=True)
    for source in ['start-web.ps1', 'restart-web.ps1', 'scripts/resolve_web_launch.ps1']:
        shutil.copy2(ROOT / source, root / source)
    return root


def run_script(script, *args):
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                           'Bypass', '-File', str(script), *args], capture_output=True, timeout=15)


def config(root, target):
    (root / 'start-web.local.json').write_text(json.dumps({'target_root': str(target)}), encoding='utf-8')


@pytest.mark.parametrize('entry,args,expected', [
    ('start-web.ps1', ['-Port', '9876', '-NoBrowser'], {'port': 9876, 'no_browser': True}),
    ('restart-web.ps1', ['--port', '9876', '--keep-tasks'], ['--port', '9876', '--keep-tasks']),
])
def test_redirect_forwards_arguments_and_exit_code(tmp_path, entry, args, expected):
    source = fixture_root(tmp_path, 'old copy')
    target = fixture_root(tmp_path, 'current copy')
    config(source, '../current copy')
    script = ("param([int]$Port,[switch]$NoBrowser)\n"
              "@{port=$Port;no_browser=[bool]$NoBrowser}|ConvertTo-Json -Compress\n"
              if entry == 'start-web.ps1' else 'ConvertTo-Json -InputObject @($args) -Compress\n')
    (target / entry).write_text(script + 'exit 23\n', encoding='utf-8-sig')
    result = run_script(source / entry, *args)
    assert result.returncode == 23, result.stderr.decode(errors='replace')
    assert json.loads(result.stdout.decode('utf-8-sig')) == expected


@pytest.mark.parametrize('bad_target', ['self', 'cycle', 'missing'])
def test_invalid_redirect_fails_before_launch(tmp_path, bad_target):
    source = fixture_root(tmp_path, 'source')
    target = fixture_root(tmp_path, 'target')
    if bad_target == 'self':
        config(source, '.')
    elif bad_target == 'cycle':
        config(source, '../target')
        config(target, '../source')
    else:
        config(source, '../does-not-exist')
    result = run_script(source / 'start-web.ps1', '-NoBrowser')
    assert result.returncode != 0
    assert b'Local web launch' in result.stderr


def test_without_local_config_uses_own_root(tmp_path):
    source = fixture_root(tmp_path, 'standalone')
    probe = source / 'probe.ps1'
    probe.write_text(". (Join-Path $PSScriptRoot 'scripts/resolve_web_launch.ps1')\n"
                     "Resolve-WebLaunchRoot -SourceRoot $PSScriptRoot -EntryName 'start-web.ps1'\n",
                     encoding='utf-8-sig')
    result = run_script(probe)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert result.stdout.decode('utf-8-sig').strip() == str(source)
