import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collect_amazon_until_complete as collector


def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(collector,'ROOT',tmp_path)
    monkeypatch.setattr(collector,'amazon_environment',lambda root:{})
    monkeypatch.setattr(collector.time,'sleep',lambda seconds:None)
    base=tmp_path/'runs/fixture';(base/'amazon').mkdir(parents=True)
    category=base/'category.json';category.write_text(json.dumps({'category_key':'fixture','amazon':{'target_count':1}}))
    return category,base,base/'amazon/amazon_frontend_latest.json'


def test_completed_details_are_never_requested_again(tmp_path,monkeypatch):
    category,_,output=fixture(tmp_path,monkeypatch)
    output.write_text(json.dumps({'status':'COMPLETE','details_complete':True,'records':[{'asin':'A'}]}))
    monkeypatch.setattr(collector.subprocess,'Popen',lambda *a,**k:(_ for _ in ()).throw(AssertionError('No fetch needed')))
    assert collector.collect(category)==0


def test_process_failures_continue_past_old_limits_until_real_completion(tmp_path,monkeypatch):
    category,_,output=fixture(tmp_path,monkeypatch);calls=[]
    class Child:
        def poll(self):return 1
        def wait(self):return 1
    def start(*args,**kwargs):
        calls.append(args)
        if len(calls)==12:
            output.write_text(json.dumps({'status':'COMPLETE','details_complete':True,'records':[{'asin':'A'}]}))
        return Child()
    monkeypatch.setattr(collector.subprocess,'Popen',start)
    assert collector.collect(category)==0
    assert len(calls)==12


def test_user_stop_prevents_another_request(tmp_path,monkeypatch):
    category,base,_=fixture(tmp_path,monkeypatch);(base/'STOP').touch()
    monkeypatch.setattr(collector.subprocess,'Popen',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Task stopped')))
    assert collector.collect(category)==0
