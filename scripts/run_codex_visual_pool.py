"""Continuously dispatch local, revision-bound image groups through Codex OAuth."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
POOL_SCHEMA_REVISION = 3
WINDOWS_BUNDLED_PYTHON = Path(r'C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.category_config import load_category
from selection_automation.codex_visual_review import checked_facts, import_reviews, normalize_review
from selection_automation.jsonl_records import read_jsonl
from selection_automation.process_lock import ProcessLock
from prepare_codex_visual_batch import prepare_batch, _contact_sheets, _facts_guide, _write_jsonl
from watch_stage3_excel import ensure_publisher, read, save


def pool_facts_guide(category):
    """Add the strict identity wire shapes missing from the general guide."""
    guide = _facts_guide(category)
    stage = category['stage3']
    facts = guide['facts_output_template']
    policy = stage.get('identity_policy') or {}
    identity_features = [row['id'] for row in policy.get('features', [])
                         if row.get('role') == 'identity' and row.get('evidence') in {'image', 'both'}]
    guide['feature_comparison_ids'] = identity_features
    facts['feature_comparisons'] = {
        fid: {'relation':'UNKNOWN', 'amazon':'', 'temu':''} for fid in identity_features
    }
    from selection_automation.set_inventory import requirements
    inventory_requirements = requirements(category)
    guide['inventory_requirements'] = inventory_requirements
    for fid in inventory_requirements:
        facts['feature_comparisons'][fid]['inventory'] = {
            side: {'complete': False, 'groups': []} for side in ('amazon', 'temu')
        }
    if inventory_requirements:
        guide['inventory_group_shape'] = {
            'region': '<实际可见区域>', 'item_type': '<该特征允许的类型枚举>',
            'count': '<实际可见数量，整数1至200>',
        }
    facts['identity']['matches'] = []
    facts['identity']['differences'] = []
    guide['identity_array_item_shapes'] = {
        'matches': {'required_keys':['feature','amazon','temu'],
                    'example':{'feature':'<具体款式部位>','amazon':'<Amazon实际可见细节>','temu':'<TEMU对应实际可见细节>'}},
        'differences': {'required_keys':['field','amazon','temu','kind','variant_field'],
                        'kind_values':['IDENTITY','VARIANT','UNRESOLVED'],
                        'variant_field_values':['NONE', *[v for v in stage.get('variant_fields', []) if v not in stage['modules']]],
                        'example':{'field':'<具体差异部位>','amazon':'<Amazon实际可见细节>','temu':'<TEMU对应实际可见细节>',
                                   'kind':'IDENTITY','variant_field':'NONE'}},
    }
    guide['strict_validation_notes'] = [
        'identity.matches 与 identity.differences 必须是对象数组，禁止写字符串数组或把模块名当作整个对象。空数组只在确实没有该类证据时使用。',
        '每个 matches 对象只能含 feature、amazon、temu；每个 differences 对象必须含 field、amazon、temu、kind、variant_field。',
        'kind=IDENTITY 或 UNRESOLVED 时 variant_field 必须为 NONE；kind=VARIANT 时 variant_field 必须使用本指南列出的规范值。',
        'feature_comparisons 的键必须与 feature_comparison_ids 完全一致；普通项恰有 relation、amazon、temu；inventory_requirements列出的特征还必须包含inventory。',
        '每个 modules 项恰有 relation、amazon_evidence、temu_evidence、hard_conflict_code；没有 confidence 字段，也不要自创分数。',
        '上述尖括号内容只是字段形状示意，不是商品事实；只能用当前两张图实际可见的证据替换，不得照抄或补猜。',
    ]
    if inventory_requirements:
        guide['strict_validation_notes'].append(
            'inventory恰含amazon、temu；每侧恰含complete布尔值和groups数组。'
            '每组恰含region非空字符串、item_type允许枚举、count整数1至200。'
            '逐件核对双方原图，只记录实际可见物件；遮挡或不能数清时complete=false，不猜隐藏数量。')
    guide['pool_schema_revision'] = POOL_SCHEMA_REVISION
    return guide


def validator_source():
    """Generate a read-only per-job validator with the same strict normalizer."""
    return f'''from pathlib import Path
import argparse, json, sys
ROOT = Path({str(ROOT)!r})
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from selection_automation.category_config import load_category
from selection_automation.codex_visual_review import checked_facts, normalize_review
from selection_automation.jsonl_records import read_jsonl
from watch_stage3_excel import save

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--category", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.jsonl"))
    parser.add_argument("--reviews", type=Path, default=Path(__file__).with_name("reviews.jsonl"))
    args=parser.parse_args()
    category=load_category(args.category)
    expected={{(row.get("asin"),row.get("goods_id"),row.get("vision_revision_key")):row for row in read_jsonl(args.manifest)}}
    errors=[]
    total=0
    for line,row in enumerate(read_jsonl(args.reviews,allow_incomplete_tail=True),1):
        total+=1
        identity=(row.get("asin"),row.get("goods_id"),row.get("vision_revision_key"))
        pair=expected.get(identity)
        try:
            if pair is None: raise ValueError("review absent from group manifest")
            checked_facts(category,row.get("visual_facts"),row.get("goods_id"))
            normalize_review(category,row,pair,Path(__file__).parent.name)
        except (ValueError,KeyError,TypeError) as exc:
            errors.append({{"line":line,"asin":row.get("asin"),"goods_id":row.get("goods_id"),
                           "vision_revision_key":row.get("vision_revision_key"),"error":str(exc)}})
    save(Path(__file__).with_name("validation_errors.json"),errors)
    print(json.dumps({{"valid_rows":total-len(errors),"error_count":len(errors),"errors":errors}},ensure_ascii=False,indent=2))
    return 1 if errors else 0

if __name__=="__main__": raise SystemExit(main())
'''


def key(row):
    return row.get('asin'), row.get('goods_id'), row.get('vision_revision_key')


def codex_executable(config):
    configured = config.get('codex_executable') or shutil.which('codex')
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    folder = Path(os.environ.get('LOCALAPPDATA', '')) / 'OpenAI/Codex/bin'
    found = sorted(folder.glob('*/codex.exe'), key=lambda p: p.stat().st_mtime, reverse=True)
    if found:
        return str(found[0])
    raise FileNotFoundError('Codex CLI unavailable; sign in to Codex and configure codex_executable')


def owner_active(path):
    """An unfinished desktop reviewer retains exclusive ownership of its shard."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(max(0, Path(path).stat().st_size - 262144))
            lines = stream.read().decode('utf-8', errors='ignore').splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            event = row.get('payload', {})
            if row.get('type') == 'event_msg' and event.get('type') in (
                    'task_started', 'task_complete', 'turn_aborted'):
                return event['type'] == 'task_started'
    except OSError:
        pass
    return True  # Missing ownership evidence is not permission to take a live shard.


