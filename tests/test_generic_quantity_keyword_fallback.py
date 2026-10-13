import pathlib,json,sys,pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from pipeline import build_category_keyword_plan,keyword_coverage
from selection_automation.product_keywords import candidates
BASES=['facial tissue','box tissue','pocket tissue','travel tissue','paper facial tissue']
def test_all_22_real_empty_candidate_titles_have_literal_fallback_within_budget():
 rows=json.loads((pathlib.Path(__file__).parent/'fixtures/xiaomi_keyword_empty_titles_22.json').read_text(encoding='utf-8'))
 assert all(candidates(r,BASES) for r in rows)
 category={'temu':{'base_queries':BASES,'keyword_features':[],'keywords_per_product':6,'keyword_limit':20,'max_products_per_keyword':400}}
 plan=build_category_keyword_plan(rows,category)
 assert keyword_coverage(rows,plan)['specific_products']==22
 assert not any('kleenex' in p['query'] or 'puffs' in p['query'] or 'premium' in p['query'] for p in plan)
 assert all(p.get('source_evidence') for p in plan[1:])
@pytest.mark.parametrize('title,base,expected',[
 ('Tea 12 Boxes',['tea'],'12 box'),('Soap 4 Travel Bottles',['soap'],'4 bottle'),
 ('Storage Container 8 Round Jars',['storage container'],'8 round jar'),
 ('Battery 120 Count',['battery'],'120 count'),('Lamp 2 Packs',['lamp'],'2 pack'),
 ('Battery 6 x 4 Batteries',['battery'],'6 x 4 count'),
])
def test_quantities_are_generic_title_evidence(title,base,expected):
 features=[f for r in candidates({'title':title},base) for f in r['features']]
 assert any(f['value']==expected and f.get('scope') for f in features)
@pytest.mark.parametrize('title',['Tea without 12 Boxes','Battery not 120 Count','Soap excluding 4 Bottles','Tea free 12 Boxes','Lamp bonus 4 pack','Battery get 120 Count'])
def test_negated_quantities_are_not_positive_search_evidence(title):
 rows=candidates({'title':title},[title.split()[0].lower()]);assert not [f for r in rows for f in r['features'] if f['kind'] in ('quantity','packaging_quantity','count')]
def test_no_supported_feature_is_still_unknown_not_fabricated():
 assert candidates({'title':'Brand Trusted Premium Facial Tissues'},['facial tissue'])==[]
def test_per_container_contents_are_not_misread_as_container_quantity():
 output=candidates({'title':'Facial Tissues 8 Cube Boxes, 80 Tissues per Box'},['facial tissue'])
 values={f['value'] for r in output for f in r['features']}
 assert '8 cube box' in values and '80 box' not in values
 assert not candidates({'title':'Tea 80 leaves per Box'},['tea'])
def test_30_product_simulation_keeps_budget_and_all_real_22_missing_rows():
 rows=json.loads((pathlib.Path(__file__).parent/'fixtures/xiaomi_keyword_empty_titles_22.json').read_text(encoding='utf-8'))
 # The original covered eight titles were not supplied. These eight are
 # explicitly synthetic controls, not claimed Xiaomi records.
 rows += [{'asin':f'SYNTHETIC{i}','title':'White Facial Tissue 6 pack'} for i in range(8)]
 cat={'temu':{'base_queries':BASES,'keyword_features':[],'keywords_per_product':6,'keyword_limit':20,'max_products_per_keyword':400}}
 plan=build_category_keyword_plan(rows,cat);assert len(plan)<20
 assert keyword_coverage(rows,plan,cat)['specific_products']==30
def test_missing_feature_and_budget_gaps_are_distinguished():
 cat={'temu':{'base_queries':['projector'],'keyword_features':[],'keyword_limit':1,'max_products_per_keyword':100}}
 rows=[{'asin':'EMPTY','title':'Projector'},{'asin':'BUDGET','title':'Green projector'}]
 cov=keyword_coverage(rows,build_category_keyword_plan(rows,cat),cat)
 assert cov['coverage_gap_counts']=={'NO_SUPPORTED_TITLE_FEATURES':1}
 assert cov['specific_products']==1
@pytest.mark.parametrize('bad',['flat',[''],[None]])
def test_invalid_feature_contract_stops_before_planning(bad):
 with pytest.raises(ValueError,match='keyword_features'):build_category_keyword_plan([],{'temu':{'keyword_features':bad}})

@pytest.mark.parametrize('title',['Tea 12 Boxes (not included)','Tea 12 Boxes excluded','Tea 12 Boxes (excluded)','Tea 12 Boxes are not included'])
def test_adjacent_suffix_exclusion_is_not_positive_quantity(title):
 assert not [f for row in candidates({'title':title},['tea']) for f in row['features'] if f['kind'] in ('quantity','packaging_quantity','count')]
@pytest.mark.parametrize('title',['Tea 12 Boxes, 4 Bottles (not included)','Tea 4 Bottles excluded, 12 Boxes','Tea 12 Boxes (included)'])
def test_excluded_other_quantity_does_not_erase_affirmative_clause(title):
 values={f['value'] for row in candidates({'title':title},['tea']) for f in row['features']}
 assert '12 box' in values and '4 bottle' not in values
