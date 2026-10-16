import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from selection_automation.response_envelope import parse_envelope,NotApplicable
from selection_automation.vision import parse_visual_response,parse_visual_response_compatible,decode
@pytest.mark.parametrize('text',[json.dumps({'candidates':[{'i':'C00','visible_fact':'text } [ inside evidence'}]}),'  {"candidates": []}  '])
def test_canonical_facts_unchanged(text):
    value,changes=parse_envelope(text);assert value==json.loads(text) and changes==[]
def test_one_extra_final_brace_removal_preserves_complete_candidate():
    value={'candidates':[{'i':'C00','m':{'0':{'r':'D','a':'unchanged left','t':'unchanged right','h':'NONE'}}}]}
    original=json.dumps(value);position=original.rfind(']');malformed=original[:position]+'}'+original[position:]
    parsed,changes=parse_envelope(malformed);assert parsed==value and changes[0]['operation']=='REMOVE_ONE_EXTRA_CONTAINER_CLOSING_BRACE'
@pytest.mark.parametrize('text',['{"candidates":[{"i":"C00"}]} trailing','{"candidates":[{"i":"C00"}}}]}','{"candidates":[{"i":"C00"','{"candidates":[{"i":"C00"} {"i":"C01"}]}'])
def test_unrecognized_or_incomplete_syntax_never_repaired(text):
    with pytest.raises((ValueError,NotApplicable)):parse_envelope(text)
def test_duplicate_fact_keys_never_discarded():
    with pytest.raises(ValueError,match='Duplicate'):parse_envelope('{"candidates":[{"i":"C00","i":"C01"}]}')
@pytest.mark.parametrize('annotation',['object','DIFFERENT'])
def test_root_extra_field_never_discarded(annotation):
    original={'candidates':[],'type':annotation};parsed,changes=parse_envelope(json.dumps(original));assert parsed==original and not changes
    with pytest.raises(ValueError,match='invalid visual response object'):decode({'stage3':{}},parsed,[])
def test_no_missing_visual_modules_filled():
    text='{"candidates":[{"i":"C00","m":{},"v":{},"c":0.9}]}';parsed,changes=parse_visual_response_compatible(text)
    cat={'stage3':{'modules':{'body':100}}}
    with pytest.raises(ValueError,match='incomplete module set'):decode(cat,parsed,['C00'])
    assert parsed['candidates'][0]['m']=={} and changes==[]
@pytest.mark.parametrize('text',[
    'Analysis {"candidates":[{"i":"C00","i":"C01"}]}',
    '```json\n{"candidates":[{"i":"C00","i":"C01"}]}\n```',
    'Analysis ```json\n{"candidates":[{"i":"C00","i":"C01"}]}\n```',
    '{"candidates":[],"candidates":[{"i":"C00"}]}',
    'Analysis {"candidates":[],"candidates":[{"i":"C00"}]}',
    '```json\n{"candidates":[],"candidates":[{"i":"C00"}]}\n```',
    '{"candidates":[{"m":{"0":{"r":"D","r":"S"}}}]}',
    'Analysis {"candidates":[{"f":{"body":{"a":"left","t":"right","t":"other"}}}]}',
])
@pytest.mark.parametrize('parser',[parse_visual_response,parse_visual_response_compatible])
def test_every_parser_path_rejects_duplicate_keys(text,parser):
    with pytest.raises(ValueError,match='Duplicate'):parser(text,allow_preamble=True)
def test_preamble_without_duplicates_remains_supported():
    expected={'candidates':[{'i':'C00','m':{'0':{'r':'D'}}}]}
    value,changes=parse_visual_response_compatible('Analysis '+json.dumps(expected),allow_preamble=True)
    assert value==expected and changes==[]
