"""Prepare revision-bound, read-only Codex image-review batches from Stage 12."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from selection_automation.category_config import load_category
from selection_automation.codex_visual_review import SOURCE, checked_facts, normalize_review
from selection_automation.stage3_admission import admitted, threshold
from selection_automation.vision import VisionRevisionContext, compatible_vision_keys


def _jsonl(path: Path):
    if not path.is_file():
        return
    with path.open('rb') as stream:
        for number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                yield number, value


def _write_jsonl(path: Path, rows):
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


def _stage12_candidates(category, stage12_config_path: Path):
    config = json.loads(stage12_config_path.read_text(encoding='utf-8-sig'))
    output_dir = Path(config['output_dir'])
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    database = output_dir / 'stage12.sqlite3'
    if not database.is_file():
        raise FileNotFoundError(f'Stage 12 database not found: {database}')
    uri = database.resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN')
        path_key = category['category_key']
        yield from (json.loads(row['payload_json']) for row in connection.execute(
            'SELECT payload_json FROM candidates WHERE path_key=? ORDER BY asin, goods_id', (path_key,)))
    finally:
        connection.rollback()
        connection.close()


def _existing_current_results(category, base: Path, current_keys, payload_by_pair):
    def compatible(pair, revision):
        expected = current_keys.get(pair, ())
        return revision in ({expected} if isinstance(expected, str) else expected)

    completed = set()
    result_file = base / 'stage123' / 'results.jsonl'
    for _, row in _jsonl(result_file) or ():
        pair = (row.get('asin'), row.get('goods_id'))
        if row.get('api_status') == 'success' and compatible(pair, row.get('vision_revision_key')):
            try:
                checked_facts(category, row['visual_facts'], row['goods_id'])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            completed.add(pair)
    review_dir = base / 'stage123' / 'codex_reviews'
    for path in sorted(review_dir.glob('*.jsonl')) if review_dir.is_dir() else ():
        for _, row in _jsonl(path) or ():
            pair = (row.get('asin'), row.get('goods_id'))
            if (row.get('review_source') == SOURCE and row.get('review_status') in {'SAME','SIMILAR', 'DIFFERENT', 'UNKNOWN'}
                    and compatible(pair, row.get('vision_revision_key'))):
                try:
                    bound = {**payload_by_pair[pair], 'vision_revision_key': row['vision_revision_key']}
                    normalize_review(category, row, bound, row.get('task_id') or 'existing')
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                completed.add(pair)
    return completed


def _unfinished_allocations(base: Path):
    reserved = set()
    for batch in sorted((base / 'nightly_review').glob('*/review-batch.json')):
        try:
            data = json.loads(batch.read_text(encoding='utf-8-sig'))
            for task in data.get('tasks', []):
                manifest = Path(task['manifest'])
                if not manifest.is_absolute():
                    manifest = batch.parent / manifest
                rows = [row for _, row in (_jsonl(manifest) or ())]
                expected = {(row['asin'], row['goods_id'], row['vision_revision_key']) for row in rows}
                if not expected:
                    continue
                output = Path(task['output'])
                if not output.is_absolute():
                    output = base / output
                received = set()
                for _, row in _jsonl(output) or ():
                    key = (row.get('asin'), row.get('goods_id'), row.get('vision_revision_key'))
                    if key in expected and row.get('review_status') in {'SAME','SIMILAR', 'DIFFERENT', 'UNKNOWN'}:
                        received.add(key)
                if received != expected:
                    reserved.update(expected - received)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Do not start another batch if prior allocation state cannot be proved.
            raise ValueError(f'cannot safely check prior batch allocation {batch}: {exc}') from exc
    return reserved


def _image_status(pair):
    missing = []
    for side in ('amazon', 'temu'):
        raw = pair.get(f'{side}_image_path')
        path = Path(str(raw)) if raw else None
        if path is None or not path.is_file():
            missing.append({'asin': pair.get('asin'), 'goods_id': pair.get('goods_id'), 'side': side,
                            'local_path': str(raw or ''), 'image_url': pair.get(f'{side}_image_url'),
                            'action': '恢复该侧本地缓存图片后重新准备批次；当前不判不同款。'})
    return missing


def _facts_guide(category):
    stage = category['stage3']
    modules = stage['modules']
    policy = stage.get('identity_policy') or {}
    visual_features = [feature['id'] for feature in policy.get('features', [])
                       if feature.get('role') == 'identity' and feature.get('evidence') in {'image', 'both'}]
    facts = {
        'overall_confidence': '<按原图实际可见程度填写0至1数字，禁止照抄占位>',
        'modules': {key: {'relation': 'UNKNOWN', 'amazon_evidence': '', 'temu_evidence': '',
                          'hard_conflict_code': 'NONE'} for key in modules},
        'identity': {
            'verdict': 'UNKNOWN',
            'design_comparison': {'relation': 'UNKNOWN', 'amazon': '', 'temu': ''},
            'matches': [], 'differences': [], 'uncertainties': ['请写明看不清或未展示的具体部分'],
            'reason': '用两侧具体事实说明判断；证据不足时保留 UNKNOWN。'},
    }
    if stage.get('require_feature_comparisons'):
        facts['feature_comparisons'] = {fid: {'relation': 'UNKNOWN', 'amazon': '', 'temu': ''}
                                        for fid in visual_features}
        required = {key: facts[key] for key in ('overall_confidence', 'identity', 'modules', 'feature_comparisons')}
    else:
        required = facts
    return {
        'purpose': '根据原图判断同款、类似款、不同款或无法判断；分析失败自动补跑，不作为商品结论。',
        'recommended_model': 'gpt-6.1-sol',
        'reasoning_effort': 'medium',
        'category_key': category['category_key'],
        'product_scope': policy.get('product_scope'),
        'visual_instructions': category.get('plain_stage3') or stage.get('visual_instructions'),
        'modules': list(modules),
        'relation_values': ['SAME', 'VARIANT', 'SIMILAR', 'DIFFERENT', 'UNKNOWN'],
        'hard_conflict_codes': ['NONE', *stage.get('identity_hard_conflicts', [])],
        'feature_comparison_ids': visual_features,
        'identity_template_required_fields': ['verdict', 'design_comparison', 'matches', 'differences', 'uncertainties', 'reason'],
        'facts_output_template': required,
        'rules': [
            '确认整台商品是否为同一基础款；不要只因同属一类、用途相同或零件相似判为同款。',
            '不同零件或配件本身不自动代表整机不同；必须指出对应的主体结构或身份硬冲突，并保留两侧证据。',
            '局部外观/颜色/标识只按品类规则处理；不能用允许变体掩盖主体、接口、连接方式或核心部件布局差异。',
            '未知模块填 relation=UNKNOWN 且双方证据为空字符串；不要用 0 分或猜测事实代替 UNKNOWN。',
            '必须填齐所有 modules 和 feature_comparisons。hard_conflict_code 无明确冲突填 NONE。',
            '不要添加 score、total_score 或任何主观分数。',
            'overall_confidence 不是主观打分，而是证据清晰程度；双边主体结构清楚时不能填0。必须用真实数字替换占位，不能照抄。',
        ],
    }


def _select_coverage_first(rows, limit, group_size=12):
    # Cover Amazon references by whole groups, rather than scattering one pair
    # from every reference and destroying the shared-image benefit.
    from selection_automation.visual_batch import groups
    ordered=sorted(rows,key=lambda row:(-float(row['vector_score']),row['asin'],row['goods_id']))
    selected=[]
    for group in groups(ordered,group_size):
        selected.extend(group[:limit-len(selected)])
        if len(selected)>=limit:break
    return selected


def _contact_sheets(folder: Path, rows, per_contact=12):
    contact_dir = folder / 'contacts'
    contact_dir.mkdir(parents=True, exist_ok=True)
    width, image_height, label_height = 320, 160, 34
    font = ImageFont.load_default()
    written = []
    for offset in range(0, len(rows), per_contact):
        group = rows[offset:offset + per_contact]
        columns = 2 if len(group) > 8 else 1
        height = ((len(group) + columns - 1) // columns) * (image_height + label_height)
        canvas = Image.new('RGB', (width * 2 * columns, height), '#f4f4f4')
        draw = ImageDraw.Draw(canvas)
        for index, row in enumerate(group):
            y = (index // columns) * (image_height + label_height)
            left_pair = (index % columns) * width * 2
            draw.text((left_pair + 8, y + 4), f"{offset + index + 1}. A | {row['asin']} | goods {row['goods_id']} | score {row['vector_score']}", fill='#111111', font=font)
            draw.text((left_pair + width + 8, y + 4), f"{offset + index + 1}. T | {row['asin']} | goods {row['goods_id']}", fill='#111111', font=font)
            for side, x in (('amazon', left_pair), ('temu', left_pair + width)):
                target = (x, y + label_height, x + width, y + label_height + image_height)
                try:
                    with Image.open(row[f'{side}_image_path']) as source:
                        image = source.convert('RGB')
                    image.thumbnail((width - 12, image_height - 12))
                    left = x + (width - image.width) // 2
                    top = y + label_height + (image_height - image.height) // 2
                    canvas.paste(image, (left, top))
                except (OSError, ValueError):
                    draw.rectangle(target, fill='#e2e2e2')
                    draw.text((x + 12, y + label_height + 12), 'IMAGE UNREADABLE / RESTORE CACHE', fill='#990000', font=font)
        path = contact_dir / f'contact-{offset // per_contact + 1:03d}.jpg'
        canvas.save(path, quality=88)
        written.append(path)
    return written


def prepare_batch(category_path: Path, *, shards=3, per_shard=96, output_dir: Path | None = None, contact_group_size=12, skip_empty=False):
    category_path = Path(category_path).resolve()
    category = load_category(category_path)
    base = category_path.parent
    stage12_config_path = base / 'stage12_config.json'
    context = VisionRevisionContext(category)
    eligible = []
    missing_images = []
    unavailable = []
    current_keys = {}
    payload_by_pair = {}
    for payload in _stage12_candidates(category, stage12_config_path):
        if not admitted(category, payload):
            continue
        pair = (payload.get('asin'), payload.get('goods_id'))
        if not all(isinstance(value, str) and value for value in pair):
            unavailable.append({'payload': payload, 'reason': 'missing ASIN or goods_id'})
            continue
        missing = _image_status(payload)
        if missing:
            missing_images.extend(missing)
            continue
        try:
            keys = compatible_vision_keys(category, payload, context=context)
            vision_key = keys[0]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unavailable.append({'asin': pair[0], 'goods_id': pair[1], 'reason': f'cannot calculate current vision revision: {exc}'})
            continue
        current_keys[pair] = set(keys)
        payload_by_pair[pair] = {**payload, 'vision_revision_key': vision_key}
        if 'facts' not in payload or payload.get('facts') is None:
            unavailable.append({'asin': pair[0], 'goods_id': pair[1], 'reason': 'candidate facts are missing'})
            continue
        eligible.append({**payload, 'vector_score': payload.get('vector_score'), 'vision_revision_key': vision_key})

    completed = _existing_current_results(category, base, current_keys, payload_by_pair)
    reserved = _unfinished_allocations(base)
    eligible = [row for row in eligible if (row['asin'], row['goods_id']) not in completed and
                not any((row['asin'],row['goods_id'],version) in reserved for version in current_keys[(row['asin'],row['goods_id'])])]
    group_size=min(contact_group_size,per_shard)
    selected = _select_coverage_first(eligible, shards * per_shard, group_size)
    if skip_empty and not selected:
        return {'selected':0, 'shard_targets':[], 'missing_image_sides':len(missing_images),
                'unavailable_candidates':len(unavailable), 'covered_amazon_asins':0,
                'shortfall':shards * per_shard, 'eligible_after_dedup':len(eligible)}
    distribution = [[] for _ in range(shards)]
    from selection_automation.visual_batch import groups
    for group in groups(selected,group_size):
        min(distribution,key=len).extend(group)

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = Path(output_dir).resolve() if output_dir else base / 'nightly_review' / timestamp
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(f'refusing to overwrite non-empty batch directory: {folder}')
    folder.mkdir(parents=True, exist_ok=True)
    guide = _facts_guide(category)
    tasks = []
    for index, rows in enumerate(distribution, 1):
        shard = folder / f'shard-{index}'
        shard.mkdir(parents=True, exist_ok=True)
        manifest = shard / 'manifest.jsonl'
        raw_reviews = shard / 'raw_reviews.jsonl'
        _write_jsonl(manifest, rows)
        raw_reviews.touch(exist_ok=True)
        (shard / 'visual-facts-guide.json').write_text(json.dumps(guide, ensure_ascii=False, indent=2), encoding='utf-8')
        contacts = _contact_sheets(shard, rows, per_contact=contact_group_size)
        output = base / 'stage123' / 'codex_reviews' / f'{folder.name}-shard-{index}.jsonl'
        tasks.append({'task_id': f'{folder.name}-shard-{index}', 'manifest': str(manifest.resolve()),
                      'raw_reviews': str(raw_reviews.resolve()), 'output': str(output.resolve()),
                      'target': len(rows), 'contact_sheets': [str(path.resolve()) for path in contacts]})
    (folder / 'review-batch.json').write_text(json.dumps({
        'schema_version': 'codex-visual-review-batch-v1', 'batch_id': folder.name,
        'category_key': category['category_key'], 'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'target_per_shard': per_shard, 'contact_group_size': contact_group_size, 'tasks': tasks,
        'raw_reviews_instruction': '看图 agent 将原始完整结论写入各 task.raw_reviews；导入后结果写入 task.output，供 watch_codex_visual_reviews.batch_tasks 观察。',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    _write_jsonl(folder / 'missing_images.jsonl', missing_images)
    _write_jsonl(folder / 'unavailable_candidates.jsonl', unavailable)
    counts = Counter(row['asin'] for row in selected)
    (folder / 'summary.json').write_text(json.dumps({
        'category_key': category['category_key'], 'admission_threshold': threshold(category),
        'stage2_candidate_threshold': category['stage2']['candidate_threshold'],
        'shards': shards, 'target_per_shard': per_shard, 'selected': len(selected),
        'shortfall': max(0, shards * per_shard - len(selected)),
        'eligible_after_dedup': len(eligible), 'excluded_current_results': len(completed),
        'reserved_in_unfinished_batches': len(reserved), 'missing_image_sides': len(missing_images),
        'unavailable_candidates': len(unavailable), 'covered_amazon_asins': len(counts),
        'amazon_asin_counts': dict(counts), 'shard_targets': [len(rows) for rows in distribution],
        'vision_revision_policy': 'current compatible_vision_keys()[0]; no key is invented when local image bytes are unavailable',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'batch_dir': str(folder.resolve()), 'selected': len(selected), 'shard_targets': [len(rows) for rows in distribution],
            'missing_image_sides': len(missing_images), 'unavailable_candidates': len(unavailable),
            'covered_amazon_asins': len(counts), 'shortfall': max(0, shards * per_shard - len(selected))}


def main():
    parser = argparse.ArgumentParser(description='Prepare local Codex image-review shards from the read-only Stage 12 candidate database.')
    parser.add_argument('--category', type=Path, required=True)
    parser.add_argument('--shards', type=int, default=3)
    parser.add_argument('--per-shard', type=int, default=96)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--contact-group-size', type=int, default=12)
    args = parser.parse_args()
    if args.shards < 1 or args.per_shard < 1 or args.contact_group_size < 1:
        parser.error('--shards, --per-shard and --contact-group-size must be positive integers')
    print(json.dumps(prepare_batch(args.category, shards=args.shards, per_shard=args.per_shard,
                                   output_dir=args.output_dir, contact_group_size=args.contact_group_size), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
