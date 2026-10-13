"""A new category must feed the proven four-worker queue from Amazon evidence."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pipeline import build_category_keyword_plan, keyword_coverage
from selection_automation.product_keywords import candidates
import json
import pytest


@pytest.mark.parametrize('value,expected',[
    ('10,000mAh','10000 mah'),('20,000 mAh','20000 mah'),
    ('1,000.5ml','1000.5 ml'),('10，000mAh','10000 mah'),
    ('10000mAh','10000 mah'),('12.5V','12.5 v'),('1,234,567Hz','1234567 hz'),
])
def test_valid_numeric_groups_keep_entire_capacity(value,expected):
    rows=candidates({'title':'Power Bank '+value},['power bank'])
    parameters={f['value'] for row in rows for f in row['features'] if f['kind']=='parameter'}
    assert parameters=={expected}


@pytest.mark.parametrize('value',['12,34mAh','1,000,00mAh','ABC10,000mAh','12.5,000mAh'])
def test_malformed_or_embedded_numeric_groups_do_not_create_suffix_parameters(value):
    rows=candidates({'title':'Power Bank '+value},['power bank'])
    assert not [f for row in rows for f in row['features'] if f['kind']=='parameter']


def test_pet_bowl_real_titles_have_concrete_coverage_for_all_ten_products():
    rows=json.loads((Path(__file__).parent/'fixtures/amazon_pet_bowls_keywords.json').read_text('utf-8'))
    category={'temu':{'base_queries':['pet food bowl','dog bowl','cat bowl','raised pet bowl','slow feeder bowl','travel pet bowl'],
        'keyword_limit':20,'max_products_per_keyword':500,'keyword_features':[]}}
    plan=build_category_keyword_plan(rows,category)
    assert len(plan)<20
    assert keyword_coverage(rows,plan)['specific_products']==10
    assert {e['asin'] for p in plan for e in p['source_evidence'] if e.get('title')}=={r['asin'] for r in rows}
    assert any('collapsible' in p['query'] for p in plan)
    for p in plan:
        if 'B09XXPCB4T' in p['source_asins']:
            assert 'puzzle' in p['query'] and 'bowl' not in p['query']


def test_colors_dimensions_models_and_plural_titles_need_no_category_dictionary():
    rows=candidates({'title':'Green Dog Bowls 70cm Model MX70'},['dog bowl'])
    assert any('green 70 cm' in r['query'] for r in rows)
    assert any('mx70' in r['query'] for r in rows)
    assert all('dog bowl' in r['query'] for r in rows)
    assert all('red' not in r['query'] for r in rows)


def test_one_amazon_drill_title_drops_unseen_seed_but_keeps_evidenced_terms():
    category={'stage1':{'text_gate':'declarative'},'temu':{
        'base_queries':['power drill','cordless drill','electric drill','drill driver'],
        'keyword_limit':20,'max_products_per_keyword':3,'include_terms':[],
        'keyword_features':[],'severe_exclude_terms':[]}}
    rows=[{'asin':'B00ET5VMTU','rank':1,'title':'DEWALT 20V Max Cordless Drill Driver Set'}]
    plan=build_category_keyword_plan(rows,category)
    assert plan[0]['query']=='power drill'
    assert any('cordless' in row['query'] for row in plan)
    assert 'electric drill' not in [p['query'] for p in plan]
    assert keyword_coverage(rows,plan)['specific_products']==1
    assert plan[0]['purpose']=='CATEGORY_SAMPLE' and plan[0]['source_asins']==[]
    assert all(row['source_asins']==['B00ET5VMTU'] for row in plan[1:])


def test_selected_base_query_that_overlaps_title_keeps_triple_category_budget():
    bases=['fuel transfer pump','portable fuel transfer pump','electric siphon pump']
    category={'stage1':{'text_gate':'declarative'},'temu':{
        'base_queries':bases,'keyword_limit':20,'max_products_per_keyword':400,
        'keywords_per_product':6,'keyword_features':[],'include_terms':[],'severe_exclude_terms':[]}}
    rows=[{'asin':'A1','rank':1,'title':'Fuel transfer pump'},
          {'asin':'A2','rank':2,'title':'Electric siphon pump'},
          {'asin':'A3','rank':3,'title':'Blue fuel transfer pump 12V'}]
    plan=build_category_keyword_plan(rows,category)
    by_query={item['query']:item for item in plan}
    assert by_query['fuel transfer pump']['purpose']=='CATEGORY_SAMPLE'
    assert by_query['fuel transfer pump']['max_products']==1200
    # This secondary base query adds a distinct product and remains a category search.
    assert by_query['electric siphon pump']['purpose']=='CATEGORY_SAMPLE'
    assert by_query['electric siphon pump']['max_products']==1200
    assert by_query['electric siphon pump']['source_asins']==['A2']
    assert by_query['electric siphon pump']['source_evidence'][0]['title']=='Electric siphon pump'
    # Redundant base queries still lose coverage selection; no new query is invented.
    assert 'portable fuel transfer pump' not in by_query
    assert 'blue fuel transfer pump' in by_query
    assert by_query['blue fuel transfer pump']['max_products']==400
    assert all(item['query'] in bases or item['query']=='blue fuel transfer pump' for item in plan)


def test_projector_queries_wait_for_amazon_and_keep_only_title_backed_terms():
    category = {
        'stage1': {'text_gate': 'declarative'},
        'temu': {
            'base_queries': ['projector', 'mini projector', 'portable projector',
                             'home theater projector', 'projector 4k'],
            'include_terms': ['projector', 'mini projector', 'portable projector',
                              'outdoor projector', 'projector screen'],
            'severe_exclude_terms': ['projector screen'],
            'keyword_features': [], 'keyword_limit': 20,
            'max_products_per_keyword': 100,
        },
    }
    assert [item['query'] for item in build_category_keyword_plan([], category)] == ['projector']
    rows = [
        {'asin': 'A1', 'rank': 1, 'title': 'Mini Projector with WiFi'},
        {'asin': 'A2', 'rank': 2, 'title': 'Portable Outdoor Projector 4K'},
        {'asin': 'A3', 'rank': 3, 'title': 'Mini Projector for Bedroom'},
        {'asin': 'A4', 'rank': 4, 'title': 'Portable Projector 4K for Camping'},
    ]
    plan = build_category_keyword_plan(rows, category)
    queries = [item['query'] for item in plan]
    assert queries[0]=='projector'
    assert 'mini projector' in queries and 'portable projector' in queries
    assert keyword_coverage(rows,plan)['specific_products']==4
    assert 'home theater projector' not in queries
    assert 'projector screen' not in queries
    assert all(item['source_asins'] and item['source_evidence'] for item in plan)
    assert all({entry['asin'] for entry in item['source_evidence']} == set(item['source_asins']) for item in plan)
    assert [item['max_products'] for item in plan] == [300, 300, 300]


def test_existing_towel_flow_still_starts_primary_query_while_amazon_loads():
    category = {'stage1': {'text_gate': 'bath_towel_set'}, 'temu': {
        'base_queries': ['bath towel set'], 'keyword_limit': 12,
        'max_products_per_keyword': 100, 'keywords_per_product': 6}}
    plan = build_category_keyword_plan([], category)
    assert [item['query'] for item in plan] == ['bath towel set']
    assert plan[0]['purpose'] == 'CATEGORY_SAMPLE'
    assert plan[0]['max_products'] == 300


def test_category_sample_always_survives_tight_keyword_budget_with_triple_target():
    category = {'temu': {'base_queries': ['dog bowl'], 'keyword_limit': 1,
                         'max_products_per_keyword': 500}}
    plan = build_category_keyword_plan([{'asin': 'A1', 'title': 'Blue stainless steel dog bowl'}], category)
    assert len(plan) > 1
    assert plan[0]['query'] == 'dog bowl'
    assert plan[0]['purpose'] == 'CATEGORY_SAMPLE'
    assert plan[0]['max_products'] == 1500
    assert category['temu']['max_products_per_keyword'] == 500


def test_legacy_towel_plan_keeps_category_sample_and_normal_specific_targets():
    category = {'stage1': {'text_gate': 'bath_towel_set'}, 'temu': {
        'base_queries': ['bath towel set'], 'keyword_limit': 12,
        'max_products_per_keyword': 500, 'keywords_per_product': 6}}
    rows = [{'asin': 'A1', 'title': '100% Cotton Bath Towel Set 6 Piece White'}]
    plan = build_category_keyword_plan(rows, category)
    assert plan[0]['query'] == 'bath towel set'
    assert plan[0]['purpose'] == 'CATEGORY_SAMPLE'
    assert plan[0]['max_products'] == 1500
    assert all(item['max_products'] == 500 for item in plan[1:])


def test_unique_product_feature_is_retained_and_false_substrings_rejected():
    cat={'temu':{'base_queries':['power drill'],'keyword_features':['magnetic','cord'],
                'keyword_limit':10,'max_products_per_keyword':3}}
    rows=[{'asin':'A1','rank':1,'title':'Magnetic cordless power drill'}]
    plan=build_category_keyword_plan(rows,cat)
    assert any('magnetic' in p['query'] for p in plan)
    assert 'cord power drill' not in [p['query'] for p in plan]
    assert keyword_coverage(rows,plan)['without_specific_query']==[]


def test_limited_budget_spreads_specific_queries_across_products():
    cat={'temu':{'base_queries':['projector','mini projector','wifi projector','portable projector'],
                'keyword_features':['laser'],'keyword_limit':3,'max_products_per_keyword':3}}
    rows=[{'asin':'A1','rank':1,'title':'mini projector wifi projector portable projector'},
          {'asin':'A2','rank':2,'title':'laser projector'}]
    plan=build_category_keyword_plan(rows,cat)
    assert len(plan)==3
    assert keyword_coverage(rows,plan)['specific_products']==2
    cat['temu']['keyword_limit']=1
    assert keyword_coverage(rows,build_category_keyword_plan(rows,cat))['specific_products']==2


def test_core_slow_feeding_survives_keyword_budget():
    rows=json.loads((Path(__file__).parent/'fixtures/amazon_pet_bowls_keywords.json').read_text('utf-8'))
    category={'temu':{'base_queries':['pet food bowl','dog bowl','cat bowl'],
        'keyword_limit':20,'max_products_per_keyword':500}}
    plan=build_category_keyword_plan(rows,category)
    assert any('B0787FVCBW' in p['source_asins'] and ('slow feeding' in p['query'] or 'slow feeder' in p['query']) for p in plan)
    assert not any('35 lb' in p['query'] for p in plan)


def test_parameter_range_is_preserved_and_animal_weight_excluded():
    rows=candidates({'title':'Adjustable dog bowl Medium 20–35 lbs dogs 2 cup capacity'},['dog bowl'])
    assert not any('35 lb' in p['query'] for p in rows)
    assert any('2 cup' in p['query'] for p in rows)
    rows=candidates({'title':'Adjustable Stand 20–35 cm'},['stand'])
    assert any('20-35 cm' in p['query'] for p in rows)