def tasks(base):
    for path in sorted((base / 'nightly_review').glob('*/review-batch.json')):
        data = read(path, {})
        if data.get('category_key') != base.name:
            continue
        for item in data.get('tasks', []):
            manifest = Path(item['manifest']).resolve()
            manifest.relative_to(path.parent.resolve())
            output = Path(item['output']).resolve()
            output.relative_to((base / 'stage123/codex_reviews').resolve())
            rows = list(read_jsonl(manifest))
            if not rows:
                continue
            if len({key(row) for row in rows}) != len(rows):
                raise ValueError(f"duplicate manifest identities: {item['task_id']}")
            yield {**item, 'rows': rows}


def pending_rows(category, task):
    bound = {key(row): row for row in task['rows']}
    received = set()
    for row in read_jsonl(Path(task['output'])):
        identity = key(row)
        if identity not in bound:
            continue
        try:
            normalize_review(category, row, bound[identity], task['task_id'])
        except (ValueError, KeyError, TypeError):
            continue
        received.add(identity)
    return [row for row in task['rows'] if key(row) not in received]


def command(executable, job, model='gpt-6.1-sol', effort='medium'):
    args = [executable, 'exec', '--ignore-user-config', '--ephemeral',
            '-m', model, '-c', f'model_reasoning_effort="{effort}"',
            '-c', 'service_tier="default"', '-c', 'approval_policy="never"',
            '-c', 'sandbox_mode="danger-full-access"', '--json',
            '--output-last-message', str(job / 'summary.txt'),
            '-i', str(job / 'contacts/contact-001.jpg')]
    # Give the model the actual images, not just a crowded contact sheet.
    inputs = read(job / 'image-inputs.json', {})
    for image in inputs.get('images', []):
        args.extend(['-i', image['path']])
    return args + ['-']


