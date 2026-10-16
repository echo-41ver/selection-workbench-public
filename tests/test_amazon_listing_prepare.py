import json
import os
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_real_collector_listing_only_never_opens_details(tmp_path):
    cat={'category_key':'listing_fixture','amazon':{'mode':'BEST_SELLERS_FRONTEND','target_count':1,
         'bestsellers_url':'https://www.amazon.com/zgbs/hi/552794'}}
    config=tmp_path/'category.json';config.write_text(json.dumps(cat))
    (tmp_path/'page1.html').write_text('<div class="zg-grid-general-faceout"><span class="zg-bdg-ctr">#1</span>'
        '<a href="/dp/B000000001">Magnetic power drill</a>'
        '<img src="https://m.media-amazon.com/images/I/fixture.jpg"></div>')
    output=tmp_path/'amazon.json'
    result=subprocess.run(['node','--import',(ROOT/'tests/fixtures/amazon_browser_fixture.mjs').as_uri(),
        str(ROOT/'scripts/collect_amazon_search_frontend.mjs'),'--config',str(config),'--output',str(output),'--listing-only'],
        cwd=ROOT,env={**os.environ,'AMAZON_FIXTURE_DIR':str(tmp_path)},capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    data=json.loads(output.read_text('utf-8'))
    assert data['status']=='LISTING_COMPLETE' and data['listing_complete']
    assert not data['details_complete'] and not data['complete']
    assert data['records'][0]['asin']=='B000000001'
    requests=[json.loads(line)['url'] for line in (tmp_path/'requests.jsonl').read_text().splitlines()]
    assert not any('/dp/' in url for url in requests)


def test_ranking_second_page_failure_resumes_without_reloading_first(tmp_path):
    cat={'category_key':'listing_fixture','amazon':{'mode':'BEST_SELLERS_FRONTEND','target_count':51,
         'bestsellers_url':'https://www.amazon.com/zgbs/hi/552794'}}
    config=tmp_path/'category.json';config.write_text(json.dumps(cat))
    def card(n):
        return f'<div class="zg-grid-general-faceout"><span class="zg-bdg-ctr">#{n}</span>' \
               f'<a href="/dp/B{n:09d}">Power drill {n}</a>' \
               '<img src="https://m.media-amazon.com/images/I/fixture.jpg"></div>'
    (tmp_path/'page1.html').write_text(''.join(card(n) for n in range(1,51)))
    (tmp_path/'page2.html').write_text(card(51))
    output=tmp_path/'amazon.json'
    command=['node','--import',(ROOT/'tests/fixtures/amazon_browser_fixture.mjs').as_uri(),
        str(ROOT/'scripts/collect_amazon_search_frontend.mjs'),'--config',str(config),'--output',str(output),'--listing-only']
    env={**os.environ,'AMAZON_FIXTURE_DIR':str(tmp_path),'AMAZON_FIXTURE_CONTINUE':'1'}
    failed=subprocess.run(command,cwd=ROOT,env={**env,'AMAZON_FIXTURE_FAIL_PAGE2':'1'},capture_output=True,text=True,timeout=45)
    assert failed.returncode!=0
    partial=json.loads(output.read_text('utf-8'))
    assert partial['record_count']==50 and partial['status']=='LISTING_FAILED'
    before=(tmp_path/'requests.jsonl').read_text().splitlines()
    resumed=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=45)
    assert resumed.returncode==0,resumed.stderr
    extra=[json.loads(line)['url'] for line in (tmp_path/'requests.jsonl').read_text().splitlines()[len(before):]]
    ranking=[url for url in extra if '/zgbs/' in url]
    assert ranking and all('pg=2' in url for url in ranking)
    complete=json.loads(output.read_text('utf-8'))
    assert complete['status']=='LISTING_COMPLETE'
    assert [r['rank'] for r in complete['records']]==list(range(1,52))
