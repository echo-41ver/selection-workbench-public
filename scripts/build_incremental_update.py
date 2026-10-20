"""Build a code-only update from committed source and a released base inventory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_delivery_packages import committed_files, file_sha256, scan_files
from apply_update import checked_name, DEPENDENCY_FILES, SCHEMA

METADATA = {'BUILD_INFO.json', 'SOURCE_SYNC.json', 'UPDATE_BASE.json'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_files(root, ref, channel):
    commit, files = committed_files(root, ref)
    files = {name: data for name, data in files.items()
             if not name.startswith('deployment-assets/') and name not in METADATA}
    if channel == 'internal':
        files['README.md'] = files['docs/DEPLOYMENT_GUIDE.md']
    for name in files:
        checked_name(name)
    findings = scan_files(files)
    if findings:
        raise ValueError('更新源码包含不允许的内容: ' + json.dumps(findings, ensure_ascii=False))
    return commit, files


def base_inventory(package):
    with zipfile.ZipFile(package) as archive:
        info = json.loads(archive.read('BUILD_INFO.json'))
        names = [i.filename for i in archive.infolist() if not i.is_dir()
                 and not i.filename.startswith('deployment-assets/') and i.filename not in METADATA]
        files = {}
        for name in names:
            if name == 'company.local.json':
                continue  # Never read or distribute an installation's company configuration.
            checked_name(name)
            files[name] = digest(archive.read(name))
    return {'schema': SCHEMA, 'commit': info['commit'], 'channel': info['channel'], 'files': files}


def build(base, root, destination, ref='HEAD'):
    if base.get('schema') != SCHEMA or base.get('channel') not in {'public', 'internal'}:
        raise ValueError('升级基准清单无效')
    for name in base['files']:
        checked_name(name)
    commit, files = source_files(root, ref, base['channel'])
    if commit == base['commit']:
        raise ValueError('源码与已装版本相同，无需创建更新包')
    hashes = {name: digest(data) for name, data in files.items()}
    dependencies = [name for name in DEPENDENCY_FILES if base['files'].get(name) != hashes.get(name)]
    if dependencies:
        raise ValueError('依赖版本已变化，不能只发代码更新包；需先制作配套依赖更新: ' + ', '.join(dependencies))
    changed = {name: data for name, data in files.items() if base['files'].get(name) != hashes[name]}
    deleted = {name: value for name, value in base['files'].items() if name not in files}
    manifest = {'schema': SCHEMA, 'kind': 'code-incremental', 'channel': base['channel'],
                'base_commit': base['commit'], 'target_commit': commit,
                'built_at_utc': datetime.now(timezone.utc).isoformat(),
                'files': [{'path': name, 'before_sha256': base['files'].get(name),
                           'sha256': hashes[name], 'bytes': len(data)} for name, data in sorted(changed.items())],
                'deleted': [{'path': name, 'before_sha256': value} for name, value in sorted(deleted.items())]}
    inventory = {'schema': SCHEMA, 'commit': commit, 'channel': base['channel'], 'files': hashes}
    required = ('update.cmd', 'scripts/apply_update.ps1', 'scripts/apply_update.py')
    if any(name not in files for name in required):
        raise ValueError('目标源码缺少升级入口')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f'selection-workbench-update-{base["commit"][:7]}-to-{commit[:7]}-{base["channel"]}.zip'
    if target.exists():
        raise FileExistsError('已有同名更新包，未覆盖: ' + str(target))
    temporary = target.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in required:
            archive.writestr(name, files[name])
        archive.writestr('update-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('update-base.json', json.dumps(inventory, ensure_ascii=False, indent=2))
        archive.writestr('升级说明.txt', '此包用于已部署的工作台。解压到单独目录，先完成当前任务，再双击 update.cmd，选择原安装目录。\n'
                         '自动备份被更新的文件，升级失败自动回滚。公司配置、运行环境、采集数据和断点保留。\n'
                         '仅支持清单指定的原版本；无需重新运行 deploy.cmd。\n')
        for name, data in changed.items():
            archive.writestr('payload/' + name, data)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise ValueError('更新包校验失败')
    temporary.replace(target)
    report = {'path': str(target.resolve()), 'sha256': file_sha256(target), 'bytes': target.stat().st_size,
              'base_commit': base['commit'], 'target_commit': commit, 'channel': base['channel'],
              'changed_files': len(changed), 'deleted_files': len(deleted), 'contains_company_credentials': False}
    target.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--base-package', type=Path)
    group.add_argument('--base-manifest', type=Path)
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--dist', type=Path, default=ROOT / 'dist/updates')
    args = parser.parse_args()
    base = base_inventory(args.base_package) if args.base_package else json.loads(args.base_manifest.read_text('utf-8'))
    print(json.dumps(build(base, ROOT, args.dist, args.ref), ensure_ascii=False))


if __name__ == '__main__':
    main()
