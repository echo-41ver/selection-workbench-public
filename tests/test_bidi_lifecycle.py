import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_owned_bidi_cleanup_and_top_context(tmp_path):
    module=(ROOT/'scripts/temu_bidi_lifecycle.mjs').as_uri()
    import json
    code = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {createOwnedLifecycle,topDocumentUrl} from MODULE;
const file=FILE, calls=[];
const call=async (name,args)=>{ calls.push([name,args]); return {}; };
assert.equal(topDocumentUrl('captcha',{context:'iframe',url:'ordinary'},'top'),'captcha');
assert.equal(topDocumentUrl('captcha',{context:'top',url:'search'},'top'),'search');
await fs.writeFile(file,JSON.stringify({sid:'sid',context:'top',intercepts:['old-owned']}));
const life=createOwnedLifecycle({call,sid:'sid',context:'top',file});
await life.recover();
assert.deepEqual(calls,[['network.removeIntercept',{intercept:'old-owned'}]]);
await life.added({intercept:'current-owned'});
assert.equal(life.owns({intercepts:['someone-else']}),false);
assert.equal(life.owns({intercepts:['current-owned']}),true);
life.track('blocked');life.subscribed({subscription:'my-subscription'});
await life.cleanup();await life.cleanup();
assert.deepEqual(calls.slice(1),[
 ['network.removeIntercept',{intercept:'current-owned'}],
 ['network.continueRequest',{request:'blocked'}],
 ['session.unsubscribe',{subscriptions:['my-subscription']}]
]);
for (const prior of [{sid:'other',context:'top'},{sid:'sid',context:'other'}]) {
 await fs.writeFile(file,JSON.stringify({...prior,intercepts:['unknown']}));
 const count=calls.length;
 await createOwnedLifecycle({call,sid:'sid',context:'top',file}).recover();
 assert.equal(calls.length,count);
}
""".replace('MODULE',json.dumps(module)).replace('FILE',json.dumps(str(tmp_path/'owned.json')))
    result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr


import pytest
from selection_automation import worker_runtime


@pytest.mark.parametrize('cooperative,timeout', [(True,False),(True,True),(False,False)])
def test_collector_stop_waits_before_windows_terminate(tmp_path, monkeypatch, cooperative, timeout):
    events=[]
    class Child:
        returncode=None
        def poll(self): return self.returncode
        def wait(self, timeout=None):
            events.append(('wait',timeout))
            if timeout==15 and should_timeout:
                raise subprocess.TimeoutExpired('node',timeout)
            self.returncode=2
        def terminate(self): events.append(('terminate',None))
        def kill(self): events.append(('kill',None))
    class Lease:
        stop_path=tmp_path/'worker.stop.json'
        checks=0
        def check_stop(self):
            self.checks+=1
            if self.checks>1:
                self.stop_path.write_text('{}')
                raise worker_runtime.WorkerStopRequested('stop')
    should_timeout=timeout
    child=Child();lease=Lease()
    monkeypatch.setattr(worker_runtime.subprocess,'Popen',lambda *a,**k:child)
    command=['node','collector']
    if cooperative: command+=['--stop-file',str(lease.stop_path)]
    with pytest.raises(worker_runtime.WorkerStopRequested):
        worker_runtime.run_collector(command,lease=lease,cwd=tmp_path,stream=None)
    if cooperative and not timeout:
        assert events==[('wait',15)]
    elif cooperative:
        assert events==[('wait',15),('terminate',None),('wait',10)]
    else:
        assert events==[('terminate',None),('wait',10)]
