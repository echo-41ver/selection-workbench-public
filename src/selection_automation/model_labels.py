"""Display model names from native catalogs without guessing the underlying model."""
import json
from pathlib import Path


def model_label(value,root=None,agent_id=None):
    text=str(value or '').strip()
    if not text:return '未记录'
    for encoding in ('gbk','latin1'):
        try:fixed=text.encode(encoding).decode('utf-8')
        except (UnicodeError,LookupError):continue
        if fixed!=text and any('\u4e00'<=char<='\u9fff' for char in fixed):text=fixed;break
    root=Path(root) if root else Path(__file__).resolve().parents[2]
    for path in (root/'runtime/agent-config/model-catalogs').glob('*.json'):
        if agent_id and path.stem!=agent_id:continue
        try:catalog=json.loads(path.read_text('utf-8'))
        except (OSError,ValueError):continue
        for model in catalog.get('models',[]):
            if model.get('id')==text:return model.get('name') or text
    return '模型名称读取失败' if '\ufffd' in text or text.count('?')>1 else text