def bind_original_images(job, rows):
    images, pairs = {}, []
    for row in rows:
        bound = {'identity': list(key(row))}
        for side in ('amazon', 'temu'):
            path = Path(row[f'{side}_image_path']).resolve()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            images.setdefault(str(path), {'path':str(path), 'sha256':digest})
            bound[side] = str(path)
        pairs.append(bound)
    save(job / 'image-inputs.json', {'images':list(images.values()), 'pairs':pairs})


def check_original_binding(job, pair):
    inputs = read(job / 'image-inputs.json', {})
    if not inputs:
        if read(job / 'job.json', {}).get('schema_revision', 0) >= 3:
            raise ValueError('confirmed pair has no attached original image receipt')
        # Pre-contract in-flight jobs retain their original validation contract.
        return
    bound = next((r for r in inputs['pairs'] if r['identity'] == list(key(pair))), None)
    images = {r['path']:r['sha256'] for r in inputs['images']}
    if bound is None:
        raise ValueError('confirmed pair absent from attached original images')
    for side in ('amazon', 'temu'):
        path = str(Path(pair[f'{side}_image_path']).resolve())
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError('confirmed pair original image unavailable') from exc
        if bound.get(side) != path or images.get(path) != digest:
            raise ValueError('confirmed pair original image binding changed')


def same_amazon_groups(rows, group_size):
    groups = {}
    for row in rows:
        groups.setdefault(row['asin'], []).append(row)
    return [group[offset:offset+group_size] for group in groups.values()
            for offset in range(0, len(group), group_size)]


def job_environment(config):
    env = {k: v for k, v in os.environ.items() if k not in ('DEEPSEEK_API_KEY', 'OPENAI_API_KEY')}
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    proxy = config.get('proxy_url')
    if not proxy and os.name == 'nt' and not env.get('HTTPS_PROXY') and not env.get('https_proxy'):
        # Electron honors WinINET; the standalone CLI needs explicit proxy variables.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as registry:
                enabled = winreg.QueryValueEx(registry,'ProxyEnable')[0]
                server = winreg.QueryValueEx(registry,'ProxyServer')[0]
            if enabled and server:
                if '=' in server:
                    parts = dict(item.split('=',1) for item in server.split(';') if '=' in item)
                    server = parts.get('https') or parts.get('http')
                if server:
                    proxy = server if '://' in server else 'http://' + server
        except OSError:
            pass
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy)
    return env


