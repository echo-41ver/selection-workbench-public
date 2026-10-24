"""Real rule transport receives verified image bytes, never remote-fetch URLs."""
import base64
from copy import deepcopy
import hashlib
import io
import json

import pytest
from PIL import Image

import generate_category as generator
from extract_match_criteria import extract
from selection_workflow.image_transport import ImageTransport
from test_all_product_visual_criteria import inputs, response


def picture():
    data=io.BytesIO();Image.new('RGB',(24,24),'blue').save(data,format='JPEG')
    return data.getvalue()


class Reply(io.BytesIO):
    status=200
    headers={}


def image_client(data,downloads):
    class ImageReply:
        status_code=200
        headers={'content-length':str(len(data))}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield data
    class Client:
        def stream(self,method,url,**kwargs):
            assert method=='GET' and kwargs['follow_redirects'] is False
            downloads.append((url,None));return ImageReply()
    return Client()


@pytest.fixture
def wire(tmp_path,monkeypatch):
    monkeypatch.setattr(generator,'ROOT',tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_ONLY')
    monkeypatch.setenv('SELECTION_RULES_BASE_URL','http://localhost:1234/v1')
    monkeypatch.setenv('SELECTION_RULES_MODEL','test')
    calls=[];downloads=[];data=picture()
    def send(request,**kwargs):
        calls.append(json.loads(request.data))
        return Reply(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"items":[]}'}}]}).encode())
    monkeypatch.setattr(ImageTransport,'_client',lambda self,proxy:image_client(data,downloads))
    monkeypatch.setattr(generator,'open_model_url',send)
    return calls,downloads,data


def test_rule_transport_sends_all_original_bytes_and_records_source_hashes(wire,tmp_path):
    calls,downloads,data=wire
    urls=[f'https://m.media-amazon.com/images/I/{i}.jpg' for i in range(8)]
    generator.model_json('offline',images=urls)
    sent=[part['image_url']['url'] for part in calls[0]['messages'][-1]['content'][1:]]
    assert len(sent)==8 and all(value.startswith('data:image/jpeg;base64,') for value in sent)
    assert all(base64.b64decode(value.split(',',1)[1])==data for value in sent)
    assert downloads==[(url,None) for url in urls]
    evidence=json.loads(next((tmp_path/'runtime/model-call-evidence').glob('*.json')).read_text())
    assert [row['source_url'] for row in evidence['image_inputs']]==urls
    assert all(row['content_sha256']==row['wire_sha256']==hashlib.sha256(data).hexdigest() for row in evidence['image_inputs'])
    assert evidence['outcome']=='success'
    assert 'base64,' not in json.dumps(evidence)


def test_validated_cache_is_reused_but_corrupt_cache_is_downloaded_once(wire,tmp_path):
    calls,downloads,data=wire;urls=['https://m.media-amazon.com/images/I/one.jpg']
    generator.model_json('first',images=urls);generator.model_json('second',images=urls)
    assert len(downloads)==1 and len(calls)==2
    cached=list((tmp_path/'runtime/model-image-cache').rglob('*.img'));assert len(cached)==1
    cached[0].write_bytes(b'broken cached pixels')
    generator.model_json('repair',images=urls)
    assert len(downloads)==2 and cached[0].read_bytes()==data


def test_responses_transport_also_embeds_original_bytes(wire,monkeypatch):
    calls,downloads,data=wire
    monkeypatch.setattr('selection_automation.rule_provider.request_protocol',lambda provider:('responses','http://localhost:1234/v1/responses'))
    def send(request,**kwargs):
        calls.append(json.loads(request.data))
        return Reply(json.dumps({'status':'completed','output':[{'type':'message','role':'assistant',
            'content':[{'type':'output_text','text':'{"items":[]}'}]}]}).encode())
    monkeypatch.setattr(generator,'open_model_url',send)
    generator.model_json('test',images=['https://m.media-amazon.com/images/I/one.jpg'])
    image=calls[0]['input'][-1]['content'][1]
    assert image['type']=='input_image' and base64.b64decode(image['image_url'].split(',',1)[1])==data


def test_provider_400_keeps_unknown_billing_and_never_persists_echoed_base64(wire,monkeypatch,tmp_path):
    from urllib.error import HTTPError
    calls,downloads,data=wire
    def reject(request,**kwargs):
        calls.append(json.loads(request.data));attachment=calls[-1]['messages'][-1]['content'][1]['image_url']['url']
        raise HTTPError('http://localhost:1234/v1',400,'bad request',{},io.BytesIO(json.dumps({'error':{'message':'invalid image: '+attachment}}).encode()))
    monkeypatch.setattr(generator,'open_model_url',reject)
    with pytest.raises(generator.CategoryGenerationError) as failure:
        generator.model_json('test',images=['https://m.media-amazon.com/images/I/one.jpg'])
    assert failure.value.code=='MODEL_HTTP_400' and 'base64,' not in str(failure.value)
    evidence=json.loads(next((tmp_path/'runtime/model-call-evidence').glob('*.json')).read_text())
    assert evidence['http_status']==400 and evidence['billing_state']=='UNKNOWN' and evidence['amount'] is None
    assert evidence['transport_attempt_count']==1 and 'base64,' not in json.dumps(evidence)


@pytest.mark.parametrize('bad_kind',['redirect','oversize','second_corrupt'])
def test_all_images_must_be_safe_before_the_first_model_post(wire,monkeypatch,bad_kind):
    calls,downloads,data=wire
    import selection_automation.rule_image_input as image_input
    if bad_kind=='oversize':monkeypatch.setattr(image_input,'MAX_BYTES',128)
    class ReplyImage:
        status_code=302 if bad_kind=='redirect' else 200
        headers={'location':'http://127.0.0.1/private'} if bad_kind=='redirect' else {}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield b'corrupt' if bad_kind=='second_corrupt' and len(downloads)>1 else data
    class Client:
        def stream(self,method,url,**kwargs):downloads.append(url);return ReplyImage()
    monkeypatch.setattr(ImageTransport,'_client',lambda *a:Client())
    with pytest.raises(generator.CategoryGenerationError):
        generator.model_json('test',images=['https://m.media-amazon.com/images/I/one.jpg','https://m.media-amazon.com/images/I/two.jpg'])
    assert calls==[]
    assert all(url.startswith('https://m.media-amazon.com/') for url in downloads)


@pytest.mark.parametrize('bad_kind',['corrupt','unsupported','foreign_url'])
def test_invalid_local_image_never_enters_model_transport(wire,monkeypatch,tmp_path,bad_kind):
    calls,downloads,_=wire
    data=b'broken JPEG'
    if bad_kind=='unsupported':
        out=io.BytesIO();Image.new('RGB',(24,24),'blue').save(out,format='TIFF');data=out.getvalue()
    monkeypatch.setattr(ImageTransport,'_client',lambda *a,**k:image_client(data,downloads))
    url='https://m.media-amazon.com/images/I/bad.jpg' if bad_kind!='foreign_url' else 'http://127.0.0.1/private'
    with pytest.raises(generator.CategoryGenerationError) as failed:generator.model_json('test',images=[url])
    assert failed.value.code=='MODEL_IMAGE_INPUT_FAILED' and calls==[]
    evidence=json.loads(next((tmp_path/'runtime/model-call-evidence').glob('*.json')).read_text())
    assert evidence['transport_attempt_count']==0 and evidence['outcome']=='failed'
    assert evidence['request_sent'] is False and evidence['billing_state']=='NO_CHARGE'


def test_explicit_single_image_400_retry_preserves_rejection_and_is_only_once(tmp_path):
    category,snapshot=inputs(1);path=tmp_path/'receipt.json';calls=[]
    def reject(question,**kwargs):
        calls.append(1)
        raise generator.CategoryGenerationError('invalid_request_error: Failed to download image from '+snapshot['records'][0]['main_image_url'],'MODEL_HTTP_400')
    extract(category,snapshot,reject,path)
    original=deepcopy(json.loads(path.read_text())['batches'])
    extract(category,snapshot,reject,path)
    assert len(calls)==1
    result=extract(category,snapshot,response,path,retry_http400=True)
    assert result['match_criteria']['state']=='ready'
    batch=next(iter(json.loads(path.read_text())['batches'].values()))
    assert batch['history']==list(original.values()) and batch['http400_retries']==1
    assert batch['history'][0]['state']=='uncertain' and batch['history'][0]['error']=='MODEL_HTTP_400'


@pytest.mark.parametrize('kind',['timeout','other400','retry400'])
def test_explicit_retry_does_not_release_unknown_or_other_400_or_repeat_retry(tmp_path,kind):
    category,snapshot=inputs(1);path=tmp_path/'receipt.json';calls=[]
    def fail(question,**kwargs):
        calls.append(1)
        if kind=='timeout':raise TimeoutError('result unknown')
        message='bad parameter' if kind=='other400' else 'Failed to download image from '+snapshot['records'][0]['main_image_url']
        raise generator.CategoryGenerationError(message,'MODEL_HTTP_400')
    extract(category,snapshot,fail,path)
    extract(category,snapshot,fail,path,retry_http400=True)
    extract(category,snapshot,fail,path,retry_http400=True)
    assert len(calls)==(2 if kind=='retry400' else 1)
    assert category['match_criteria']['state']!='ready'


@pytest.mark.parametrize('error_code',['MODEL_INVALID_JSON','MODEL_OUTPUT_TRUNCATED','MODEL_HTTP_429','timeout'])
def test_explicit_image_download_retry_never_gets_an_additional_automatic_model_turn(tmp_path,error_code):
    category,snapshot=inputs(1);path=tmp_path/'receipt.json';calls=[]
    def fail(question,**kwargs):
        calls.append(1)
        if len(calls)==1:raise generator.CategoryGenerationError('Failed to download image from '+snapshot['records'][0]['main_image_url'],'MODEL_HTTP_400')
        if error_code=='timeout':raise TimeoutError('new request outcome unknown')
        raise generator.CategoryGenerationError('new response failed',error_code)
    extract(category,snapshot,fail,path)
    result=extract(category,snapshot,fail,path,retry_http400=True)
    assert len(calls)==2 and result['match_criteria']['state']!='ready'
    batch=next(iter(json.loads(path.read_text())['batches'].values()))
    assert batch['error']==('TimeoutError' if error_code=='timeout' else error_code)
    assert batch['http400_retries']==1 and batch['history'][0]['error']=='MODEL_HTTP_400'
    if error_code=='timeout':
        extract(category,snapshot,fail,path,retry_http400=True)
        assert len(calls)==2 and next(iter(json.loads(path.read_text())['batches'].values()))==batch


@pytest.mark.parametrize('resuming',[False,True])
def test_web_only_authorizes_rejected_image_retry_on_explicit_preparation_resume(tmp_path,monkeypatch,resuming):
    import web_console as web
    console=web.Console(tmp_path);folder=tmp_path/'runtime/web-console';folder.mkdir(parents=True,exist_ok=True)
    category,snapshot=inputs(1);category['temu']={'worker_count':1}
    path=folder/'category-12345678123456781234567812345678.json'
    if resuming:path.write_text(json.dumps(category));console.preparation_path=path
    amazon=folder/'amazon.json';amazon.write_text(json.dumps(snapshot));plan=folder/'keywords.json';plan.write_text('{"keywords":[]}')
    monkeypatch.setattr(console,'collect_amazon_and_plan',lambda *a:(amazon,plan))
    monkeypatch.setattr(console,'visual_preparation_complete',lambda category,snapshot:category.get('test_reviewed') is True)
    monkeypatch.setattr(console,'explain_file',lambda source,*a:source)
    monkeypatch.setattr(console,'persist_draft',lambda:None)
    monkeypatch.setattr(web,'draft_view',lambda category,*a:{'category_key':category['category_key']})
    extracted=[]
    def invoke(args,**kwargs):
        if Path(args[0]).name=='generate_category.py':Path(args[args.index('--output')+1]).write_text(json.dumps(category))
        if Path(args[0]).name=='extract_match_criteria.py':
            extracted.append(args);dest=Path(args[args.index('--category')+1]);value=json.loads(dest.read_text());value['test_reviewed']=True;dest.write_text(json.dumps(value))
        return type('Result',(),{'returncode':0})()
    from pathlib import Path
    monkeypatch.setattr(console,'invoke',invoke)
    console._start({'category_name':'浴帘','amazon_count':1,'per_keyword':1,'worker_count':1,'api_key':'TEST_ONLY'})
    assert len(extracted)==1 and ('--retry-http-400' in extracted[0]) is resuming
