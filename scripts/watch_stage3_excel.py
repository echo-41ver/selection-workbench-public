"""Category-aware, non-paid third-layer Excel publication in a separate process."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.process_lock import ProcessLock
from selection_automation.publication_revision import database_signature
from selection_automation.merge_temu_snapshots import atomic_write

_EXPORT_VERSION = None


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def signature(paths):
    rows = []
    for path in paths:
        path = Path(path).resolve()
        try:
            stat = path.stat()
            rows.append(f'{path}:{stat.st_mtime_ns}:{stat.st_size}')
        except FileNotFoundError:
            rows.append(f'{path}:missing')
    return hashlib.sha256('|'.join(rows).encode()).hexdigest()


def ensure_publisher(category_path, root=ROOT):
    """Start without waiting; OS locks arbitrate simultaneous startup attempts."""
    import psutil
    base = root / 'runs' / read(category_path)['category_key']
    folder = base / 'stage123'
    folder.mkdir(parents=True, exist_ok=True)
    prior = read(folder / 'excel_publisher.json', {})
    try:
        process = psutil.Process(prior['pid'])
        if process.is_running() and abs(process.create_time() - prior['created']) < .01:
            return
    except (KeyError, psutil.Error):
        pass
    with (folder / 'excel_publisher.log').open('ab') as log:
        child = subprocess.Popen([sys.executable, str(root / 'scripts/watch_stage3_excel.py'),
                                  '--category', str(category_path)], cwd=root,
            env={k: v for k, v in os.environ.items() if k not in {'DEEPSEEK_API_KEY','SELECTION_STAGE3_API_KEY'}},
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return child


def external_export_running(category_path):
    """Legacy/manual exports may predate publish.lock: wait for that exact category."""
    import psutil
    target = Path(category_path).resolve()
    for process in psutil.process_iter(['pid', 'name']):
        if process.info['pid'] == os.getpid() or not str(process.info.get('name') or '').lower().startswith(('python','pypy')):
            continue
        try:args = process.cmdline()
        except psutil.Error:continue
        if not any(
                Path(arg).name.lower() == 'export_category_workbooks.py' for arg in args):
            continue
        try:
            category = Path(args[args.index('--category') + 1]).resolve()
            kind = args[args.index('--kind') + 1]
        except (ValueError, IndexError, OSError):
            continue
        if category == target and kind == 'layer123':
            return True
    return False


def publisher_phase(base,phase):
    """Expose the running publisher's bounded operation, retaining its identity."""
    path=base/'stage123/excel_publisher.json'
    state=read(path,{})
    if state.get('pid')==os.getpid():
        save(path,{**state,'phase':phase,'updated_at':time.time()})


def source_paths(category_path, base):
    from selection_automation.selection_delivery import delivery_source_paths
    folder = base / 'stage123'
    frozen = folder / 'export_frozen'
    return [category_path, folder / 'results.jsonl', folder / 'requests.jsonl',
            folder / 'request_intents.jsonl', folder / 'paid_enabled.json',
            base / 'stage12_config.json', base / 'commercial_report.json',
            *sorted(frozen.glob('*.json*')), *sorted((folder/'codex_reviews').glob('*.jsonl')),
            *delivery_source_paths(base), *export_code_paths()]


def export_code_paths():
    return [ROOT / 'scripts' / f'{name}.py' for name in (
        'export_category_workbooks', 'export_selection_result', 'export_incremental_layer123_excel', 'export_shower_layer123_excel')] + [
            ROOT/'src/selection_automation/selection_delivery.py',
            ROOT/'src/selection_automation/model_labels.py',
            ROOT/'src/selection_automation/operations.py',
            ROOT/'src/selection_automation/stage3_provider.py',
            ROOT/'src/selection_automation/vision.py',
            ROOT/'src/selection_automation/codex_visual_review.py',
            ROOT/'src/selection_automation/commercial.py',
            ROOT/'src/selection_automation/frontend_market.py',
            ROOT/'src/selection_automation/rules.py',
            ROOT/'src/selection_automation/identity.py',
            ROOT/'src/selection_automation/visual_decision.py']


def fresh_export():
    """Reload changed exporter code before each new build, never during an active build."""
    global _EXPORT_VERSION
    version = signature(export_code_paths())
    module = importlib.import_module('export_category_workbooks')
    if version != _EXPORT_VERSION:
        importlib.invalidate_caches()
        # The generic exporter imports these helpers lazily while writing its rows.
        for name in ('selection_automation.visual_decision', 'selection_automation.identity',
                     'selection_automation.rules', 'selection_automation.vision',
                     'export_incremental_layer123_excel', 'export_shower_layer123_excel',
                     'selection_automation.codex_visual_review', 'selection_automation.operations',
                     'selection_automation.frontend_market', 'selection_automation.commercial',
                     'selection_automation.stage3_provider', 'selection_automation.selection_delivery',
                     'export_selection_result'):
            if name in sys.modules:
                importlib.reload(sys.modules[name])
        module = importlib.reload(module)
        _EXPORT_VERSION = version
    return module.export


def database_paths(base):
    cfg = read(base / 'stage12_config.json', {})
    if not cfg.get('output_dir'):
        return []
    output = Path(cfg['output_dir'])
    if not output.is_absolute():
        output = base / output
    db = output / 'stage12.sqlite3'
    return [db, db.with_name(db.name + '-wal')]


