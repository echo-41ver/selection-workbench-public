import ast,json,pathlib,sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
import selection
import retain_proxy_connections as keeper


def test_all_supervisor_exit_reasons_preserve_browser_proxy_and_leases():
    source=pathlib.Path(selection.__file__).read_text("utf-8")
    tree=ast.parse(source)
    supervise=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="supervise")
    final=next(n for n in ast.walk(supervise) if isinstance(n,ast.Try) and n.finalbody).finalbody
    text=ast.unparse(ast.Module(body=final,type_ignores=[]))
    assert "ensure_running(ROOT)" in text
    assert "close_terminal_browsers" not in text
    assert "revoke_all_activity" in text
    assert "explicitly_stopped = (base / 'STOP').exists()" in text
    assert "status.startswith('完成')" in text
    assert "cleanup_ended_task(base.name)" in text
    assert "proxyshare_chain_manager" not in text
    assert text.index("lease.__exit__")<text.index("ensure_running(ROOT)")


def test_terminating_collector_preserves_durable_descendants(monkeypatch):
    import psutil
    events=[]
    class Process:
        def __init__(self,pid,name="python.exe",args=(),kids=()):self.pid=pid;self.label=name;self.args=args;self.kids=kids
        def name(self):return self.label
        def cmdline(self):return self.args
        def children(self,recursive=True):return self.kids
        def terminate(self):events.append(self.pid)
        def is_running(self):return False
    content=Process(12,"plugin-container.exe")
    browser=Process(11,"FlowerBrowser.exe",kids=[content])
    driver=Process(13,"geckodriver.exe")
    chain=Process(14,"node.exe",["node","D:/app/scripts/proxyshare_chain_proxy.mjs"])
    lease=Process(15,"python.exe",["python","D:/app/scripts/retain_proxy_connections.py"])
    collector=Process(16,"node.exe",["node","D:/app/scripts/temu_flower_collect.mjs"])
    parent=Process(10,kids=[browser,content,driver,chain,lease,collector])
    monkeypatch.setattr(psutil,"Process",lambda _:parent)
    monkeypatch.setattr(psutil,"wait_procs",lambda *a,**k:None)
    selection.terminate_owned(SimpleNamespace(pid=10,poll=lambda:None))
    assert events==[16,10]


def test_existing_independent_retainer_is_reused_without_launch(tmp_path,monkeypatch):
    (tmp_path/"runtime").mkdir()
    (tmp_path/"runtime/proxy-connections.json").write_text(json.dumps({"pid":1,"created":100}))
    monkeypatch.setattr(keeper,"owned_chains",lambda r:{1:(5,100)})
    monkeypatch.setattr(keeper.psutil,"Process",lambda p:SimpleNamespace(pid=1,create_time=lambda:100,
        cmdline=lambda:["python",str(tmp_path/"scripts/retain_proxy_connections.py")]))
    import subprocess
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**k:pytest.fail("duplicate keeper"))
    assert keeper.ensure_running(tmp_path)==1


def test_no_live_chain_does_not_launch_or_change_route(tmp_path,monkeypatch):
    monkeypatch.setattr(keeper,"owned_chains",lambda r:{})
    import subprocess
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**k:pytest.fail("no chain to retain"))
    assert keeper.ensure_running(tmp_path) is None


def test_stop_owned_helper_requires_exact_pid_creation_time_and_script(tmp_path,monkeypatch):
    (tmp_path/"runtime").mkdir();(tmp_path/"scripts").mkdir()
    script=tmp_path/"scripts/retain_proxy_connections.py";script.touch()
    descriptor=tmp_path/"runtime/proxy-connections.json"
    descriptor.write_text(json.dumps({"pid":41,"created":12.5}))
    class Process:
        pid=41
        def __init__(self,pid):pass
        def create_time(self):return 12.5
        def cmdline(self):return ["python",str(script)]
        def terminate(self):self.terminated=True
        def wait(self,timeout):pass
    process=Process(41)
    monkeypatch.setattr(keeper.psutil,"Process",lambda _:process)
    assert keeper.stop_owned(tmp_path)
    assert process.terminated and not descriptor.exists()


def test_stop_owned_helper_leaves_unverified_process_alone(tmp_path,monkeypatch):
    (tmp_path/"runtime").mkdir();(tmp_path/"scripts").mkdir()
    script=tmp_path/"scripts/retain_proxy_connections.py";script.touch()
    descriptor=tmp_path/"runtime/proxy-connections.json"
    descriptor.write_text(json.dumps({"pid":41,"created":12.5}))
    class Process:
        def __init__(self,pid):pass
        def create_time(self):return 12.5
        def cmdline(self):return ["python","unrelated.py"]
        def terminate(self):pytest.fail("unowned process must not be terminated")
    monkeypatch.setattr(keeper.psutil,"Process",Process)
    assert not keeper.stop_owned(tmp_path)
    assert descriptor.exists()
