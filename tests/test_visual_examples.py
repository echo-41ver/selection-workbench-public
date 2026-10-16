"""Reviewed examples may calibrate a category, but cannot label themselves."""
import hashlib
import pytest
from selection_automation import visual_examples as examples


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(examples,'ROOT',tmp_path)
    assets=tmp_path/'assets/identity_examples';assets.mkdir(parents=True)
    monkeypatch.setattr(examples,'ASSETS',assets)
    rows=[]
    for i,label in enumerate(['SAME','SAME','DIFFERENT']):
        row={'label':label,'reason':'主体区域布局保持或明确改变','source':'reviewed'}
        for side in ['amazon','temu']:
            p=assets/f'{i}_{side}.img';p.write_bytes(f'{i}_{side}'.encode())
            row[side+'_image_path']=str(p.relative_to(tmp_path))
            row[side+'_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append(row)
    return {'stage3':{'reference_examples':rows}},assets


def test_shared_image_excludes_entire_answer_example(tmp_path,monkeypatch):
    c,assets=setup(tmp_path,monkeypatch)
    selected=examples.select_examples(c,[assets/'0_amazon.img'])
    assert len(selected)==2
    assert selected[0]==c['stage3']['reference_examples'][1]
    assert selected[1]['label']=='DIFFERENT'


def test_tampered_image_and_external_path_are_rejected(tmp_path,monkeypatch):
    c,assets=setup(tmp_path,monkeypatch)
    (assets/'0_amazon.img').write_bytes(b'changed')
    with pytest.raises(ValueError,match='image changed'):examples.select_examples(c,[])
    c['stage3']['reference_examples'][0]['amazon_image_path']='../../private.img'
    with pytest.raises(ValueError,match='portable approved'):examples.select_examples(c,[])


def test_empty_library_preserves_existing_request_behavior():
    assert examples.select_examples({'stage3':{}},['not-read.img'])==[]