def prompt(job, target, model, category_path):
    python = WINDOWS_BUNDLED_PYTHON
    validator = job / 'validate_reviews.py'
    validate_command = (f"& '{python}' '{validator}' --category '{category_path}' "
                        f"--manifest '{job / 'manifest.jsonl'}' --reviews '{job / 'reviews.jsonl'}'")
    return f"""完成这组真实图片严格同款核验，共{target}对。上下文只读取一次。
已附联系图，左 A 是 Amazon，右 T 是 TEMU，序号与 {job / 'manifest.jsonl'} 一致。
另已直接附上 image-inputs.json 列出的双方原图。本组只有一个Amazon基准商品；先独立识别这张Amazon原图的出售主体、驱动/控制结构、管路，再逐一识别TEMU主体。不得把相邻行、另一商品或联系图位置的描述挪到本行。两侧都出现软管或泵头不能证明同款。
协议与完整字段模板：{job / 'visual-facts-guide.json'}。
原图路径已写入 manifest 的绝对路径；只按该路径打开，不要自行把路径拼到 runs、category 或其他目录下。
每条SAME必须逐对核对已经附上的双方原图，事实仅来自本行明确绑定的两图；缩略图不清晰就查看 manifest 指定原图。无法看到原图时不能写SAME，也不能凭标题补造泵头、按钮、颜色或出液部件。
写 UTF-8 JSONL 到 {job / 'reviews.jsonl'}，每对一条，严格保留 asin、goods_id、vision_revision_key。
每条包含 review_status（SAME/DIFFERENT/UNKNOWN）、amazon_evidence、temu_evidence、reason、
reviewer="{job.name}"、model="{model}"、visual_facts（完整 modules、identity、overall_confidence、要求的 feature_comparisons）。
严格按 visual-facts-guide.json 的对象字段输出。identity.matches 每一项是 {{"feature":"具体部位","amazon":"Amazon侧实际证据","temu":"TEMU侧对应证据"}} 对象；identity.differences 每一项是 {{"field":"具体差异部位","amazon":"Amazon侧实际证据","temu":"TEMU侧对应证据","kind":"IDENTITY|VARIANT|UNRESOLVED","variant_field":"规范变体字段或NONE"}} 对象，不能写字符串列表。identity、modules、feature_comparisons 的键、字段必须齐全且不能增加字段；UNKNOWN模块/特征的双方证据使用空字符串。顶层amazon_evidence、temu_evidence始终填写本行原图实际可见事实的非空摘要；整体UNKNOWN不代表两侧所有外观不可见，不能抹掉已观察的事实。
每个模块只用 relation、amazon_evidence、temu_evidence、hard_conflict_code 四字段；不得添加confidence。feature_comparisons 只使用指南中列出的全部身份特征ID；普通项只含 relation、amazon、temu；inventory_requirements列出的特征必须额外包含双侧inventory，严格采用指南的complete/groups及region/item_type/count字段，逐件查看原图，不凭标题推断数量。
不能捏造事实、分数、模块证据或身份特征；不得把通用“同类部件”写成匹配证据。具体差异需有两侧对应事实。颜色、标签、包装等只能按当前品类明确允许的变体标注；不得用变体解释结构差异。
如果目录中已有 validation_errors.json，先只修复其中明确列出的无效行；保留 reviews.jsonl 里已通过验证的行及其原文，不要从头重看有效行。对错误行只改 schema/字段映射，视觉事实本身不足时重新查看该行原图并补足真实观察，不猜测。
写完后运行本 job 的严格校验脚本：
{validate_command}
使用该绝对 Python 路径通过 exec_command 运行；检查 validation_errors.json，逐条修复后再次运行，直到本组所有可判定行通过。若 MCP/桥接查看图片失败，不要调用 MCP 桥反复尝试：使用 Codex 原生 view_image 或 exec_command 读取 manifest 里的绝对原图路径；Python 操作使用给出的 bundled python。
只写本组 reviews.jsonl；不改代码、不操作浏览器、不调用付费API、不改其他任务或Excel。
已有结果保留；仅在新任务无旧 reviews 时从头分析。错误行修复后不得删除本组其他行。完成本组全部待修/待写行后报告有效、无效数量及具体校验结果。父程序验证、导入并自动分配下一组。
简短报告实际写入数量。Windows运行Python设置UTF8。"""


def _absolute_image_rows(rows, root=ROOT):
    result = []
    for source in rows:
        row = dict(source)
        for field in ('amazon_image_path', 'temu_image_path'):
            value = row.get(field)
            if isinstance(value, str) and value:
                path = Path(value)
                row[field] = str((path if path.is_absolute() else root / path).resolve())
        result.append(row)
    return result


