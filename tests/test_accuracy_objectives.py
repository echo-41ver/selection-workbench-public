"""Cross-category accuracy guards and real pipeline routing; no API requests."""
from category_fixtures import category_path
import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from identity_fixtures import identity
from selection_automation.rules import text_gate, score
from selection_automation.vision import decode, parse_visual_response
from selection_workflow.incremental_stage12 import IncrementalStage12, PathSpec, SourceSpec
from selection_workflow.matching_views import image_views, subject_view, similarity_scores, VECTOR_REVISION, FEATURE_DIM

ROOT=Path(__file__).resolve().parents[1]


def test_single_json_fence_preserves_fields_and_extra_prose_is_rejected():
    raw='{"candidates":[],"extra":"still present"}'
    assert parse_visual_response('```json\n'+raw+'\n```')==json.loads(raw)
    with pytest.raises(ValueError):parse_visual_response('My answer:\n```json\n'+raw+'\n```')
    with pytest.raises(ValueError):parse_visual_response('```json\n{"candidates":\n```')


def category():
    cat=json.loads((ROOT/'config/categories/power_drill.json').read_text('utf-8'))
    cat['stage1']['attributes']=[{'name':'material','hard_conflict':True,
       'values':{'cotton':['cotton','纯棉'],'polyester':['polyester','聚酯']},'ambiguous_phrases':[]}]
    return cat


@pytest.mark.parametrize('left,right',[
    ('not made of cotton; polyester','cotton'),('without cotton','cotton'),
    ('not 100% cotton','polyester'),('cotton-free','polyester'),('free from cotton','polyester'),
    ('non-cotton','cotton'),('不是纯棉 聚酯','纯棉'),('cotton-like polyester','cotton'),
    ('polyester with cotton feel','cotton'),('仿纯棉 聚酯','纯棉'),('纯棉质感 聚酯','纯棉'),
    ('cotton / polyester SKU choices','cotton'),('', 'polyester')])
def test_first_layer_never_excludes_negated_appearance_missing_or_multi_value(left,right):
    result=text_gate(category(),left,right)
    assert not result['excluded'] and result['stage1_status']=='TEXT_INSUFFICIENT'


def test_explicit_material_conflict_still_excludes_and_variants_do_not():
    cat=category()
    assert text_gate(cat,'cotton','polyester')['excluded']
    cat['stage3']['variant_fields']=['color']
    cat['stage1']['attributes']=[{'name':'color','hard_conflict':True,'values':{'red':['red'],'blue':['blue']}}]
    assert not text_gate(cat,'red','blue')['excluded']


def test_legacy_towel_and_shower_gates_share_conservative_context_rules():
    from selection_workflow.bath_towel_text_gate_v1 import evaluate_text_gate as towel
    from selection_workflow.shower_text_gate_v2 import evaluate_text_gate as shower
    assert not towel('not cotton bath towels set','polyester bath towels set')['hard_conflicts']
    assert towel('cotton bath towels set','polyester bath towels set')['hard_conflicts']
    assert not shower('not PVC shower curtain','polyester shower curtain')['hard_conflicts']
    assert shower('PVC shower curtain','polyester shower curtain')['hard_conflicts']


def features(first, second, third):
    a=np.zeros((3,FEATURE_DIM),dtype=np.float64)
    a[:,:2]=np.array([first,second,third]);return a.reshape(-1)


def test_multiview_keeps_legacy_recall_and_recovers_whole_item_match():
    a=features([1,0],[0,1],[1,1]);b=features([.3,.954],[0,1],[-1,1])
    legacy=similarity_scores(a[None,:FEATURE_DIM],b[None,:FEATURE_DIM],'legacy')[0]
    new=similarity_scores(a[None],b[None],VECTOR_REVISION)[0]
    assert legacy<60 and new>=99.99 and new>=legacy
    rng=np.random.default_rng(30)
    a=rng.normal(size=(16,3*FEATURE_DIM));b=rng.normal(size=a.shape)
    old=np.einsum('ij,ij->i',a[:,:FEATURE_DIM]/np.linalg.norm(a[:,:FEATURE_DIM],axis=1)[:,None],
                 b[:,:FEATURE_DIM]/np.linalg.norm(b[:,:FEATURE_DIM],axis=1)[:,None])*100
    assert np.all(similarity_scores(a,b,VECTOR_REVISION)>=old-1e-10)


