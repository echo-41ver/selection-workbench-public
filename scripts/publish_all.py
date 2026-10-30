"""A separate low-priority publisher; never blocks collection or analysis."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from selection import ROOT, read, save
from selection_automation.process_lock import ProcessLock
from selection_automation.publication_revision import database_signature


@contextmanager
def publish_lock(path, timeout=300):
    """An independent publisher queues behind an export instead of failing it."""
    guard = ProcessLock(path)
    deadline = time.monotonic() + timeout
    while True:
        try:
            guard.__enter__()
            break
        except RuntimeError as error:
            if str(error) != 'Another supervisor owns this installation':
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.5, remaining))
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


def signature(paths):
    paths = [Path(path).resolve() for path in paths]
    return hashlib.sha256('|'.join(
        f'{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}' if path.exists()
        else f'{path}:missing' for path in paths
    ).encode()).hexdigest()


def publish(category_path, *, kind=None):
    requested_kind = kind
    from export_category_workbooks import export
    from selection_workflow.excel_delivery import retry_fixed_view
    cat = read(category_path)
    from selection_automation.collection_contract import collection_only
    only_collection = collection_only(cat)
    key = cat['category_key']
    base = ROOT / 'runs' / key
    with publish_lock(base / 'publish.lock'):
        prior = read(base / 'publish_state.json', {})
        current = dict(prior)
        pending = dict(prior.get('pending_views', {}))
        deliveries = dict(prior.get('deliveries', {}))
        pending.pop('layer12',None)
        deliveries.pop('layer12',None)
        errors = dict(prior.get('errors', {})) if requested_kind else {}
        if requested_kind:
            for name in ('commercial', requested_kind, 'analysis' if requested_kind == 'layer123' else requested_kind):
                errors.pop(name, None)

        # Retry a locked view even when the source signature has not changed.
        for kind, item in list(pending.items()):
            if requested_kind and kind != requested_kind:
                continue
            retried = retry_fixed_view(Path(item['数据源工作簿']), Path(item['固定查看副本']))
            deliveries[kind] = {**deliveries.get(kind, {}), **retried}
            if retried['固定查看副本已更新']:
                deliveries[kind].pop('固定查看副本错误', None)
                pending.pop(kind)

        def record(kind, delivered):
            deliveries[kind] = delivered
            if not (delivered.get('固定查看副本已更新') or delivered.get('时间戳查看快照已生成')):
                raise RuntimeError(f'{kind}: 工作簿已写入数据文件，但当前查看副本和快照均未交付')
            if delivered.get('固定查看副本已更新'):
                pending.pop(kind, None)
            else:
                pending[kind] = {name: delivered[name] for name in ('数据源工作簿', '固定查看副本')}

        common = [category_path, ROOT / 'scripts' / 'export_category_workbooks.py',
                  ROOT / 'scripts' / 'export_shower_layer123_excel.py',
                  ROOT / 'src' / 'selection_workflow' / 'excel_images.py',
                  ROOT / 'src' / 'selection_workflow' / 'commercial_excel.py',
                  ROOT / 'src' / 'selection_automation' / 'commercial.py',
                  ROOT / 'src' / 'selection_automation' / 'identity.py',
                  ROOT / 'src' / 'selection_automation' / 'jsonl_records.py',
                  ROOT / 'src' / 'selection_workflow' / 'excel_delivery.py']
        common += [ROOT/'scripts/export_selection_result.py', ROOT/'src/selection_automation/selection_delivery.py',
                   ROOT/'src/selection_automation/operations.py', ROOT/'src/selection_automation/stage3_provider.py']
        config = base / 'stage12_config.json'
        cfg = read(config, {})
        db = Path(cfg['output_dir']) / 'stage12.sqlite3' if cfg.get('output_dir') and not only_collection else None
        analysis_changed = False
        frozen = base / 'stage123' / 'export_frozen'
        codex_paths = sorted((base / 'stage123/codex_reviews').glob('*.jsonl'))
        codex_available = any(path.stat().st_size for path in codex_paths)
        database_available=bool(db and db.exists() and requested_kind in (None, 'layer123'))
        if database_available:
            from selection_automation.selection_delivery import delivery_source_paths
            paid_marker=base/'stage123'/'paid_enabled.json'
            vision_results=base/'stage123'/'results.jsonl'
            database_files_before = signature([db,db.with_name(db.name+'-wal')])
            database_before = database_signature([db,db.with_name(db.name+'-wal')])
            before = database_before + signature([
                                paid_marker, vision_results,
                                base / 'stage123' / 'requests.jsonl', base / 'stage123' / 'request_intents.jsonl', config, *common,
                                ROOT / 'src' / 'selection_automation' / 'rules.py',
                                ROOT / 'src' / 'selection_automation' / 'vision.py',
                                ROOT / 'scripts' / 'export_incremental_layer123_excel.py',
                                ROOT / 'src/selection_automation/codex_visual_review.py', *codex_paths,
                                *delivery_source_paths(base)])
            if before != prior.get('analysis'):
                try:
                    files_ready=all((frozen/name).exists() for name in (
                        f'layer12_rows_{key}.jsonl',f'layer12_summary_{key}.json',f'layer3_candidates_{key}.jsonl'))
                    if not files_ready or database_before != prior.get('analysis_database'):
                        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'run_incremental_stage12.py'),
                                    'snapshot', '--config', str(config), '--snapshot-dir', str(frozen),
                                    '--path-key', key], cwd=ROOT, check=True, stdout=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                        current['analysis_database']=database_before
                    analysis_changed = True
                except Exception as exc:
                    errors['analysis'] = str(exc)
        business_path = base / 'commercial_report.json'
        from selection_automation.commercial import report_signature
        # Bind after the SQLite freeze, using the same evidence identity as readers.
        business_signature = report_signature(ROOT, cat)
        if not only_collection and (business_signature != prior.get('commercial') or not business_path.exists()):
            try:
                from selection_automation.commercial import build_report
                save(business_path, build_report(ROOT, cat))
                current['commercial'] = business_signature
            except Exception as exc:
                errors['commercial'] = str(exc)
        common += [business_path, ROOT / 'src/selection_workflow/commercial_excel.py']
        for kind, path in [('amazon', base / 'amazon' / 'amazon_frontend_latest.json'),
                           ('temu', base / 'temu' / 'union' / 'products.jsonl')]:
            if requested_kind and kind != requested_kind:
                continue
            if path.exists():
                dependencies = [base / 'temu' / 'union' / 'summary.json', *(base/'temu/workers').glob('*/*/products.jsonl')] if kind == 'temu' else []
                data_signature = signature([path, *dependencies, *common])
                if data_signature != prior.get(kind):
                    try:
                        record(kind, export(category_path, kind))
                        current[kind] = data_signature
                    except Exception as exc:
                        errors[kind] = str(exc)
        business_changed = current.get('commercial') != prior.get('commercial')
        if database_available and (analysis_changed or business_changed) and 'analysis' not in errors:
            try:
                from watch_stage3_excel import source_paths, signature as stage3_signature
                stage3_before=stage3_signature(source_paths(category_path.absolute(),base.absolute()))
                stage3_database_before=current.get('analysis_database',database_before)
                # One focused delivery also describes unfinished analysis honestly.
                record('layer123', export(category_path, 'layer123'))
                if codex_available or (paid_marker.exists() and vision_results.exists() and vision_results.stat().st_size):
                    from watch_stage3_excel import source_paths, signature as stage3_signature, database_paths
                    # Both publishers deliver the same workbook under the same lock.
                    # Capture inputs BEFORE export: later arrivals must stay dirty.
                    current['stage3_excel'] = stage3_before
                    current['stage3_excel_database'] = stage3_database_before
                    current['stage3_excel_database_files'] = database_files_before
                    current['stage3_excel_updated_at'] = time.time()
                current['analysis'] = before
            except Exception as exc:
                errors['analysis'] = str(exc)
        current.update(errors=errors, pending_views=pending, deliveries=deliveries)
        save(base / 'publish_state.json', current)
        return current


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--category', type=Path, required=True)
    p.add_argument('--kind', choices=('amazon', 'temu', 'layer123'))
    args = p.parse_args()
    try:
        import psutil
        psutil.Process().nice(psutil.NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
    except (ImportError, OSError):
        pass
    state = publish(args.category, kind=args.kind)
    if state['errors']:
        raise SystemExit(json.dumps(state['errors'], ensure_ascii=False))


if __name__ == '__main__':
    main()