def _group_digest(rows):
    return hashlib.sha256(json.dumps([key(row) for row in rows]).encode()).hexdigest()[:16]


def _strict_attempt(record):
    """Treat pre-contract retry limits as fresh after a validator/prompt revision."""
    if record.get('schema_revision') != POOL_SCHEMA_REVISION:
        return {'count':0, 'next_retry_at':0, 'schema_revision':POOL_SCHEMA_REVISION}
    return record


def import_job(category, task, job):
    expected = list(read_jsonl(job / 'manifest.jsonl'))
    bound = {key(row): row for row in expected}
    good, errors, validation_errors = {}, [], []
    for line, row in enumerate(read_jsonl(job / 'reviews.jsonl', allow_incomplete_tail=True), 1):
        try:
            identity = key(row)
            if identity not in bound:
                raise ValueError('review absent from group manifest')
            # Expose the normalizer's actual nested reason instead of its broad
            # backwards-compatible pump wrapper when a strict fact field fails.
            if category['stage3'].get('codex_require_module_facts'):
                checked_facts(category, row.get('visual_facts'), row.get('goods_id'))
            normalize_review(category, row, bound[identity], task['task_id'])
            if row.get('review_status') == 'SAME':
                check_original_binding(job, bound[identity])
            good[identity] = row
        except (ValueError, KeyError, TypeError) as exc:
            detail = str(exc)
            if detail == 'pump Codex reviews require valid complete module facts':
                try:
                    checked_facts(category, row.get('visual_facts'), row.get('goods_id'))
                except (ValueError, KeyError, TypeError) as facts_exc:
                    detail = str(facts_exc)
            errors.append(detail)
            validation_errors.append({'line':line,'asin':row.get('asin'),'goods_id':row.get('goods_id'),
                                      'vision_revision_key':row.get('vision_revision_key'),'error':detail})
    save(job / 'validation_errors.json', validation_errors)
    complete_group = bool(expected) and len(good) == len(expected) and not errors
    if complete_group:
        # Keep group membership stable across schema retries; partial valid rows
        # remain in reviews.jsonl and are not needlessly re-reviewed.
        import_reviews(category, task['rows'], list(good.values()), task['output'], task['task_id'])
    if good:
        raw = Path(task['raw_reviews'])
        prior = {key(row): row for row in read_jsonl(raw, allow_incomplete_tail=True)}
        prior.update(good)
        save_rows(raw, list(prior.values()))
    return (len(good) if complete_group else 0), errors


def write_job_validator(job):
    (Path(job) / 'validate_reviews.py').write_text(validator_source(), encoding='utf-8')


def save_rows(path, rows):
    from selection_automation.merge_temu_snapshots import atomic_write
    atomic_write(path, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))


def alive_job(row):
    import psutil
    try:
        process = psutil.Process(row['pid'])
        return (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                and abs(process.create_time() - row['created']) < .01
                and str(Path(row['folder']) / 'summary.txt') in process.cmdline())
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


def job_reviews_ready(category, job):
    """Complete bound image facts do not depend on the CLI's final summary."""
    try:
        folder = Path(job['folder'])
        if not (folder / 'image-inputs.json').is_file():
            return False
        expected = list(read_jsonl(folder / 'manifest.jsonl'))
        reviews = list(read_jsonl(folder / 'reviews.jsonl'))
        bound = {key(row): row for row in expected}
        if (not expected or len(bound) != len(expected) or len(reviews) != len(expected)
                or {key(row) for row in reviews} != set(bound)):
            return False
        for row in reviews:
            checked_facts(category, row.get('visual_facts'), row.get('goods_id'))
            normalize_review(category, row, bound[key(row)], job['task_id'])
            check_original_binding(folder, bound[key(row)])
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def stop_owned_job(job):
    """Stop only the PID, creation time and summary-path bound to this job."""
    import psutil
    if not alive_job(job):
        return False
    try:
        process = psutil.Process(job['pid'])
        if (abs(process.create_time() - job['created']) >= .01
                or str(Path(job['folder']) / 'summary.txt') not in process.cmdline()):
            return False
        process.terminate()
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired:
            if not alive_job(job):
                return False
            process.kill()
            process.wait(timeout=5)
        return True
    except psutil.Error:
        return False


