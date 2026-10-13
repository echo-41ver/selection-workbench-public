"""Capture loaded code identity once per process; never present current HEAD as an old process version."""
import hashlib
import json
import os
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
            result=subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True,timeout=5)
            if result.returncode==0: commit=result.stdout.strip()
        except (OSError,subprocess.SubprocessError): pass
    if not commit:
        try: commit=json.loads((root/'BUILD_INFO.json').read_text('utf-8')).get('commit')
        except (OSError,ValueError): pass
    digest=hashlib.sha256()
    for directory in ('src','scripts','web'):
        for path in sorted((root/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.mjs','.html','.ps1'):
                digest.update(path.relative_to(root).as_posix().encode());digest.update(path.read_bytes())
    return {'commit':commit,'source_sha256':digest.hexdigest()}


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
