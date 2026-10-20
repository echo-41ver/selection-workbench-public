import ast,importlib.util,pathlib,sys,types,tempfile,json,unittest
from unittest.mock import patch,Mock
sys.dont_write_bytecode=True
B=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(B/'src'))
def load(name,f):
 spec=importlib.util.spec_from_file_location(name,B/f);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
w=load('selection_automation.worker_runtime_v2','src/selection_automation/worker_runtime.py')
r=load('selection_automation.recovery_policy','src/selection_automation/recovery_policy.py');sys.modules['selection_automation.recovery_policy']=r
tree=ast.parse((B/'scripts/pipeline.py').read_text('utf-8'))
owned=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='owned_worker')
loop=next(n for n in ast.walk(owned) if isinstance(n,ast.While) and isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.Name) and n.test.left.id=='attempt')
# Execute actual post-collector branch statements in an isolated mock harness.
start=next(i for i,n in enumerate(loop.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='status' for t in n.targets))
body=loop.body[start:]
fn=ast.FunctionDef(name='branch',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg='progress'),ast.arg(arg='returncode')],kwonlyargs=[],kw_defaults=[],defaults=[ast.Constant(1)]),body=[ast.Assign(targets=[ast.Name(id='recovery_record',ctx=ast.Store())],value=ast.Dict(keys=[],values=[])),ast.Assign(targets=[ast.Name(id='attempt',ctx=ast.Store())],value=ast.Constant(0)),ast.While(test=ast.Compare(left=ast.Name(id='attempt',ctx=ast.Load()),ops=[ast.Lt()],comparators=[ast.Constant(3)]),body=[ast.AugAssign(target=ast.Name(id='attempt',ctx=ast.Store()),op=ast.Add(),value=ast.Constant(1))]+body,orelse=[])],decorator_list=[])
code=compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'actual_pipeline_branch','exec')
class Tests(unittest.TestCase):
 def args(self,root,task='current',worker='worker-01-new'):
  return ['node.exe',str(root/'scripts/temu_flower_collect.mjs'),'--output',str(root/f'runs/{task}/temu/workers/{worker}/query')]
 def test_real_entry_spaces_and_exact_task(self):
  root=pathlib.Path.cwd()/'root with spaces';a=self.args(root)
  self.assertTrue(w.collector_belongs_to_worker(a,root,'worker-01-new','current'))
  for bad in [['node.exe','other.mjs',*a[1:]],['node.exe','--eval',*a[1:]],self.args(root,'other'),self.args(root,worker='worker-02-new'),a+['--output',a[-1]],self.args(root)+['extra']]:
   if bad[-1]=='extra':continue
   self.assertFalse(w.collector_belongs_to_worker(bad,root,'worker-01-new','current'))
 def test_mock_terminate_only_exact(self):
  import psutil
  root=pathlib.Path.cwd()/'root with spaces'
  a=self.args(root);args=[a,['node.exe','other.mjs',*a[1:]],self.args(root,'other'),self.args(root,worker='worker-02-new')]
  ps=[types.SimpleNamespace(info={'name':'node.exe','cmdline':x},terminate=Mock()) for x in args]
  with patch.object(psutil,'process_iter',return_value=ps),patch.object(psutil,'wait_procs',return_value=([ps[0]],[])):
   self.assertEqual(w.stop_owned_collectors(root,'worker-01-new','current')['stopped_owned_collectors'],1)
  ps[0].terminate.assert_called_once()
  for p in ps[1:]:p.terminate.assert_not_called()
 def env(self):
  forbidden=Mock(side_effect=AssertionError('automatic action invoked'))
  repair=types.ModuleType('repair_browser_control');repair.repair=forbidden;sys.modules['repair_browser_control']=repair
  env={'write_worker_state':Mock(),'session':{'name':'worker-01-new'},'query':'q','output':pathlib.Path('out'),'log':pathlib.Path('out/log'),'returncode':1,'lease':types.SimpleNamespace(check_stop=forbidden),'classify_recovery_kind':forbidden,'recovery_budget':forbidden,'collector_block':Mock(return_value=None),'interruptible_wait':forbidden,'completed':forbidden}
  from empty_results_checkpoint import confirmed_empty_checkpoint,confirmed_empty_observation
  env.update(confirmed_empty_checkpoint=confirmed_empty_checkpoint,confirmed_empty_observation=confirmed_empty_observation,completed_queries=[],item_index=1,items=[])
  exec(code,env);return env,forbidden
 def test_cross_status_terminal_flags_prevent_all_actions(self):
  statuses=['STOPPED_BOOTSTRAP_NAVIGATION','STOPPED_BROWSER_CONTROL_FAILURE','STOPPED_PROXY_OR_NETWORK_FAILURE','OTHER_TRANSIENT','KEYWORD_COMPLETE']
  for status in statuses:
   for flags in [{'manual_action_required':True},{'automatic_retry':False},{'manual_action_required':True,'automatic_retry':False}]:
    with self.subTest(status=status,flags=flags):
     env,forbidden=self.env();self.assertFalse(env['branch']({'status':status,**flags}));forbidden.assert_not_called();self.assertEqual(env['write_worker_state'].call_args.args[1]['status'],'HUMAN_ACTION_REQUIRED')
 def test_missing_or_false_manual_retains_transient_retry(self):
  for flags in [{},{'manual_action_required':False},{'manual_action_required':False,'automatic_retry':True}]:
   env,_=self.env();env.update(classify_recovery_kind=Mock(return_value=None),recovery_budget=Mock(return_value=(False,{})),recovery_record={},transport_recovery_allowed=Mock(return_value=False),interruptible_wait=Mock(),item_index=1,items=[],completed_queries=[])
   # Reload compiled function so names resolve updated mock globals.
   exec(code,env)
   with self.assertRaises(RuntimeError):env['branch']({'status':'OTHER_TRANSIENT',**flags})
   self.assertEqual(env['interruptible_wait'].call_count,2)
if __name__=='__main__':unittest.main()
