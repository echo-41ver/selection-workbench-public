from copy import deepcopy
import json

import pytest

from selection_automation.codex_visual_review import import_reviews, load_reviews
from selection_automation.visual_text_evidence import price_sales_only_change
from selection_automation.vision import vision_revision
from test_codex_visual_review import fixture as review_fixture
from test_set_inventory_identity import configured, facts
from test_vision_cache_identity import fixture as image_fixture


@pytest.fixture
def saved_review(tmp_path):
    category=configured()
    pair=image_fixture(tmp_path)
    pair.update(amazon_title='Six knives',temu_title='Eight knives',
                temu_product_facts={'fields':{'card_text':'Eight knives\n$4.94\n31\nsold\n31sold\n8 hooks'}})
    pair['vision_revision_key']=vision_revision(category,pair)
    _,review=review_fixture()
    review.update(**{k:pair[k] for k in ('asin','goods_id','vision_revision_key')},
                  visual_facts=facts(category,a=6,t=8))
    output=tmp_path/'stage123/codex_reviews/original.jsonl'
    import_reviews(category,[pair],[review],output,'original-paid-review')
    manifest=tmp_path/'stage123/codex_pool/jobs/original/manifest.jsonl'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(pair)+'\n',encoding='utf-8')
    return category,pair,output


@pytest.mark.parametrize('old,new',[('$4.94','$5.50'),('31\nsold\n31sold','42\nsold\n42sold')])
def test_price_or_sales_refresh_reuses_original_paid_facts_without_rewriting(tmp_path,saved_review,old,new):
    category,original,path=saved_review
    current=deepcopy(original)
    current['temu_product_facts']['fields']['card_text']=current['temu_product_facts']['fields']['card_text'].replace(old,new)
    current_key=vision_revision(category,current)
    assert current_key!=original['vision_revision_key']
    before=path.read_bytes()
    result=load_reviews(tmp_path,{('A','T'):{current_key}},category=category,current_pairs=[current])
    assert result[('A','T')]['review_status']=='DIFFERENT'
    assert result[('A','T')]['reused_from_vision_revision_key']==original['vision_revision_key']
    assert result[('A','T')]['model']=='Codex看图复核'
    assert path.read_bytes()==before
    assert not {'usage','api_status','response_id'} & result[('A','T')].keys()
    by_revision=load_reviews(tmp_path,by_revision=True,category=category,current_pairs=[current])
    assert ('A','T',current_key) in by_revision


@pytest.mark.parametrize('change',['title','card_design','card_quantity','sku','amazon_image','temu_image','questions','model'])
def test_changed_design_specification_images_or_model_contract_cannot_reuse(tmp_path,saved_review,change):
    category,original,_=saved_review
    current=deepcopy(original)
    current['temu_product_facts']['fields']['card_text']=current['temu_product_facts']['fields']['card_text'].replace('$4.94','$5.50')
    if change=='title':current['temu_title']='Different knife design'
    elif change=='card_design':current['temu_product_facts']['fields']['card_text']+='\nBlue handles'
    elif change=='card_quantity':current['temu_product_facts']['fields']['card_text']=current['temu_product_facts']['fields']['card_text'].replace('8 hooks','12 hooks')
    elif change=='sku':current['temu_product_facts']['fields']['size']='12 inches'
    elif change.endswith('_image'):(tmp_path/('amazon.jpg' if change=='amazon_image' else 'temu.jpg')).write_bytes(b'changed-image')
    elif change=='questions':category['stage3']['module_descriptions']['body']+=' Different visible body question.'
    elif change=='model':category['stage3']['model']='different-model'
    current_key=vision_revision(category,current)
    assert load_reviews(tmp_path,{('A','T'):{current_key}},category=category,current_pairs=[current])=={}


def test_missing_original_manifest_does_not_authorize_rebinding(tmp_path,saved_review):
    category,original,_=saved_review
    current=deepcopy(original)
    current['temu_product_facts']['fields']['card_text']=current['temu_product_facts']['fields']['card_text'].replace('$4.94','$5.50')
    (tmp_path/'stage123/codex_pool/jobs/original/manifest.jsonl').unlink()
    assert load_reviews(tmp_path,{('A','T'):{vision_revision(category,current)}},category=category,current_pairs=[current])=={}


def test_only_complete_price_and_sold_lines_are_ignored():
    original={'temu_title':'Curtain','temu_product_facts':{'fields':{'card_text':'12\nhooks\nOriginal price $4.94\n31 sold'}}}
    current=deepcopy(original)
    current['temu_product_facts']['fields']['card_text']='12\nhooks\nOriginal price $5.50\n42 sold'
    assert price_sales_only_change(original,current)
    current['temu_product_facts']['fields']['card_text']='18\nhooks\nOriginal price $5.50\n42 sold'
    assert not price_sales_only_change(original,current)
