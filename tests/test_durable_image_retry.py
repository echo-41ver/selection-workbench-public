import json,io,sqlite3,sys,pathlib,urllib.error
import numpy as np
import pytest
from PIL import Image
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from selection_workflow.incremental_stage12 import IncrementalStage12,SourceSpec,PathSpec
from selection_workflow.image_retry import ImageRetryQueue,classify_failure
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from run_incremental_stage12 import WatchInstance

class Vectorizer:
    revision='fake-vector-v1'
    def encode(self,p):return np.array([1,2,3,4],dtype=np.float32)

def setup(tmp_path,temu_count=1):
    stream=io.BytesIO();Image.new('RGB',(32,32),(200,20,30)).save(stream,format='JPEG');data=stream.getvalue()
    a=tmp_path/'amazon.jsonl';t=tmp_path/'temu.jsonl'
    a.write_text(''.join(json.dumps({'asin':f'A{n}','title':'red floral shower curtain','main_image_url':'https://a.test/img','url':f'https://a.test/{n}'})+'\n' for n in range(30)))
    t.write_text(''.join(json.dumps({'goods_id':f'T{n}','title':'red floral shower curtain','main_image_url':f'https://t.test/img{n}','product_url':f'https://t.test/{n}'})+'\n' for n in range(temu_count)))
    engine=IncrementalStage12(tmp_path/'run',vectorizer=Vectorizer(),image_download_workers=4)
    engine.configure([SourceSpec('a','AMAZON','amazon_generic',a),SourceSpec('t','TEMU','temu_frontend',t)],[PathSpec('p','a','t','test')])
    engine.ingest_source('a');engine.ingest_source('t')
    clock=[1000.];engine.image_retry.clock=lambda:clock[0];engine.image_retry.jitter=lambda:.5
    return engine,clock,data

def test_one_asset_for_thirty_pairs_retries_past_three_and_restarts(tmp_path):
    e,clock,data=setup(tmp_path);hits=[]
    def download(product):
        ref,key=e._image_identity(product)
        if ref.startswith('https://t.'):
            hits.append(ref)
            if len(hits)<=4:raise TimeoutError('temporary timeout')
        return data
    e._download_image_bytes=download;e.process_batch(100)
    assert len(hits)==1
    assert e.con.execute("select count(*) from pairs where status='WAIT_IMAGE_RETRY'").fetchone()[0]==30
    queue=e.con.execute("select * from image_retry_queue where source_ref like 'https://t.%'").fetchone()
    key=queue['image_key'];due=queue['next_due'];e.close()
    e=IncrementalStage12(tmp_path/'run',vectorizer=Vectorizer(),image_download_workers=4)
    e.image_retry.clock=lambda:clock[0];e.image_retry.jitter=lambda:.5;e._download_image_bytes=download
    assert e.image_retry.get(key)['next_due']==due
    e.process_batch(100);assert len(hits)==1
    for _ in range(4):
        clock[0]=e.image_retry.get(key)['next_due']+1;e.process_batch(100)
    assert len(hits)==5 and e.image_retry.get(key)['status']=='READY'
    e.process_batch(100)
    assert e.con.execute("select count(*) from pairs where status='CANDIDATE'").fetchone()[0]==30
    assert e.con.execute('select retry_count from images where image_key=?',(key,)).fetchone()[0]==4
    assert e.con.execute("select count(*) from image_retry_history where image_key=? and event='DOWNLOAD_FAILED'",(key,)).fetchone()[0]==4
    e.process_batch(100);assert len(hits)==5;e.close()

def test_permanent_refusal_is_action_required_and_good_images_are_not_starved(tmp_path):
    e,clock,data=setup(tmp_path,2);hits=[]
    def download(product):
        ref,_=e._image_identity(product);hits.append(ref)
        if ref=='https://t.test/img0':raise urllib.error.HTTPError(ref,403,'Forbidden',{},None)
        return data
    e._download_image_bytes=download;e.process_batch(100)
    assert e.con.execute("select count(*) from pairs where status='CANDIDATE'").fetchone()[0]==30
    assert e.con.execute("select count(*) from pairs where status='FAILED_IMAGE_ACTION'").fetchone()[0]==30
    clock[0]+=100000;e.process_batch(100)
    assert hits.count('https://t.test/img0')==1;e.close()

