import pytest
import web_console as web
from selection_automation.operator_feedback import read_feedback


def test_operator_feedback_rejects_unlisted_product(tmp_path, monkeypatch):
    console=web.Console(tmp_path)
    monkeypatch.setattr(console,'operator_view',lambda:{'subscription':{'label':'运营'},'snapshot':{'products':[]}})
    with pytest.raises(web.ConsoleError,match='商品不在'):
        console.operator_feedback({'asin':'UNKNOWN','state':'NEW'})
    assert read_feedback(tmp_path,'运营')=={}


def test_feedback_uses_current_operator_and_trusted_source(tmp_path, monkeypatch):
    console=web.Console(tmp_path)
    view={'subscription':{'label':'数码运营'},'snapshot':{'products':[{'asin':'B01MOUSE','source_run_key':'mouse'}]}}
    monkeypatch.setattr(console,'operator_view',lambda:view)
    result=console.operator_feedback({'asin':'B01MOUSE','state':'INQUIRY','label':'其他人','source_run_key':'other'})
    assert result['feedback']['source_run_key']=='mouse'
    assert read_feedback(tmp_path,'数码运营')['B01MOUSE']['state']=='INQUIRY'
    assert read_feedback(tmp_path,'其他人')=={}
    view['snapshot']['products']=[];view['feedback']=read_feedback(tmp_path,'数码运营')
    assert console.operator_feedback({'asin':'B01MOUSE','state':'NEW'})['feedback']['state']=='NEW'
