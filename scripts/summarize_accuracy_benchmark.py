"""Reproduce reference-based counts; never use a tested model as ground truth."""
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.rules import score
from selection_automation.vision import decode


def summarize(folder):
    def read(name):
        return json.loads((folder / name).read_text('utf-8'))
    def lines(name):
        return [json.loads(line) for line in (folder / name).read_text('utf-8').splitlines() if line.strip()]
    source = {s['sample']: s for name in ('labels.json', 'supplementary_towels.json') for s in read(name)}
    refs = {s['sample']: s for s in read('labels.json')}
    revision = read('reference_revision.json')
    for row in revision['corrections'] + revision['user_additions']:
        refs[row['sample']] = {**source[row['sample']], **row}
    primary = {sid for sid, s in refs.items() if s['reference_confidence'] == '高' and s['reference'] in {'SAME', 'DIFFERENT'}}
    known = {sid for sid, s in refs.items() if s['reference'] in {'SAME', 'DIFFERENT'}}
    common = {6, 18, 26, 39, 59, 60, 80, 81, 112, 117}
    blind = {s['sample'] for s in read('blind_models.json')}
    configs = {k: read(k + '_v5.json') for k in ('drill', 'remote', 'projector', 'curtain', 'towel')}
    def state(row):
        return 'SAME' if row.get('actual') in {'确认同款', '同款（存在规格变体）'} else 'DIFFERENT' if row.get('actual') == '不同款' else 'UNRESOLVED'
    def metrics(rows, ids):
        selected = [r for r in rows if r['sample'] in ids]
        counts = {'n': len(selected), 'true_same': 0, 'true_different': 0, 'correct': 0, 'false_same': 0,
                  'false_different': 0, 'unresolved': 0, 'failed': 0, 'confirmed_true_same': 0}
        errors = []
        for r in selected:
            truth = refs[r['sample']]['reference']; actual = state(r)
            counts['true_same' if truth == 'SAME' else 'true_different'] += 1
            if r.get('api_status') == 'failed': counts['failed'] += 1
            if actual == truth:
                counts['correct'] += 1
                if truth == 'SAME': counts['confirmed_true_same'] += 1
            elif actual == 'UNRESOLVED': counts['unresolved'] += 1
            else:
                counts['false_same' if actual == 'SAME' else 'false_different'] += 1
                errors.append({'sample': r['sample'], 'reference': truth, 'actual': actual})
        for name, numerator, denominator in (
            ('decision_accuracy', counts['correct'], counts['n']),
            ('same_recall', counts['confirmed_true_same'], counts['true_same']),
            ('same_precision', counts['confirmed_true_same'], counts['confirmed_true_same'] + counts['false_same'])):
            counts[name] = round(numerator / denominator * 100, 2) if denominator else None
        counts['errors'] = errors
        return counts
    result = {'reference': {'primary_main': len(primary & set(range(1, 101))), 'primary_blind30': len(primary & blind),
                           'primary_common10': len(primary & common), 'revision': revision}, 'models': {}}
    all_api = []
    for name in ('baseline', 'current', 'deepseek_v5_low', 'deepseek_v5_medium', 'deepseek_v5_high', 'final_v6_medium', 'detail_low', 'countercheck_low'):
        if not (folder / (name + '.jsonl')).exists(): continue
        raw = lines(name + '.jsonl'); all_api.extend(raw)
        rows = []
        for r in raw:
            r = dict(r)
            if name not in ('baseline', 'current') and r.get('facts'):
                cat = configs[source[r['sample']]['category_key']]
                if name in ('final_v6_medium', 'detail_low') and source[r['sample']]['category_key'] == 'towel':
                    cat = json.loads((ROOT / 'config/categories/bath_towel_set.json').read_text('utf-8'))
                r['actual'] = score(cat, r['facts'])['state']
            rows.append(r)
        times = [r['elapsed_seconds'] for r in raw if isinstance(r.get('elapsed_seconds'), (int, float))]
        result['models'][name] = {'rows': len(rows), 'primary': metrics(rows, primary), 'all_known': metrics(rows, known),
            'blind30_primary': metrics(rows, primary & blind), 'common10_primary': metrics(rows, primary & common),
            'strict_output_success': sum(r.get('api_status') == 'success' for r in raw),
            'api_median_seconds': round(statistics.median(times), 2) if times else None,
            'states': {str(r['sample']): r.get('actual', 'FORMAT_OR_API_FAILURE') for r in rows}}
    durations = {'low': 709.218, 'medium': 931.711, 'high': 951.681}
    for effort in durations:
        raw = lines('luna_' + effort + '.jsonl'); rows = []; strict = 0; semantic = 0
        for r in raw:
            payload = r.get('raw', r)
            sid = r['sample'] if 'sample' in r else int(payload['candidates'][0]['i'])
            cat = configs[source[sid]['category_key']]; row = {'sample': sid}
            try: decode(cat, payload, ['C00']); strict += 1
            except (ValueError, KeyError): pass
            try:
                # Diagnostic only: preserve the agent's actual candidate ID.
                # The production decoder is NOT changed to accept it.
                actual_id = payload['candidates'][0]['i']
                f = decode(cat, payload, [actual_id])[actual_id]
                row.update(actual=score(cat, f)['state'], api_status='success'); semantic += 1
            except (ValueError, KeyError): row['api_status'] = 'failed'
            rows.append(row)
        result['models']['luna_' + effort] = {'rows': len(rows), 'primary': metrics(rows, primary & blind),
            'all_known': metrics(rows, known & blind), 'common10_primary': metrics(rows, primary & common),
            'blind30_primary': metrics(rows, primary & blind), 'strict_output_success': strict, 'semantic_decodable': semantic,
            'codex_whole_task_seconds': durations[effort], 'states': {str(r['sample']): r.get('actual', 'FORMAT_FAILURE') for r in rows}}
    vectors = read('vectors.json'); before = read('local_baseline.json'); after = read('local_current.json')
    true_ids = {sid for sid in primary if refs[sid]['reference'] == 'SAME'}
    vtrue = [r for r in vectors if r['sample'] in true_ids]
    result['local'] = {'observations': len(vectors), 'unique_pairs': len({(r['category_key'], r['asin'], r['goods_id']) for r in vectors}),
        'stage1_old_excluded': sum(r['excluded'] for r in before), 'stage1_new_excluded': sum(r['excluded'] for r in after),
        'primary_positive_n': len(vtrue), 'stage1_old_false_exclusions': sum(r['excluded'] for r in before if r['sample'] in true_ids),
        'stage1_new_false_exclusions': sum(r['excluded'] for r in after if r['sample'] in true_ids),
        'stage2_old_candidates': sum(r['old_candidate'] for r in vectors), 'stage2_new_candidates': sum(r['new_candidate'] for r in vectors),
        'stage2_old_candidates_lost': sum(r['old_candidate'] and not r['new_candidate'] for r in vectors),
        'stage2_old_true_recalled': sum(r['old_candidate'] for r in vtrue), 'stage2_new_true_recalled': sum(r['new_candidate'] for r in vtrue),
        'stage2_positive_scores': [{k:r[k] for k in ('sample','old_score','new_score','threshold','old_candidate','new_candidate')} for r in vtrue]}
    result['paid'] = {'requests': sum(r.get('api_requests', 0) for r in all_api), 'unique_pairs': len({r['sample'] for r in all_api}),
        'input_tokens': sum(r.get('usage', {}).get('input_tokens', 0) or 0 for r in all_api),
        'output_tokens': sum(r.get('usage', {}).get('output_tokens', 0) or 0 for r in all_api)}
    (folder / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    return result


if __name__ == '__main__':
    result = summarize(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'runtime/benchmark_20260930')
    print(json.dumps({k:v for k,v in result.items() if k != 'models'}, ensure_ascii=False, indent=2))
    for name, m in result['models'].items():
        print(name, json.dumps({k:v for k,v in m.items() if k not in {'states', 'all_known'}}, ensure_ascii=False))