def test_stop_does_not_download_or_erase_durable_retry(tmp_path):
    e,clock,data=setup(tmp_path);hits=[]
    def download(product):
        ref,_=e._image_identity(product)
        if ref.startswith('https://t.'):hits.append(ref);raise TimeoutError('temporary timeout')
        return data
    e._download_image_bytes=download;e.process_batch(100)
    before=[tuple(r) for r in e.con.execute('select * from image_retry_queue')]
    clock[0]+=1000;e.stop_requested=lambda:True
    assert e.process_batch(100)['cancelled'] and len(hits)==1
    assert before==[tuple(r) for r in e.con.execute('select * from image_retry_queue')]
    e.close()

def test_legacy_failure_migration_keeps_pair_revision_attempts_and_audit(tmp_path):
    e,clock,data=setup(tmp_path)
    product=e._product('t','T0');ref,key=e._image_identity(product)
    e.con.execute('insert into images values(?,?,?,?,?,?,?,?)',(key,ref,None,None,'FAILED',3,'SSL: UNEXPECTED_EOF_WHILE_READING','old'))
    e.con.execute("update pairs set status='FAILED_BOUNDED',retry_count=3,last_error='old SSL failure'");e.con.commit()
    revisions=[r[0] for r in e.con.execute('select pair_revision from pairs')]
    lease=WatchInstance(e.root);lease.acquire()
    e.migrate_image_retries(lease)
    assert e.image_retry.get(key)['attempts']==3
    assert e.con.execute("select count(*) from pairs where status='WAIT_IMAGE_RETRY' and retry_count=3").fetchone()[0]==30
    assert e.con.execute('select count(*) from image_pair_requeue_history').fetchone()[0]==30
    e._download_image_bytes=lambda product:data;e.process_batch(100);e.process_batch(100)
    assert e.con.execute("select count(*) from pairs where status='CANDIDATE' and retry_count=3").fetchone()[0]==30
    assert revisions==[r[0] for r in e.con.execute('select pair_revision from pairs')];e.close();lease.release()

def test_read_only_constructor_does_not_migrate_and_restore_needs_exclusive_writer(tmp_path):
    e,clock,data=setup(tmp_path);product=e._product('t','T0');ref,key=e._image_identity(product)
    e.con.execute('insert into images values(?,?,?,?,?,?,?,?)',(key,ref,None,None,'FAILED',3,'SSL: UNEXPECTED_EOF_WHILE_READING','old'))
    e.con.execute("update pairs set status='FAILED_BOUNDED',retry_count=3,last_error='preserved'");e.con.commit();e.close()
    e=IncrementalStage12(tmp_path/'run',vectorizer=Vectorizer())
    assert e.con.execute("select count(*) from pairs where status='FAILED_BOUNDED'").fetchone()[0]==30
    assert e.con.execute('select count(*) from image_retry_history').fetchone()[0]==0
    with pytest.raises(RuntimeError):e.migrate_image_retries(None)
    owner=WatchInstance(e.root);owner.acquire();other=WatchInstance(e.root)
    try:
        with pytest.raises(SystemExit):other.acquire()
        whitelist={(r['path_key'],r['asin'],r['goods_id'],r['pair_revision']) for r in e.con.execute('select * from pairs limit 1')}
        e.migrate_image_retries(owner,allowed_assets={key},allowed_pairs=whitelist)
        assert e.con.execute("select count(*) from pairs where status='WAIT_IMAGE_RETRY'").fetchone()[0]==1
        assert e.con.execute("select count(*) from pairs where status='FAILED_BOUNDED'").fetchone()[0]==29
    finally:e.close();owner.release()

def test_transient_retry_backoff_is_persistent_and_capped(tmp_path):
    con=sqlite3.connect(tmp_path/'queue.db');con.row_factory=sqlite3.Row;clock=[1000.]
    q=ImageRetryQueue(con,clock=lambda:clock[0],jitter=lambda:.5)
    for _ in range(25):
        q.started('asset','https://a.test',{});q.finish('asset',TimeoutError('timeout'))
        row=q.get('asset');assert 1<=row['next_due']-clock[0]<=300
        clock[0]=row['next_due'];con.commit()
    assert q.get('asset')['attempts']==25 and q.get('asset')['status']=='RETRY'
    assert classify_failure(urllib.error.HTTPError('https://a.test',404,'Not Found',{},None))=='ACTION_MISSING'
    assert classify_failure(RuntimeError('certificate verify failed'))=='ACTION_TLS'
    con.close()
