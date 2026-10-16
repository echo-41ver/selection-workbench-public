"""Atomic JSON publication under Windows short-lived sharing conflicts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from selection_automation import merge_temu_snapshots as module


def test_short_destination_lock_retries_and_cleans_temporary(tmp_path,monkeypatch):
    target=tmp_path/'assignments.json'; target.write_text('{"old":true}',encoding='utf-8')
    replace=module.os.replace; attempts=[]
    def transient(source,dest):
        attempts.append(1)
        if len(attempts)<3:
            assert json.loads(target.read_text())=={'old':True}
            raise PermissionError('sharing violation')
        replace(source,dest)
    monkeypatch.setattr(module.os,'replace',transient)
    monkeypatch.setattr(module.time,'sleep',lambda seconds:None)
    module.atomic_write(target,'{"new":true}')
    assert len(attempts)==3
    assert json.loads(target.read_text())=={'new':True}
    assert not list(tmp_path.glob('*.tmp'))


def test_permanent_lock_raises_and_preserves_old_json(tmp_path,monkeypatch):
    target=tmp_path/'assignments.json'; target.write_text('{"old":true}',encoding='utf-8')
    clock=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    def blocked(*args):raise PermissionError('persistent sharing violation')
    monkeypatch.setattr(module.os,'replace',blocked)
    with pytest.raises(PermissionError):module.atomic_write(target,'{"new":true}')
    assert clock[0]==pytest.approx(2.0)
    assert json.loads(target.read_text())=={'old':True}
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.skipif(sys.platform!='win32',reason='Windows real concurrent replace')
def test_four_windows_processes_publish_complete_json(tmp_path):
    target=tmp_path/'assignments.json'; module.atomic_write(target,'{"writer":-1,"payload":"seed"}')
    gate=tmp_path/'start'
    script=r'''
import json,sys,time
from pathlib import Path
from selection_automation.merge_temu_snapshots import atomic_write
path,gate=map(Path,sys.argv[1:3]); writer=int(sys.argv[3])
while not gate.exists():time.sleep(.005)
for iteration in range(30):
 atomic_write(path,json.dumps({'writer':writer,'iteration':iteration,'payload':str(writer)*10000}))
'''
    processes=[subprocess.Popen([sys.executable,'-c',script,str(target),str(gate),str(i)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for i in range(4)]
    try:
        gate.touch(); deadline=time.monotonic()+20; observed=0
        while any(p.poll() is None for p in processes):
            assert time.monotonic()<deadline,'concurrent writers timed out'
            try: value=json.loads(target.read_text(encoding='utf-8'))
            except PermissionError: continue
            if value['writer']!=-1:
                assert value['payload']==str(value['writer'])*10000
                observed+=1
            time.sleep(.002)
        for process in processes:
            stdout,stderr=process.communicate(timeout=2)
            assert process.returncode==0,stderr
        assert observed>0
        value=json.loads(target.read_text(encoding='utf-8'))
        assert value['payload']==str(value['writer'])*10000
        assert not list(tmp_path.glob('*.tmp'))
    finally:
        for process in processes:
            if process.poll() is None:process.kill()
            process.wait(timeout=5)
