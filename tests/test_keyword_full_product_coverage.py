import sys,pathlib,pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from pipeline import build_category_keyword_plan,keyword_coverage
def cat(base,limit=2):return {'temu':{'base_queries':[base],'keyword_limit':limit,'keywords_per_product':1,'max_products_per_keyword':100,'keyword_features':[]}}
@pytest.mark.parametrize('base',['projector','dog bowl','travel bag'])
def test_shared_keyword_binds_all_30_even_when_generated_after_per_product_limit(base):
 rows=[{'asin':f'A{i:02}','rank':i+1,'title':f'Green {base}'+(f' {70+i}cm' if i>=8 else '')} for i in range(30)]
 plan=build_category_keyword_plan(rows,cat(base));assert len(plan)==2
 shared=next(p for p in plan if p['query']==f'green {base}')
 assert set(shared['source_asins'])=={r['asin'] for r in rows}
 assert len(shared['source_evidence'])==30
 assert keyword_coverage(rows,plan)['specific_products']==30
def test_30_products_can_share_multiple_keywords_without_invented_bindings():
 rows=[{'asin':f'A{i:02}','rank':i+1,'title':f'{"Green" if i<15 else "Blue"} projector'+(f' {70+i}cm' if i%15>=8 else '')} for i in range(30)]
 # Include plain-title product representatives so the already planned shared queries exist.
 rows[0]['title']='Green projector';rows[15]['title']='Blue projector'
 plan=build_category_keyword_plan(rows,cat('projector',3));assert len(plan)==3
 assert keyword_coverage(rows,plan)['specific_products']==30
 for color in ('green','blue'):
  p=next(p for p in plan if p['query']==f'{color} projector')
  expected={r['asin'] for r in rows if color in r['title'].lower()}
  assert set(p['source_asins'])==expected

def test_featureless_titles_remain_truthfully_without_specific_query():
 rows=[{'asin':f'A{i:02}','title':'Projector'} for i in range(30)]
 plan=build_category_keyword_plan(rows,cat('projector'));cov=keyword_coverage(rows,plan)
 assert cov['amazon_products']==30 and cov['evidenced_products']==30 and cov['specific_products']==0
 assert len(cov['without_specific_query'])==30 and len(plan)==1
 assert cov['measurement']=='PLANNED_TITLE_EVIDENCE_NOT_SEARCH_EXECUTION'
def test_legacy_limit_cannot_drop_evidenced_product_queries():
 rows=[{'asin':f'A{i:02}','title':f'Green projector {70+i}cm'} for i in range(30)]
 plan=build_category_keyword_plan(rows,cat('projector',1));assert len(plan)>1
 assert keyword_coverage(rows,plan)['specific_products']==30
def test_incremental_later_22_products_enrich_existing_shared_query():
 category=cat('projector');first=[{'asin':f'A{i:02}','title':'Green projector'} for i in range(8)]
 assert keyword_coverage(first,build_category_keyword_plan(first,category))['specific_products']==8
 final=first+[{'asin':f'A{i:02}','title':f'Green projector {70+i}cm'} for i in range(8,30)]
 assert keyword_coverage(final,build_category_keyword_plan(final,category))['specific_products']==30

@pytest.mark.parametrize('legacy_limit',[None,1,20,60])
def test_unique_models_are_retained_without_a_fixed_total_cap(legacy_limit):
 rows=[{'asin':f'U{i:03}','title':f'Travel bag Model MX{i+100}'} for i in range(80)]
 category=cat('travel bag',legacy_limit)
 plan=build_category_keyword_plan(rows,category)
 assert len(plan)==81
 assert keyword_coverage(rows,plan,category)['specific_products']==80
 assert len({p['query'] for p in plan})==len(plan)

def test_redundant_color_size_combinations_do_not_add_searches():
 rows=[{'asin':f'R{i}','title':f'Green travel bag {i+20}cm'} for i in range(80)]
 rows[0]['title']='Green travel bag'
 plan=build_category_keyword_plan(rows,cat('travel bag',1))
 assert len(plan)==2
 assert plan[1]['query']=='green travel bag'
 assert len(plan[1]['source_asins'])==80

def test_shared_product_coverage_does_not_hide_distinct_structures():
 category=cat('travel bag',1);category['temu']['keywords_per_product']=6
 rows=[{'asin':'F','title':'Green foldable travel bag'},
       {'asin':'M','title':'Green magnetic travel bag'},
       {'asin':'N','title':'Green travel bag'}]
 plan=build_category_keyword_plan(rows,category)
 assert keyword_coverage(rows,plan,category)['specific_products']==3
 assert any('foldable' in p['query'] for p in plan)
 assert any('magnetic' in p['query'] for p in plan)
 assert len(plan)==4
