from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from web_console import worker_supervisor_paths


def test_live_workers_exclude_retained_error_snapshots(tmp_path):
    folder = tmp_path / 'temu' / 'supervisors'
    folder.mkdir(parents=True)
    names = ['worker-01-new', 'worker-02-new', 'worker-03-new', 'worker-04-new',
             'worker-01-new-before-sort-resume-123', 'worker-04-new-backup',
             'worker-01-new-before-timeout-resume-456']
    for name in names:
        (folder / f'{name}.json').write_text('{}')
    assert [path.stem for path in worker_supervisor_paths(tmp_path)] == names[:4]
    assert len(list(folder.glob('*.json'))) == 7
