"""Exercise diagnostics on Windows without starting installers or paid services."""
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
pytestmark = pytest.mark.skipif(os.name != 'nt' or not PS.is_file(), reason='Windows PowerShell 5.1 required')


def invoke(tmp_path, body):
    harness = tmp_path / 'harness.ps1'
    harness.write_text("$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n. $env:SUPPORT_SOURCE\n" + body, encoding='utf-8-sig')
    return subprocess.run([str(PS), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(harness)], env={**os.environ, 'SUPPORT_SOURCE': str(ROOT/'scripts/deployment_support.ps1'), 'SUPPORT_ROOT': str(tmp_path)}, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)


def test_real_installer_code_and_chinese_next_step_are_preserved(tmp_path):
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    (runtime/'install-status.json').write_text(json.dumps({'stage': '准备系统工具', 'succeeded': False}, ensure_ascii=False), encoding='utf-8')
    (runtime/'install-failure.json').write_text(json.dumps({'command': {'exit_code': 3, 'exit_code_available': True}, 'message': 'installer stopped'}), encoding='utf-8')
    result = invoke(tmp_path, 'Show-DeploymentFailure -Root $env:SUPPORT_ROOT -NativeExitCode 1 -NoDialog\n')
    assert result.returncode == 0, result.stderr
    data = json.loads((runtime/'deployment-user-failure.json').read_text(encoding='utf-8-sig'))
    assert data['native_exit_code'] == 1
    assert '3' in data['reason'] and '准备系统工具' in data['reason']
    assert '导出诊断' in data['next_step'] and '无需查看日志' in data['next_step']


def test_diagnostic_zip_excludes_private_state_and_redacts_log_secrets(tmp_path):
    runtime = tmp_path/'runtime'
    attempt = runtime/'deployment-entry'/('a'*32)
    attempt.mkdir(parents=True)
    secret = 'TEST_ONLY_PRIVATE_VALUE'
    (attempt/'install.stderr.log').write_text('api_key='+secret+'\n--password '+secret+'\nAuthorization: Bearer '+secret+'\n', encoding='utf-8')
    (runtime/'company.dpapi').write_text(secret, encoding='utf-8')
    (runtime/'company.local.json').write_text(secret, encoding='utf-8')
    (runtime/'install-status.json').write_text('{"stage":"failed","succeeded":false}', encoding='utf-8')
    result = invoke(tmp_path, '$p=Export-DeploymentDiagnostics -Root $env:SUPPORT_ROOT\nWrite-Output $p\n')
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip().splitlines()[-1])
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert 'runtime/install-status.json' in names
        assert any(n.endswith('install.stderr.log') for n in names)
        assert not any('company' in n.lower() for n in names)
        text = '\n'.join(archive.read(n).decode('utf-8') for n in names)
    assert secret not in text
    assert '[REDACTED]' in text
    assert 'succeeded' in text


def test_unconfirmed_worker_never_tells_user_to_repeat_deployment(tmp_path):
    result = invoke(tmp_path, '$d=Get-DeploymentUserFailure -Root $env:SUPPORT_ROOT -NativeExitCode 124\n$d|ConvertTo-Json -Compress\n')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data['native_exit_code'] == 124
    assert '后台安装可能仍在运行' in data['reason']
    assert '请勿重复启动' in data['next_step']


def test_export_does_not_overwrite_existing_diagnostic(tmp_path):
    output = tmp_path/'existing.zip'
    output.write_bytes(b'preserve existing diagnostic')
    result = invoke(tmp_path, "Export-DeploymentDiagnostics -Root $env:SUPPORT_ROOT -OutputPath (Join-Path $env:SUPPORT_ROOT 'existing.zip')\n")
    assert result.returncode != 0
    assert output.read_bytes() == b'preserve existing diagnostic'


def test_runtime_parent_junction_cannot_export_another_directory(tmp_path):
    program = tmp_path/'program'
    outside = tmp_path/'private-state'
    program.mkdir()
    outside.mkdir()
    (outside/'install-status.json').write_text('{"private":"TEST_ONLY_OUTSIDE_VALUE"}', encoding='utf-8')
    junction = program/'runtime'
    made = subprocess.run([os.environ.get('ComSpec', 'cmd.exe'), '/d', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True, timeout=10)
    assert made.returncode == 0, made.stderr
    try:
        result = invoke(tmp_path, "$p=Export-DeploymentDiagnostics -Root (Join-Path $env:SUPPORT_ROOT 'program')\nWrite-Output $p\n")
        assert result.returncode == 0, result.stderr
        with zipfile.ZipFile(Path(result.stdout.strip().splitlines()[-1])) as archive:
            assert 'runtime/install-status.json' not in archive.namelist()
            assert b'TEST_ONLY_OUTSIDE_VALUE' not in b'\n'.join(archive.read(n) for n in archive.namelist())
    finally:
        # rmdir removes only this exact junction, never its target contents.
        junction.rmdir()
    assert (outside/'install-status.json').is_file()


def test_redaction_consumes_escaped_quoted_secret_values(tmp_path):
    marker = 'TEST_ONLY_ESCAPED_SECRET_TAIL'
    records = [json.dumps({field: 'prefix"' + marker, 'stage': 'failed'})
               for field in ('proxy_password', 'api_key', 'cookie', 'access_token')]
    records += [
        '--password "prefix\\"' + marker + '" stage=failed',
        "password='prefix\\'" + marker + "' stage=failed",
        '{"password":"prefix\\"' + marker,
    ]
    records += [header + ': first=' + marker + '; second=' + marker
                for header in ('Cookie', 'Set-Cookie', 'Authorization', 'Proxy-Authorization', 'INFO Cookie')]
    (tmp_path/'records.json').write_text(json.dumps(records), encoding='utf-8')
    result = invoke(tmp_path, "$records=Get-Content -LiteralPath (Join-Path $env:SUPPORT_ROOT 'records.json') -Raw|ConvertFrom-Json\n$out=@($records|ForEach-Object{Protect-DeploymentSupportText $_})\nConvertTo-Json -InputObject $out -Compress\n")
    assert result.returncode == 0, result.stderr
    cleaned = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(cleaned) == len(records)
    assert all(marker not in line and '[REDACTED]' in line for line in cleaned)
    assert all('failed' in line for line in cleaned[:4])
