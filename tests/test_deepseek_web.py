import io,json,urllib.error
import pytest
from selection_automation import deepseek_web as web

PROVIDER={'base_url':'https://api.deepseek.com','model':'deepseek-flash'}


def test_search_uses_official_endpoint_and_existing_key(monkeypatch):
    calls=[]
    def open(req,**kwargs):
        calls.append(req)
        return io.BytesIO(json.dumps({'results':[{'url':'https://www.amazon.com/Best-Sellers/zgbs'}]}).encode())
    monkeypatch.setattr(web,'open_model_url',open)
    assert len(web.search('mouse pad','test-secret',PROVIDER,original='鼠标垫'))==1
    assert calls[0].full_url=='https://api.deepseek.com/v1/tools/search'
    assert calls[0].headers['Authorization']=='Bearer test-secret'
    payload=json.loads(calls[0].data)
    assert payload['ori_question']=='鼠标垫' and payload['need_content'] is True


@pytest.mark.parametrize('status,retries',[(408,3),(429,3),(503,3),(401,1),(402,1),(403,1),(404,1)])
def test_read_only_web_errors_retry_only_transient_failures(monkeypatch,status,retries):
    calls=[]
    def open(req,**kwargs):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url,status,'failure',{},io.BytesIO(b'{"error":{"message":"test-secret sk-abcdefghijk"}}'))
    monkeypatch.setattr(web,'open_model_url',open)
    monkeypatch.setattr(web.time,'sleep',lambda _:None)
    with pytest.raises(web.WebLookupError) as error:web.search('mouse pad','test-secret',PROVIDER)
    assert error.value.status==status and len(calls)==retries
    assert 'test-secret' not in str(error.value) and 'sk-abcdefghijk' not in str(error.value)


@pytest.mark.parametrize('url',['https://api.example.com','http://api.deepseek.com','https://api.deepseek.com.evil.test'])
def test_never_forwards_key_to_other_provider(monkeypatch,url):
    monkeypatch.setattr(web,'open_model_url',lambda *a,**kw:pytest.fail('key forwarded'))
    with pytest.raises(web.WebLookupError):web.search('mouse pad','test-secret',{'base_url':url})


def test_fetch_reads_markdown_and_empty_search_is_valid(monkeypatch):
    monkeypatch.setattr(web,'open_model_url',lambda *a,**kw:io.BytesIO(b'{"content":"# Best Sellers in Mouse Pads","title":"Mouse Pads"}'))
    assert web.fetch('https://www.amazon.com/gp/bestsellers/office-products/705323011','test-secret',PROVIDER)['title']=='Mouse Pads'
    monkeypatch.setattr(web,'open_model_url',lambda *a,**kw:io.BytesIO(b'{"results":[]}'))
    assert web.search('unknown category','test-secret',PROVIDER)==[]
