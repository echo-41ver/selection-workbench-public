import importlib.util,pathlib,unittest
from types import SimpleNamespace
from selection_automation.adspower_diagnostics import AdsPowerAPIError
s=importlib.util.spec_from_file_location('kernel',pathlib.Path(__file__).resolve().parents[1]/'src/selection_automation/adspower_kernel.py');k=importlib.util.module_from_spec(s);s.loader.exec_module(k)
class Tests(unittest.TestCase):
 def api(self,states,installed=False):
  calls=[]
  def call(base,path,**kw):
   calls.append((path,kw))
   if 'kernels?' in path:return {'data':{'list':[{'kernel_type':'Firefox','kernel':k.REQUIRED_KERNEL['version'],'is_downloaded':installed or any(c[0].endswith('download-kernel') for c in calls[:-1]) and not states}]}}
   return {'data':{'status':states.pop(0),'progress':50}}
  return call,calls
 def run_install(self,api,**kw):return k.ensure_kernel('http://127.0.0.1:50325',api,pathlib.Path.cwd(),sleep=lambda _:None,disk_usage=lambda _:SimpleNamespace(free=10*1024**3),**kw)
 def test_completed_verified(self):
  api,calls=self.api(['pending','downloading','installing','completed']);self.assertTrue(self.run_install(api)['installed']);self.assertEqual(sum(p.endswith('download-kernel') for p,_ in calls),4)
 def test_idempotent(self):
  api,calls=self.api([],True);self.run_install(api);self.assertEqual(len(calls),1)
 def test_failed_unknown(self):
  for status,reason in [('failed','KERNEL_DOWNLOAD_FAILED'),('weird','KERNEL_STATUS_UNKNOWN')]:
   api,_=self.api([status]);self.assertRaisesRegex(RuntimeError,reason,self.run_install,api)
 def test_completion_needs_inventory(self):
  def api(b,p,**kw):return {'data':{'status':'completed'}} if p.endswith('download-kernel') else {'data':{'list':[{'kernel_type':'Firefox','kernel':k.REQUIRED_KERNEL['version'],'is_downloaded':False}]}}
  self.assertRaisesRegex(RuntimeError,'COMPLETION_UNVERIFIED',self.run_install,api)
 def test_disk_failure_no_post(self):
  api,calls=self.api(['completed']);self.assertRaisesRegex(RuntimeError,'DISK_SPACE',self.run_install,api,min_free_bytes=20*1024**3);self.assertEqual(len(calls),1)
 def test_deadline(self):
  api,calls=self.api(['pending']);t=iter([0,0,100]);self.assertRaisesRegex(RuntimeError,'DEADLINE',self.run_install,api,clock=lambda:next(t),deadline_seconds=1)
 def test_remote_rejected(self):
  self.assertRaisesRegex(RuntimeError,'LOOPBACK',k.ensure_kernel,'https://example.com',lambda *a,**kw:None,'.')
 def test_slow_download_uses_remaining_stage_budget(self):
  installed=[False]
  def api(base,path,**kw):
   if 'kernels?' in path:return {'data':{'list':[{'kernel_type':'Firefox','kernel':'153','is_downloaded':installed[0]}]}}
   if kw['timeout']<25:raise AdsPowerAPIError(path,'CONNECTIVITY','TEST_ONLY_SLOW_DOWNLOAD')
   installed[0]=True
   return {'data':{'status':'completed'}}
  self.assertTrue(self.run_install(api,clock=lambda:0,deadline_seconds=30)['installed'])
 def test_lost_download_response_verifies_inventory_without_reposting(self):
  posts=[]
  def api(base,path,**kw):
   if 'kernels?' in path:return {'data':{'list':[{'kernel_type':'Firefox','kernel':'153','is_downloaded':bool(posts)}]}}
   posts.append(path)
   raise AdsPowerAPIError(path,'CONNECTIVITY','TEST_ONLY_RESPONSE_LOST')
  self.assertTrue(self.run_install(api)['installed'])
  self.assertEqual(len(posts),1)
 def test_unconfirmed_or_unauthorized_download_is_not_success(self):
  for category,installed_after in [('CONNECTIVITY',False),('AUTHORIZATION',True)]:
   posts=[];error=AdsPowerAPIError('/api/v2/browser-profile/download-kernel',category,'TEST_ONLY_FAILURE')
   def api(base,path,**kw):
    if 'kernels?' in path:return {'data':{'list':[{'kernel_type':'Firefox','kernel':'153','is_downloaded':bool(posts) and installed_after}]}}
    posts.append(path)
    raise error
   with self.assertRaises(AdsPowerAPIError) as caught:self.run_install(api)
   self.assertIs(caught.exception,error)
   self.assertEqual(len(posts),1)
if __name__=='__main__':unittest.main()
