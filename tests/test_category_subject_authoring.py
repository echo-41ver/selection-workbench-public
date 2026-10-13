import pytest
from generate_category import CategoryGenerationError,ensure_subject_rules,validate_subject_rules


def policy(name,en,other):
    return {'category_name_zh':name,'temu':{'category_aliases':[name,en],
      'subject_filter':{'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':True,'exclude_subject_terms':[other]}}}


@pytest.mark.parametrize('name,en,other',[('鼠标','computer mouse','keyboard'),('锅铲','spatula','soup ladle'),
    ('电钻','power drill','power saw'),('手机壳','phone case','charging cable')])
def test_new_categories_all_require_their_own_complete_subject_rules(name,en,other):
    config=policy(name,en,other)
    assert validate_subject_rules(config) is config
    assert ensure_subject_rules(config,{},model=lambda *a,**k:pytest.fail('complete rules do not need an extra model')) is config


@pytest.mark.parametrize('patch',[{'category_aliases':[]},{'category_aliases':['mouse']},{'subject_filter':{}},
    {'subject_filter':{'mode':'TARGET_BODY_ONLY','reject_mixed_subjects':False,'exclude_subject_terms':['keyboard']}}])
def test_incomplete_rules_do_not_silently_allow_unfiltered_collection(patch):
    config=policy('鼠标','mouse','keyboard');config['temu'].update(patch)
    with pytest.raises(CategoryGenerationError):ensure_subject_rules(config,{},allow_model=False)


def test_legacy_rules_receive_one_bounded_category_specific_supplement_without_a_template():
    calls=[];config={'category_name_zh':'锅铲','temu':{'include_terms':['spatula']}}
    proposal=policy('锅铲','spatula','soup ladle')['temu']
    def model(prompt,**kw):calls.append(prompt);return proposal
    result=ensure_subject_rules(config,{'heading':'Kitchen Spatulas'},model=model)
    assert len(calls)==1 and 'Kitchen Spatulas' in calls[0]
    assert result['temu']['category_aliases']==['锅铲','spatula']
    assert 'category_aliases' not in config['temu']


def test_conflicting_body_terms_are_rejected():
    config=policy('手机壳','phone case','phone case')
    with pytest.raises(CategoryGenerationError) as error:validate_subject_rules(config)
    assert error.value.code=='CATEGORY_SUBJECT_RULES_CONFLICT'


def test_copied_prompt_fields_are_not_treated_as_concrete_excluded_products():
    config=policy('锅铲','spatula','品牌')
    with pytest.raises(CategoryGenerationError,match='占位词'):validate_subject_rules(config)