def test_complete_item_view_keeps_both_ends_of_long_product():
    image=Image.new('RGB',(600,100),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((0,20,40,80),fill='red');draw.rectangle((560,20,599,80),fill='blue')
    _,whole,_=image_views(image);pixels=np.asarray(whole)
    assert ((pixels[:,:,0]>200)&(pixels[:,:,2]<50)).any()
    assert ((pixels[:,:,2]>200)&(pixels[:,:,0]<50)).any()
    # A varied scene border is not represented as reliable subject detection.
    scene=Image.new('RGB',(100,100),'red');ImageDraw.Draw(scene).rectangle((50,0,99,99),fill='blue')
    assert subject_view(scene).size==scene.size


@pytest.mark.parametrize('filename',['power_drill.json','bath_towel_set.json','shower_curtain_local_union_20260922.json'])
def test_third_layer_cannot_confirm_common_parts_with_similar_design(filename):
    cat=json.loads(category_path(filename).read_text('utf-8'))
    fact={'identity':identity(),'overall_confidence':.95,'modules':{
        key:{'relation':'SAME','amazon_evidence':'圆弧主体和连接件','temu_evidence':'圆弧主体和连接件','hard_conflict_code':'NONE'}
        for key in cat['stage3']['modules']}}
    assert score(cat,fact)['state']=='确认同款'
    fact['identity']['design_comparison']['relation']='SIMILAR'
    assert score(cat,fact)['state']=='待复核'
    fact['identity']['design_comparison'].update(relation='DIFFERENT',amazon='左侧长方形接口及圆弧凸缘',temu='左侧圆孔接口，无凸缘')
    assert score(cat,fact)['state']=='不同款'
    del fact['identity']['design_comparison']
    assert score(cat,fact)['state']=='待复核'


@pytest.mark.parametrize('invalid',[None,{}, {'relation':'SAME','amazon':'detail'},
                                    {'relation':'SAME','amazon':True,'temu':'detail'}])
def test_current_decoder_rejects_missing_or_malformed_design(invalid):
    cat=category();proof=identity();proof['design_comparison']=invalid
    raw={'candidates':[{'i':'T','c':.9,'v':proof,'m':{str(i):{'r':'S','a':'主体弧形','t':'主体弧形','h':'NONE'} for i in range(len(cat['stage3']['modules']))}}]}
    with pytest.raises(ValueError):decode(cat,raw,['T'])


@pytest.mark.parametrize('zero_view',[False,True])
def test_new_view_recall_and_invalid_views_reach_real_candidate_queue(tmp_path,zero_view):
    class Vectors:
        revision=VECTOR_REVISION
        def encode(self,path):
            with Image.open(path) as image:is_a=image.getpixel((0,0))[0]==255
            vec=features([1,0],[0,1],[1,1]) if is_a else features([.3,.954],[0,1],[-1,1])
            if zero_view and not is_a:vec[FEATURE_DIM:2*FEATURE_DIM]=0
            return vec
    sources=[]
    for side,color,pid in [('amazon','red','A'),('temu','green','T')]:
        image=tmp_path/f'{side}.png';Image.new('RGB',(32,32),color).save(image)
        data=tmp_path/f'{side}.jsonl';data.write_text(json.dumps({('asin' if side=='amazon' else 'goods_id'):pid,
            'title':'shower curtain','local_image_path':str(image)})+'\n')
        sources.append(SourceSpec(side,'AMAZON' if side=='amazon' else 'TEMU','amazon_generic' if side=='amazon' else 'temu_frontend',data))
    pipeline=IncrementalStage12(tmp_path/'output',threshold=60,vectorizer=Vectors())
    try:
        pipeline.configure(sources,[PathSpec('pair','amazon','temu','test')])
        for s in sources:pipeline.ingest_source(s.key)
        pipeline.process_batch()
        row=pipeline.con.execute('SELECT * FROM pairs').fetchone()
        if zero_view:
            assert row['status']=='FAILED_RETRYABLE' and row['vector_score'] is None
        else:
            assert row['status']=='CANDIDATE' and row['vector_score']>=99.99
            assert pipeline.con.execute('SELECT count(*) FROM candidates').fetchone()[0]==1
    finally:pipeline.close()


@pytest.mark.parametrize('own,related', [
    ('cotton towel', 'pair it with your favorite polyester curtain'),
    ('cotton pet bowl cover', 'pair this with a polyester travel bag'),
    ('cotton storage liner', 'layer with a polyester bag'),
])
def test_related_other_item_material_is_not_own_material(own, related):
    cat = category()
    result = text_gate(cat, own + '; ' + related, 'cotton item')
    assert not result['excluded']
    assert result['facts'][0]['amazon'] == ['cotton']
    # The reference cannot hide a real own-item contradiction either.
    result = text_gate(cat, 'polyester item; pair it with a cotton towel', 'cotton item')
    assert result['excluded']


@pytest.mark.parametrize('left,right,excluded', [
    ('PEVA shower liner; pair it with your favorite fabric shower curtain', 'PEVA shower liner', False),
    ('polyester shower curtain; layer with a clear PEVA liner', 'PEVA shower liner', True),
    ('PVC shower curtain', 'polyester shower curtain', True),
    ('not PVC shower curtain', 'polyester shower curtain', False),
    ('cotton bath towel set; pair it with a polyester robe', 'cotton bath towel set', False),
    ('polyester bath towel set; pair it with cotton towels', 'cotton bath towel set', True),
])
def test_legacy_gates_retain_own_material_and_ignore_other_items(left,right,excluded):
    from selection_workflow.shower_text_gate_v2 import evaluate_text_gate as shower
    from selection_workflow.bath_towel_text_gate_v1 import evaluate_text_gate as towel
    gate = shower if 'shower' in left else towel
    assert (gate(left, right)['stage1_status'] == 'TEXT_HARD_EXCLUDE') is excluded


def test_own_named_layer_and_complete_bundle_are_not_other_item_instructions():
    from selection_automation.rules import attribute_values
    aliases = {'cotton':['cotton'], 'polyester':['polyester']}
    assert attribute_values('double layer with cotton lining', aliases)[0] == {'cotton'}
    assert attribute_values('inner layer with polyester lining', aliases)[0] == {'polyester'}
    assert attribute_values('cotton robe supplied paired with polyester towels', aliases)[0] == {'cotton','polyester'}


@pytest.mark.parametrize('body', ['Cotton cover with a waterproof layer with polyester lining',
    'Cotton pet bowl cover with an absorbent inner layer with polyester lining',
    'Cotton storage liner with the reinforced protective layer with polyester lining'])
def test_modified_own_layer_material_is_not_discarded(body):
    from selection_automation.rules import attribute_values
    assert attribute_values(body, {'cotton':['cotton'], 'polyester':['polyester']})[0] == {'cotton', 'polyester'}
    result = text_gate(category(), body, 'Polyester cover')
    assert not result['excluded']
