"""Reproduce the delivery experiment; no provider calls and no inferred labels."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def decision(value):
    if value in ('SAME', '确认同款', '同款（存在规格变体）'):
        return 'SAME'
    if value in ('DIFFERENT', '不同款'):
        return 'DIFFERENT'
    return 'UNRESOLVED'


def metrics(records, reference):
    assert len({r['sample'] for r in records}) == len(records), 'Duplicate sample'
    selected = [r for r in records if r['sample'] in reference]
    result = dict(n=len(selected), correct=0, false_same=0, false_different=0,
                  unresolved=0, failed=0, true_same=0, confirmed_true_same=0)
    outcomes = []
    for row in selected:
        truth = reference[row['sample']]
        actual = decision(row.get('actual')) if row.get('api_status') == 'success' else 'FAILED'
        result['true_same'] += truth == 'SAME'
        result['confirmed_true_same'] += truth == actual == 'SAME'
        result['correct'] += truth == actual
        result['false_same'] += truth == 'DIFFERENT' and actual == 'SAME'
        result['false_different'] += truth == 'SAME' and actual == 'DIFFERENT'
        result['unresolved'] += actual == 'UNRESOLVED'
        result['failed'] += actual == 'FAILED'
        outcomes.append({'sample': row['sample'], 'reference': truth, 'actual': actual})
    result['outcomes'] = sorted(outcomes, key=lambda r: r['sample'])
    result['request_median_seconds'] = round(statistics.median(r['elapsed_seconds'] for r in records), 3)
    return result


def summarize(folder):
    old = folder.parent / 'benchmark_20260930'
    refs = {r['sample']: r for r in read(old / 'labels.json')}
    correction = read(old / 'reference_revision.json')
    for row in correction['corrections'] + correction['user_additions']:
        refs[row['sample']] = row
    anchor_ids = {r['sample'] for r in read(folder / 'batch_inputs.json')}
    reference = {sid: refs[sid]['reference'] for sid in anchor_ids}
    assert len(reference) == 10
    assert all(refs[sid]['reference_confidence'] == '高' for sid in anchor_ids)
    result = {'scope': 'Known regression anchors, not an untouched holdout.',
              'anchor_reference': reference, 'methods': {}}
    batch = rows(folder / 'batch_results.jsonl')
    for size in (1, 4, 12):
        result['methods'][f'batch_{size}'] = metrics([r for r in batch if r['size'] == size], reference)
    files = {'independent_flash': 'independent_results_recovered.jsonl',
             'independent_pro_text': 'pro_text_results.jsonl',
             'minimal_flash': 'minimal_results.jsonl',
             'image_adjudication': 'adjudication_recovered_results.jsonl'}
    for method, filename in files.items():
        path = folder / filename
        if path.exists() and path.stat().st_size:
            result['methods'][method] = metrics(rows(path), reference)
    holdout = read(folder / 'holdout.json')
    holdout_refs = {r['sample']: r['reference'] for r in holdout
                   if r['reference_confidence'] == '高' and r['reference'] in ('SAME', 'DIFFERENT')}
    result['fresh_holdout'] = metrics(rows(folder / 'holdout_flash.jsonl'), holdout_refs)
    result['fresh_holdout']['not_scored'] = len(holdout) - len(holdout_refs)
    result['fresh_holdout']['limitation'] = '19 negative references and no positives; cannot measure recall.'
    # Count each billed stage once. Observation-comparison results reuse ledger
    # records and must not be added again; Pro text reuses the same observations.
    billed = batch + rows(folder / 'holdout_flash.jsonl')
    billed += [r for r in rows(folder / 'independent_calls.jsonl') if r.get('event') == 'request_finished']
    billed += rows(folder / 'pro_medium.jsonl') + rows(folder / 'pro_text_results.jsonl')
    billed += rows(folder / 'minimal_results.jsonl')
    if (folder / 'adjudication_results.jsonl').exists():
        billed += rows(folder / 'adjudication_results.jsonl')
    if (folder / 'adjudication_recovery_results.jsonl').exists():
        billed += rows(folder / 'adjudication_recovery_results.jsonl')
    result['recorded_requests'] = len(billed)
    result['unrecorded_in_flight_requests_upper_bound'] = read(folder / 'pro_summary.json')['in_flight_requests_aborted_upper_bound']
    result['known_usage'] = {key: sum(r.get('usage', {}).get(key, 0) or 0 for r in billed)
                             for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    result['billing_scope'] = 'Completed experiments only; paused holdout_verified trial is recorded separately in its checkpoint.'
    result['input_file_sha256'] = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                  for name in ('batch_inputs.json', 'holdout.json', 'independent_protocol.json',
                                               'pro_text_inputs.json', 'minimal_protocol.json')}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT / 'runtime/benchmark_20261002')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = summarize(args.folder)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + '\n', encoding='utf-8')
    else:
        print(encoded)
