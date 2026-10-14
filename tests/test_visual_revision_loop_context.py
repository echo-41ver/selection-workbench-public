import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import export_category_workbooks as exporter
from selection_automation import commercial, stage3_progress, vision


def fixture(tmp_path):
    category = json.loads((ROOT/'config/categories/power_drill.json').read_text(encoding='utf-8'))
    base = tmp_path/'runs'/category['category_key']
    folder = base/'stage123/export_frozen';folder.mkdir(parents=True)
    images = [tmp_path/f'image-{i}' for i in range(2)]
    for image in images: image.write_bytes(b'image-old')
    pairs = [{'asin':'A','goods_id':str(i),'pair_revision':str(i),'vector_score':100,
              'pair_status':'CANDIDATE','amazon_image_path':str(images[i]),
              'temu_image_path':str(images[i])} for i in range(2)]
    def rows(path, values):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(''.join(json.dumps(v)+'\n' for v in values),encoding='utf-8')
    key = category['category_key']
    rows(folder/f'layer3_candidates_{key}.jsonl',pairs)
    rows(folder/f'layer12_rows_{key}.jsonl',pairs)
    (folder/f'layer12_summary_{key}.json').write_text(json.dumps({
        'actual_pairs':2,'amazon_products':1,'temu_products':2}),encoding='utf-8')
    rows(stage3_progress.candidate_path(base),pairs)
    rows(base/'stage123/results.jsonl',[{**p,'vision_revision_key':vision.compatible_vision_keys(category,p)[0],
                                       'api_status':'success'} for p in pairs])
    return category,base,pairs,images,rows


def count_contexts(monkeypatch,module):
    created=[];original=vision.VisionRevisionContext
    def prepare(category):
        context=original(category);created.append(context);return context
    monkeypatch.setattr(module,'VisionRevisionContext',prepare)
    return created


def test_readonly_loop_prepares_once_per_call_and_rereads_actual_image_bytes(tmp_path,monkeypatch):
    category,base,pairs,images,rows=fixture(tmp_path)
    contexts=count_contexts(monkeypatch,stage3_progress)
    first=stage3_progress.snapshot(base,category,history=False)
    assert first['completed']==2 and len(contexts)==1
    stat=images[0].stat();images[0].write_bytes(b'image-new')
    os.utime(images[0],ns=(stat.st_atime_ns,stat.st_mtime_ns))
    second=stage3_progress.snapshot(base,category,history=False)
    assert second['completed']==1 and len(contexts)==2 and contexts[0] is not contexts[1]
    category['stage3']['module_descriptions']['body']+=' Changed question.'
    third=stage3_progress.snapshot(base,category,history=False)
    assert third['completed']==0 and len(contexts)==3


def test_focused_export_reuses_report_without_full_cartesian_export(tmp_path,monkeypatch):
    category,base,pairs,images,rows=fixture(tmp_path)
    # Avoid real workbook, report construction and image/network operations.
    (base/'stage123/results.jsonl').write_text('',encoding='utf-8')
    monkeypatch.setattr(exporter,'ROOT',tmp_path)
    monkeypatch.setattr(exporter,'load_category',lambda _:category)
    report={'products':[],'policy':{'same_limit':5}}
    monkeypatch.setattr(commercial,'current_report',lambda *args:report)
    import export_selection_result
    calls=[]
    monkeypatch.setattr(export_selection_result,'export',lambda root,cat,value:calls.append((root,cat,value)) or {'count':0})
    def forbidden(*args,**kwargs):raise AssertionError('Full Cartesian Excel must not be generated')
    monkeypatch.setattr(exporter,'export_stage_book',forbidden)
    monkeypatch.setattr(exporter,'compatible_vision_keys',forbidden)
    for kind in ('layer12','layer123'):
        assert exporter.export(base/'category.json',kind)['count']==0
    assert calls==[(tmp_path,category,report)]*2


def test_export_script_changes_do_not_invalidate_commercial_evidence_signature(tmp_path):
    category,base,*_=fixture(tmp_path)
    before=commercial.report_signature(tmp_path,category)
    script=tmp_path/'scripts/export_category_workbooks.py';script.parent.mkdir()
    script.write_text('local export-only change',encoding='utf-8')
    assert commercial.report_signature(tmp_path,category)==before
