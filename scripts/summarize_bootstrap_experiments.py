"""Summarize isolated browser runs, keeping page visits separate from human actions."""
import argparse
import json
from pathlib import Path


def verification_visits(history):
    visits, elapsed, entered = 0, 0.0, None
    for sample in history:
        if sample.get('unavailable'):
            continue  # Missing observations do not end a verification episode.
        if sample.get('challenge'):
            if entered is None:
                visits += 1
                entered = sample['at']
        elif entered is not None:
            elapsed += max(0.0, sample['at'] - entered)
            entered = None
    return {'verification_page_visits': visits,
            'completed_verification_wait_seconds': round(elapsed, 1),
            'verification_pending': entered is not None}


def lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text('utf-8-sig').splitlines() if line.strip()]


def summarize(directory):
    human = {entry['case']: entry for entry in lines(directory / 'user_actions.jsonl')}
    result = []
    for path in sorted(directory.glob('*/case.json')):
        case = json.loads(path.read_text('utf-8-sig'))
        progress = case.get('result') or {}
        entry = {'case': case['case'], 'browser': case.get('serial_number'),
                 'factor': case.get('factor') or case.get('variant'),
                 'status': case['status'], 'result': progress.get('status'),
                 'unique_goods': progress.get('unique_goods'),
                 'confirmed_rounds': progress.get('round'),
                 'active_seconds': case.get('active_seconds'),
                 'reported_human_captcha_count': human.get(case['case'], {}).get('reported_captcha_count')}
        entry.update(verification_visits(lines(path.parent / 'page_history.jsonl')))
        result.append(entry)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    text = json.dumps(summarize(args.directory), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + '\n', 'utf-8')
    else:
        print(text)
