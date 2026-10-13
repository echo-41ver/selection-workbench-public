import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import web_console as web
from selection_automation.workers import worker_count, category_worker_count


@pytest.mark.parametrize('value',[0,33,-1,1.5,True,'8',None])
def test_invalid_counts_are_rejected_before_work(value,tmp_path):
    with pytest.raises(ValueError):worker_count(value)
    with pytest.raises(web.ConsoleError):
        web.Console(tmp_path).start({'category_name':'电钻','amazon_count':1,'per_keyword':3,'api_key':'fixture','worker_count':value})


def test_default_and_eight_way_keyword_preview():
    category=json.loads((web.ROOT/'config/categories/power_drill.json').read_text('utf-8'))
    assert category_worker_count(category)==4
    category['temu']['worker_count']=8
    draft=web.draft_view(category,'fixture',{'keywords':[{'query':str(i)} for i in range(10)]})
    assert draft['worker_count']==8
    assert [row['worker'] for row in draft['keyword_plan']]==[1,2,3,4,5,6,7,8,1,2]


def test_eighth_card_rotation_routes_only_to_eighth_worker(tmp_path,monkeypatch):
    console=web.Console(tmp_path);console.key='fixture'
    owner={'category_key':'fixture','pid':123,'created':1.0}
    monkeypatch.setattr(console,'owner',lambda:owner)
    monkeypatch.setattr(web.selection,'live_owner',lambda:owner)
    web.selection.save(tmp_path/'runs/fixture/category.json',{'temu':{'worker_count':8}})
    web.selection.save(tmp_path/'config/sessions.json',{'workers':[{'name':f'worker-{i:02d}-new'} for i in range(1,9)]})
    calls=[]
    monkeypatch.setattr(console,'invoke',lambda argv: calls.append(argv) or SimpleNamespace(returncode=0))
    assert console.action('rotate',{'worker':8})['ok']
    from selection_automation.rotation_queue import read_requests
    assert read_requests(tmp_path/'runs/fixture')[0]['workers']==[8]
    with pytest.raises(web.ConsoleError):console.action('rotate',{'worker':9})
    assert calls==[]
