"""Import one manifest-bound independent review file without paid API calls."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.category_config import load_category
from selection_automation.codex_visual_review import import_reviews
from selection_automation.jsonl_records import read_jsonl


def load_manifest(path):
    """Load either the legacy JSON manifest or the helper's JSONL pairs."""
    text = Path(path).read_text(encoding='utf-8-sig').strip()
    if not text:
        raise ValueError('empty review manifest')
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        pairs = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                pairs.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f'invalid JSONL manifest line {line_number}') from exc
        value = {'pairs': pairs}
    if isinstance(value, list):
        value = {'pairs': value}
    if not isinstance(value, dict) or not isinstance(value.get('pairs'), list) or not value['pairs']:
        raise ValueError('manifest must contain a non-empty pairs list')
    required = {'asin', 'goods_id', 'vision_revision_key', 'amazon_image_path', 'temu_image_path'}
    for index, pair in enumerate(value['pairs'], 1):
        if not isinstance(pair, dict) or not required.issubset(pair) or any(not pair[key] for key in required):
            raise ValueError(f'invalid manifest pair at row {index}')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--category', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    category = load_category(args.category)
    output = args.output or ROOT / 'runs' / category['category_key'] / 'stage123/codex_reviews' / f'{args.task_id}.jsonl'
    result = import_reviews(category, load_manifest(args.manifest),
                            list(read_jsonl(args.reviews, allow_incomplete_tail=True)), output, args.task_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