class Pool:
    def __init__(self, category_path, config):
        self.category_path = Path(category_path).resolve()
        self.base = self.category_path.parent
        self.category = load_category(self.category_path)
        self.config = config
        self.folder = self.base / 'stage123/codex_pool'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.processes = {}
        self.attempts = read(self.folder / 'attempts.json', {})
        self.errors = []
        self.last_prepare = 0
        self.completed_groups = 0
        # Adopt owned subprocesses after a supervisor restart instead of duplicating them.
        for path in sorted((self.folder / 'jobs').glob('*/job.json')):
            row = read(path, {})
            self.completed_groups += row.get('state') == 'complete'
            if row.get('state') == 'running':
                self.jobs[path.parent.name] = row

    def launch(self, task, rows):
        import psutil
        rows = _absolute_image_rows(rows)
        digest = _group_digest(rows)
        prior_attempt = _strict_attempt(self.attempts.get(digest, {}))
        attempt = prior_attempt.get('count', 0) + 1
        reusable = None
        jobs_dir = self.folder / 'jobs'
        for path in sorted(jobs_dir.glob('*/job.json'), key=lambda p:p.stat().st_mtime_ns, reverse=True):
            old = read(path, {})
            if (old.get('digest') == digest and old.get('state') == 'retry'
                    and old.get('identities') == [list(key(row)) for row in rows]
                    and (path.parent / 'reviews.jsonl').is_file()):
                reusable = path.parent
                break
        reused_from = reusable
        job = jobs_dir / f'{digest}-{attempt}-{time.time_ns()}'
        job.mkdir(parents=True, exist_ok=True)
        _write_jsonl(job / 'manifest.jsonl', rows)
        _contact_sheets(job, rows, per_contact=len(rows))
        bind_original_images(job, rows)
        if reused_from:
            shutil.copy2(reused_from / 'reviews.jsonl', job / 'reviews.jsonl')
            prior_errors = reused_from / 'validation_errors.json'
            if prior_errors.is_file():
                shutil.copy2(prior_errors, job / 'validation_errors.json')
        else:
            (job / 'reviews.jsonl').touch(exist_ok=True)
        # Reused review text remains intact. The next CLI invocation repairs only
        # rows the strict validator marked, rather than re-reviewing valid rows.
        save(job / 'visual-facts-guide.json', pool_facts_guide(self.category))
        write_job_validator(job)
        model = self.config.get('model', 'gpt-6.1-sol')
        text = prompt(job, len(rows), model, self.category_path)
        env = job_environment(self.config)
        with (job / 'events.jsonl').open('ab') as stdout, (job / 'stderr.log').open('ab') as stderr:
            child = subprocess.Popen(command(codex_executable(self.config), job, model,
                                             self.config.get('effort', 'medium')),
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        child.stdin.write(text.encode('utf-8'))
        child.stdin.close()
        row = {'task_id':task['task_id'], 'folder':str(job), 'pid':child.pid,
               'created':psutil.Process(child.pid).create_time(), 'started_at':time.time(),
               'state':'running', 'identities':[list(key(value)) for value in rows],
               'digest':digest, 'attempt':attempt, 'schema_revision':POOL_SCHEMA_REVISION,
               'reused_reviews_from':str(reused_from) if reused_from else None}
        save(job / 'job.json', row)
        self.jobs[job.name] = row
        self.processes[job.name] = child
        self.attempts[digest] = {'count':attempt, 'updated_at':time.time(),
                                 'schema_revision':POOL_SCHEMA_REVISION}
        save(self.folder / 'attempts.json', self.attempts)

    def tick(self):
        self.errors = []
        current = {row['task_id']:row for row in tasks(self.base)}
        for name, job in list(self.jobs.items()):
            process = self.processes.get(name)
            alive = process.poll() is None if process is not None else alive_job(job)
            task = current.get(job.get('task_id'))
            if alive:
                if not task or not job_reviews_ready(self.category, job) or not stop_owned_job(job):
                    continue
            try:
                count, errors = import_job(self.category, task, Path(job['folder'])) if task else (0, ['task missing'])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                count, errors = 0, [str(exc)]
            state = {**job, 'state':'complete' if count == len(job['identities']) else 'retry',
                     'imported':count, 'errors':errors, 'finished_at':time.time()}
            if state['state'] == 'retry':
                log_path = Path(job['folder']) / 'stderr.log'
                tail = log_path.read_text(encoding='utf-8',errors='replace')[-4096:] if log_path.exists() else ''
                network_failure = any(text in tail.lower() for text in (
                    'request timed out', 'stream disconnected', 'connection reset',
                    'connection refused', 'error sending request', '429', '502', '503'))
                prior = _strict_attempt(self.attempts.get(job['digest'],{}))
                self.attempts[job['digest']] = prior
                prior['next_retry_at'] = time.time() + (30 if network_failure else 15)
                if network_failure:
                    # Transport recovery is independent of malformed visual-fact retries.
                    prior['count'] = max(0, prior.get('count',1)-1)
                    errors.append('Codex连接暂时中断，冷却后自动重试')
                elif not errors:
                    errors.append(f"看图组只写入{count}/{len(job['identities'])}条，将补齐缺失行")
                save(self.folder/'attempts.json',self.attempts)
            save(Path(job['folder']) / 'job.json', state)
            self.errors.extend(errors)
            self.completed_groups += state['state'] == 'complete'
            self.jobs.pop(name)
            self.processes.pop(name, None)
        owners = self.config.get('external_owners', {})
        active = {tuple(identity) for job in self.jobs.values() for identity in job['identities']}
        waiting = []
        seen = set()
        external_pending = 0
        for task in current.values():
            rows = pending_rows(self.category, task)
            if task['task_id'] in owners and owner_active(owners[task['task_id']]):
                external_pending += len(rows)
                continue
            available = [row for row in rows if key(row) not in active and key(row) not in seen]
            seen.update(key(row) for row in available)
            group_size = int(self.config.get('group_size', 12))
            for group in same_amazon_groups(available, group_size):
                waiting.append((task, group))
        workers = int(self.config.get('workers', 6))
        def ready(rows):
            digest = _group_digest(rows)
            prior = _strict_attempt(self.attempts.get(digest,{}))
            return (prior.get('count',0) < int(self.config.get('max_attempts',3))
                    and time.time() >= prior.get('next_retry_at',0))
        if (len(self.jobs) < workers and sum(ready(rows) for _,rows in waiting) < workers-len(self.jobs)
                and time.monotonic() - self.last_prepare >= 30):
            self.last_prepare = time.monotonic()
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            outcome = prepare_batch(self.category_path, shards=workers, per_shard=int(self.config.get('group_size',12)), skip_empty=True,
                                    contact_group_size=int(self.config.get('group_size',12)),
                                    output_dir=self.base / 'nightly_review' / timestamp)
            if outcome['selected']:
                return self.tick()
        retry_exhausted = 0
        newly_dispatched = 0
        for task, rows in waiting:
            if len(self.jobs) >= workers:
                break
            digest = _group_digest(rows)
            prior = _strict_attempt(self.attempts.get(digest, {}))
            self.attempts[digest] = prior
            if prior.get('count',0) >= int(self.config.get('max_attempts',3)):
                retry_exhausted += len(rows)
                continue
            if time.time() < prior.get('next_retry_at',0):
                continue
            try:
                self.launch(task, rows)
                newly_dispatched += len(rows)
            except (OSError, ValueError, RuntimeError) as exc:
                self.errors.append(str(exc))
        ensure_publisher(self.category_path, root=ROOT)
        return {'phase':('并行看图并自动接续下一组' if self.jobs else
                         '有任务需重试或人工处理，已保存结果保留' if self.errors or retry_exhausted else
                         '等待已有任务或冷却后自动补齐，结果保留' if waiting or external_pending else
                         '当前候选已分析，持续检测新增候选并汇总Excel'),
                'running':len(self.jobs), 'pending':sum(len(rows) for _, rows in waiting)-newly_dispatched,
                'external_pending':external_pending, 'retry_exhausted':retry_exhausted,
                'workers':workers, 'group_size':int(self.config.get('group_size',12)),
                'model':self.config.get('model','gpt-6.1-sol'), 'effort':self.config.get('effort','medium'),
                'service_tier':'default', 'completed_groups':self.completed_groups,
                'jobs':list(self.jobs.values()), 'errors':self.errors, 'api_requests_sent':0,
                'updated_at':time.time()}


def ensure_pool(category_path, root=ROOT):
    import psutil
    category_path = Path(category_path).resolve()
    config_path = category_path.parent / 'stage123/codex_pool_config.json'
    if not read(config_path, {}).get('enabled'):
        return
    folder = category_path.parent / 'stage123/codex_pool'
    folder.mkdir(parents=True, exist_ok=True)
    prior = read(folder / 'pool.json', {})
    try:
        process = psutil.Process(prior['pid'])
        if (process.is_running() and abs(process.create_time()-prior['created']) < .01
                and str(root / 'scripts/run_codex_visual_pool.py') in process.cmdline()):
            return
    except (KeyError, TypeError, ValueError, psutil.Error):
        pass
    with (folder / 'pool.log').open('ab') as log:
        return subprocess.Popen([sys.executable, str(root / 'scripts/run_codex_visual_pool.py'),
                                 '--category', str(category_path)], cwd=root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def watch(category_path, interval=5):
    import psutil
    category_path = Path(category_path).resolve()
    folder = category_path.parent / 'stage123/codex_pool'
    with ProcessLock(folder / 'pool.lock'):
        own = psutil.Process()
        identity = {'pid':os.getpid(), 'created':own.create_time()}
        # Initialization can scan many saved groups. Advertise the locked owner
        # immediately so ensure_pool does not spawn more managers meanwhile.
        save(folder / 'pool.json', {**read(folder / 'pool.json', {}), **identity,
             'phase':'初始化看图队列，结果保留', 'updated_at':time.time()})
        pool = Pool(category_path, read(category_path.parent / 'stage123/codex_pool_config.json', {}))
        while True:
            config = read(category_path.parent / 'stage123/codex_pool_config.json', {})
            if not config.get('enabled') or (category_path.parent / 'STOP').exists():
                # Only terminate subprocesses owned by this pool; collectors stay intact.
                for job in pool.jobs.values():
                    if alive_job(job):
                        psutil.Process(job['pid']).terminate()
                save(folder / 'pool.json', {**identity, 'phase':'已停止，结果保留', 'running':0, 'updated_at':time.time()})
                return
            pool.config = config
            try:
                state = pool.tick()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                state = {'phase':'调度暂缓，自动重试', 'errors':[str(exc)], 'running':len(pool.jobs), 'updated_at':time.time()}
            save(folder / 'pool.json', {**identity, **state})
            time.sleep(interval)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--category', type=Path, required=True)
    parser.add_argument('--interval', type=int, default=5)
    args = parser.parse_args()
    try:
        watch(args.category, max(2,args.interval))
    except RuntimeError as exc:
        if str(exc) != 'Another supervisor owns this installation':
            raise