def publish(category_path):
    """Only export layer123; never regenerate Amazon/TEMU/layer12 workbooks."""
    from selection_workflow.excel_delivery import retry_fixed_view
    key = read(category_path)['category_key']
    base = ROOT / 'runs' / key
    publisher_phase(base,'检查当前任务的导出进程')
    if external_export_running(category_path):
        return False
    publisher_phase(base,'准备进入发布锁')
    with ProcessLock(base / 'publish.lock'):
        publisher_phase(base,'发布锁内核对外部导出')
        if external_export_running(category_path):
            return False
        prior = read(base / 'publish_state.json', {})
        current = dict(prior)
        pending = dict(prior.get('pending_views', {}))
        deliveries = dict(prior.get('deliveries', {}))
        errors = dict(prior.get('errors', {}))
        item = pending.get('layer123')
        if item:
            retried = retry_fixed_view(Path(item['数据源工作簿']), Path(item['固定查看副本']))
            deliveries['layer123'] = {**deliveries.get('layer123', {}), **retried}
            if retried.get('固定查看副本已更新'):
                deliveries['layer123'].pop('固定查看副本错误', None)
                pending.pop('layer123', None)
        db_paths = database_paths(base)
        db_files_before = signature(db_paths)
        db_signature = database_signature(db_paths)
        frozen = base / 'stage123/export_frozen'
        summary = frozen / f'layer12_summary_{key}.json'
        files_ready = all((frozen / name).exists() for name in (
            f'layer12_summary_{key}.json', f'layer12_rows_{key}.jsonl', f'layer3_candidates_{key}.jsonl'))
        # Reuse a current freeze at first start; DB changes alone refresh it.
        db_changed = prior.get('stage3_excel_database', db_signature) != db_signature
        if files_ready and 'stage3_excel_database' not in prior:
            db_changed = any(path.exists() and path.stat().st_mtime_ns > summary.stat().st_mtime_ns
                             for path in db_paths)
        if db_paths and db_paths[0].exists() and (db_changed or not files_ready):
            publisher_phase(base,'冻结最新第一二层数据')
            subprocess.run([sys.executable, str(ROOT / 'scripts/run_incremental_stage12.py'),
                'snapshot', '--config', str(base / 'stage12_config.json'),
                '--snapshot-dir', str(frozen), '--path-key', key], cwd=ROOT,
                check=True, stdout=subprocess.DEVNULL,
                env={k: v for k, v in os.environ.items() if k not in {'DEEPSEEK_API_KEY','SELECTION_STAGE3_API_KEY'}}, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if not summary.exists():
            return False
        before = signature(source_paths(category_path, base))
        if before != prior.get('stage3_excel'):
            publisher_phase(base,'生成并交付综合Excel')
            delivered = fresh_export()(category_path, 'layer123')
            if not (delivered.get('固定查看副本已更新') or delivered.get('时间戳查看快照已生成')):
                raise RuntimeError('三层Excel数据已写入，但查看副本和快照均未交付')
            deliveries['layer123'] = delivered
            if delivered.get('固定查看副本已更新'):
                pending.pop('layer123', None)
            else:
                pending['layer123'] = {name: delivered[name] for name in ('数据源工作簿', '固定查看副本')}
            # Capture BEFORE export so results arriving during its two-minute build remain dirty.
            current['stage3_excel'] = before
            current['stage3_excel_database'] = db_signature
            current['stage3_excel_database_files'] = db_files_before
            current['stage3_excel_updated_at'] = time.time()
        errors.pop('stage3_excel', None)
        current.update(deliveries=deliveries, pending_views=pending, errors=errors)
        save(base / 'publish_state.json', current)
        return True


def controller_active(base):
    from selection_automation.stage3_progress import controller_alive
    state = read(base / 'stage123/controller.json', {})
    return controller_alive(base) and not state.get('error') and not str(state.get('phase', '')).startswith(
        ('分析暂停', '已停止', '第三层运行失败', '可运行候选已处理；'))


def collection_owner_active(base):
    """Keep non-paid publication alive for this independently verified task."""
    import psutil
    state = read(ROOT/'runtime/active.json', {})
    if state.get('category_key') != base.name:
        return False
    try:
        process = psutil.Process(state['pid'])
        args = process.cmdline()
        return (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
            and abs(process.create_time()-state['created']) < .01
            and Path(process.cwd()).resolve() == ROOT.resolve()
            and any(Path(arg).resolve() == (ROOT/'scripts/selection.py').resolve() for arg in args)
            and 'run' in args and '--category' in args
            and Path(args[args.index('--category')+1]).resolve() == (base/'category.json').resolve())
    except (OSError, KeyError, ValueError, IndexError, TypeError, psutil.Error):
        return False


def watch(category_path, interval=30):
    import psutil
    base = ROOT / 'runs' / read(category_path)['category_key']
    folder = base / 'stage123'
    with ProcessLock(folder / 'excel_publisher.lock'):
        process = psutil.Process()
        try:
            process.nice(psutil.NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
        except (OSError, psutil.Error):
            pass
        while True:
            save(folder / 'excel_publisher.json', {'pid': os.getpid(), 'created': process.create_time(),
                 'phase': '等待增量发布', 'updated_at': time.time()})
            try:
                complete = publish(category_path)
                # Recheck after export, since paid results may have arrived during the build.
                state = read(base / 'publish_state.json', {})
                clean = signature(source_paths(category_path, base)) == state.get('stage3_excel')
                if not controller_active(base) and not collection_owner_active(base) and complete and clean and not state.get('pending_views', {}).get('layer123'):
                    return
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                save(folder / 'excel_publisher.json', {'pid': os.getpid(), 'created': process.create_time(),
                     'phase': '发布暂缓，将自动重试', 'error': str(exc), 'updated_at': time.time()})
            time.sleep(interval)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--category', type=Path, required=True)
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args()
    try:
        watch(args.category, max(5, args.interval))
    except RuntimeError as exc:
        if str(exc) != 'Another supervisor owns this installation':
            raise
