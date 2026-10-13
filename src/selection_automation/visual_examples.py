"""Optional reviewed visual examples; never reuse a target as its own answer."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT/'assets/identity_examples'


def select_examples(category, target_paths):
    examples = category['stage3'].get('reference_examples',[])
    if not isinstance(examples,list) or len(examples)>8:
        raise ValueError('reference examples must be a bounded list')
    if not examples:
        return []
    targets = {hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in target_paths}
    selected = []
    for label in ['SAME','DIFFERENT']:
        for row in examples:
            if not isinstance(row,dict) or set(row)!={'label','reason','source','amazon_image_path','temu_image_path','amazon_sha256','temu_sha256'}:
                raise ValueError('invalid reviewed example fields')
            if row['label'] not in {'SAME','DIFFERENT'} or not all(isinstance(row[k],str) and row[k].strip() for k in row):
                raise ValueError('invalid reviewed example label or evidence')
            if row['label'] != label:
                continue
            hashes = []
            for side in ['amazon','temu']:
                path=(ROOT/row[f'{side}_image_path']).resolve()
                if not path.is_relative_to(ASSETS.resolve()):
                    raise ValueError('reviewed examples must be portable approved image assets')
                actual=hashlib.sha256(path.read_bytes()).hexdigest()
                if actual!=row[f'{side}_sha256']:
                    raise ValueError('reviewed example image changed')
                hashes.append(actual)
            if targets & set(hashes):
                continue
            selected.append(dict(row))
            break
    return selected


def example_content(category, target_paths):
    from selection_workflow.bath_towel_deepseek import _data_url
    content=[]
    for i,row in enumerate(select_examples(category,target_paths)):
        content.append({'type':'input_text','text':f'规则参考例{i+1}（不是待分析C候选）：判定{row["label"]}；依据：{row["reason"]}。仅帮助理解边界，当前商品必须独立看图。'})
        for side in ['amazon','temu']:
            content.extend([{'type':'input_text','text':f'参考例{i+1} {side}'},
                            {'type':'input_image','image_url':_data_url(ROOT/row[f'{side}_image_path']),'detail':'high'}])
    return content
