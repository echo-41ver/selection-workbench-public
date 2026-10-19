import shutil
import importlib.util,pathlib,sys,unittest,tempfile,subprocess,socket,time,json,os
from unittest.mock import patch
BASE=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE/'src'))
def module(name,path):
 s=importlib.util.spec_from_file_location(name,BASE/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
r=module('recovery','src/selection_automation/recovery_policy.py');w=module('selection_automation.worker_runtime_probe','src/selection_automation/worker_runtime.py')
class Tests(unittest.TestCase):
 def test_explicit_manual_never_rotates(self):
  p={'status':'STOPPED_PROXY_OR_NETWORK_FAILURE','manual_action_required':True,'automatic_retry':False}
  self.assertFalse(w.transport_recovery_allowed(p));self.assertIsNone(r.recovery_kind(p['status'],p,True))
 def test_target_refusal_uses_access_cooldown(self):
  for status in ['STOPPED_HTTP_403','STOPPED_ACCESS_DENIED']:
   self.assertEqual(r.recovery_kind(status,{},True),'ACCESS')
   self.assertGreater(r.site_access_wait('ACCESS',{}),0)
 def test_confirmed_login_redirect_recovers_without_treating_captcha_as_login(self):
  for status in ['STOPPED_HTTP_403','STOPPED_ACCESS_DENIED']:
   self.assertEqual(r.recovery_kind(status,{'confirmed_login_redirect':True},True),'LOGIN')
   self.assertEqual(r.recovery_kind(status,{'confirmed_login_redirect':True,'manual_action_required':True},True),'LOGIN')
  self.assertIsNone(r.recovery_kind('WAITING_CAPTCHA',{'confirmed_login_redirect':True},True))
 def test_budget_is_bounded(self):
  for kind in ['TRANSPORT','LOGIN','INITIAL_PRODUCTS','CONTROL']:
   self.assertFalse(r.recovery_budget({'attempts_without_progress':3,'unique_goods_before':0},0,'profile',kind,limit=3)[0])
  self.assertTrue(r.recovery_budget({'attempts_without_progress':3,'unique_goods_before':0},1,'profile','TRANSPORT')[0])
 def test_foreign_process_not_owned(self):
  root=pathlib.Path.cwd();args=['node',str(root/'scripts/temu_flower_collect.mjs'),'--output',str(root/'runs/task/temu/workers/worker-01-new/query')]
  self.assertTrue(w.collector_belongs_to_worker(args,root,'worker-01-new','task'))
  self.assertFalse(w.collector_belongs_to_worker(args,root,'worker-02-new','task'))
  self.assertFalse(w.collector_belongs_to_worker(['node','-e','temu_flower_collect.mjs worker-01-new'],root,'worker-01-new','task'))
 def test_owned_stop_only(self):
  import psutil
  root=pathlib.Path.cwd()
  class P:
   def __init__(self,worker):self.info={'name':'node.exe','cmdline':['node',str(root/'scripts/temu_flower_collect.mjs'),'--output',str(root/f'runs/task/temu/workers/{worker}/query')]};self.stopped=False
   def terminate(self):self.stopped=True
  own=P('worker-01-new');foreign=P('worker-02-new')
  with patch.object(psutil,'process_iter',return_value=[own,foreign]),patch.object(psutil,'wait_procs',return_value=([own],[])):
   self.assertEqual(w.stop_owned_collectors(root,'worker-01-new','task')['stopped_owned_collectors'],1)
  self.assertTrue(own.stopped);self.assertFalse(foreign.stopped)
 def test_local_denial_provenance_without_upstream(self):
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  with tempfile.TemporaryDirectory() as folder:
   env=dict(os.environ,TEMU_CHAIN_LISTEN_PORT=str(port),TEMU_CHAIN_WORKER='1',TEMU_CHAIN_ACTIVITY_DIR=folder,PROXYSHARE_PROXY_HOST='127.0.0.1',PROXYSHARE_PROXY_PORT='1',PROXYSHARE_PROXY_USER='TEST_ONLY',PROXYSHARE_PROXY_PASSWORD='TEST_ONLY')
   p=subprocess.Popen([str(BASE/'runtime/tools/node/node.exe') if (BASE/'runtime/tools/node/node.exe').is_file() else shutil.which('node'),str(BASE/'scripts/proxyshare_chain_proxy.mjs')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   try:
    end=time.monotonic()+5
    while True:
     try:conn=socket.create_connection(('127.0.0.1',port),timeout=.2);break
     except OSError:
      if time.monotonic()>end:raise
      time.sleep(.05)
    with conn:
     conn.sendall(b'CONNECT www.temu.com:443 HTTP/1.1\r\nHost: www.temu.com:443\r\n\r\n');data=conn.recv(1024)
    self.assertIn(b'403 Forbidden',data)
   finally:p.terminate();stdout,stderr=p.communicate(timeout=5)
   records=[json.loads(x) for x in stderr.splitlines() if x.startswith('{')]
   self.assertEqual(records[0]['source'],'LOCAL_ACTIVITY_REQUIRED');self.assertNotIn('TEST_ONLY',stderr)
if __name__=='__main__':unittest.main()
