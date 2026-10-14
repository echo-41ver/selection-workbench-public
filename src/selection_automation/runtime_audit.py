"""Capture loaded code identity once per process; never present current HEAD as an old process version."""
import hashlib
import json
import os
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from .merge_temu_snapshots import atomic_write


def config_hash(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def disk_version(root):
    """Read only this installation, never search other working trees for a version."""
    root=Path(root); commit=None
    if (root/'.git').exists():
        try:
            commit=read_git_head(root)
        except (OSError,ValueError): pass
    if not commit:
        try: commit=json.loads((root/'BUILD_INFO.json').read_text('utf-8')).get('commit')
        except (OSError,ValueError): pass
    digest=hashlib.sha256()
    for directory in ('src','scripts','web'):
        for path in sorted((root/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.mjs','.html','.ps1'):
                digest.update(path.relative_to(root).as_posix().encode());digest.update(path.read_bytes())
    return {'commit':commit,'source_sha256':digest.hexdigest()}


def read_git_head(root):
    """Runtime checks must not wait for Git subprocess pipes on Windows."""
    metadata=Path(root)/'.git'
    if metadata.is_file():
        pointer=metadata.read_text('utf-8').strip()
        if not pointer.startswith('gitdir: '):raise ValueError('invalid gitdir')
        metadata=(Path(root)/pointer[8:]).resolve()
    head=(metadata/'HEAD').read_text('utf-8').strip()
    if re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}',head):return head.lower()
    if not head.startswith('ref: '):raise ValueError('invalid HEAD')
    ref=head[5:]
    if not ref.startswith('refs/') or '..' in ref or any(c.isspace() for c in ref):raise ValueError('invalid ref')
    common=metadata
    if (metadata/'commondir').is_file():common=(metadata/(metadata/'commondir').read_text('utf-8').strip()).resolve()
    for folder in (metadata,common):
        loose=folder/ref
        if loose.is_file():
            value=loose.read_text('utf-8').strip()
            if re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}',value):return value.lower()
    packed=common/'packed-refs'
    if packed.is_file():
        for line in packed.read_text('utf-8').splitlines():
            fields=line.split()
            if len(fields)==2 and fields[1]==ref and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}',fields[0]):return fields[0].lower()
    raise ValueError('HEAD ref not found')


@lru_cache(maxsize=4)
def process_version(root):
    return {**disk_version(root),'loaded_at':time.time(),'pid':os.getpid()}


def record_start(root,run,component,config,worker=None,version=None):
    payload={**(version if version is not None else process_version(str(root))),'component':component,'config_sha256':config_hash(config)}
    target=Path(run)/('temu/workers/'+worker if worker else '')/'runtime_version.json'
    atomic_write(target,json.dumps(payload,ensure_ascii=False,indent=2));return payload


def recovery_event(run,worker,event,**fields):
    path=Path(run)/'temu/workers'/worker/'recovery_events.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
    # Each lane's collector/replacer is exclusively leased. Never include credentials.
    with path.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'at':time.time(),'event':event,'worker':worker,**fields},ensure_ascii=False)+'\n')
