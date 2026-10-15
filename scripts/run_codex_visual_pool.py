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
    if not config.get('codex_executable'):
        from selection_automation.local_agents import find_executable
        found = find_executable(ROOT, 'codex')
        if found:
            return found
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
    args = [executable, 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
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
    from selection_automation.local_agents import environment
    env = environment()
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
    python = Path(sys.executable).resolve()
    validator = job / 'validate_reviews.py'
    validate_command = (f"& '{python}' '{validator}' --category '{category_path}' "
                        f"--manifest '{job / 'manifest.jsonl'}' --reviews '{job / 'reviews.jsonl'}'")
    return f"""核验本组 {target} 对商品。只按绑定的 Amazon/TEMU 双方原图和品类规则判断，禁止串图或照搬相邻商品事实。
联系图左 A 为 Amazon、右 T 为 TEMU；manifest.jsonl 与 image-inputs.json 给出身份、绝对原图路径及对应顺序。实际查看双方原图。
只读本组文件与规则：{job / 'manifest.jsonl'}、{job / 'visual-facts-guide.json'}。
同款：主体设计、核心结构和部件布局一致，仅存在品类明确允许的变体。类似款：整体款式接近但具体设计不满足同一基础款。不同款：存在明确双边身份差异。看不清或相互矛盾则 UNKNOWN，不能冒充不同款。
忽略背景、光线与营销水印；本体图案、结构、套装构成按品类边界判断。文字、向量相似度和同类通用部件均不能证明同款。
把结果写入 {job / 'reviews.jsonl'}，每对一条合法 UTF-8 JSONL。严格保留 asin、goods_id、vision_revision_key；review_status 为 SAME/SIMILAR/DIFFERENT/UNKNOWN。
amazon_evidence、temu_evidence 分别简述本图实际可见细节，reason 用一句话说明依据；reviewer="{job.name}"，model="{model}"。visual_facts 完整字段以指南模板为准，只写事实，不自行评分。UNKNOWN 特征证据留空，不能补猜；顶层双方摘要保留已看清的事实。
identity.matches/differences 是对象数组，feature_comparisons、modules 不增删键；inventory_requirements 指定的双侧inventory独立清点，遮挡或不能核实则不确认完整。
已有结果只修复 validation_errors.json 指定的无效行，保留有效行。若有命令工具运行：{validate_command}，修正明确错误；没有命令工具则由父程序校验，不能声称已经通过。
仅处理本组，不改代码、不操作浏览器、不调用其他 API、不改 Excel。结束时只简报写入数量。"""


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
                and row.get('command_marker',str(Path(row['folder']) / 'summary.txt')) in process.cmdline())
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
                or job.get('command_marker',str(Path(job['folder']) / 'summary.txt')) not in process.cmdline()):
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
        backends=self.config.get('agent_backends',[])
        available=[a for a in backends if sum(j.get('agent_id','codex')==a['id'] for j in self.jobs.values())<int(a.get('max_parallel',6))]
        if backends and not available:raise RuntimeError('Agent 并发槽位已满')
        agent=min(available,key=lambda a:sum(j.get('agent_id','codex')==a['id'] for j in self.jobs.values())/int(a.get('max_parallel',6))) if available else None
        rows = _absolute_image_rows(rows[:int(agent.get('max_group_size',12))] if agent else rows)
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
        native_pending=reused_from and (reused_from/'native-session.json').is_file() and read(reused_from/'native-session.json',{}).get('state')!='complete'
        if native_pending:
            prior_agent=read(reused_from/'job.json',{}).get('agent_id')
            if not agent or prior_agent!=agent['id']:raise RuntimeError('原桌面 Agent 任务需要先恢复，不能重复改派')
        job = jobs_dir / f'{digest}-{attempt}-{time.time_ns()}'
        job.mkdir(parents=True, exist_ok=True)
        _write_jsonl(job / 'manifest.jsonl', rows)
        save(job/'analysis-contract.json',{'category':str(self.category_path),'task_id':task['task_id']})
        _contact_sheets(job, rows, per_contact=len(rows))
        bind_original_images(job, rows)
        if reused_from:
            shutil.copy2(reused_from / 'reviews.jsonl', job / 'reviews.jsonl')
            if native_pending:shutil.copy2(reused_from/'native-session.json',job/'native-session.json')
            prior_errors = reused_from / 'validation_errors.json'
            if prior_errors.is_file():
                shutil.copy2(prior_errors, job / 'validation_errors.json')
        else:
            (job / 'reviews.jsonl').touch(exist_ok=True)
        # Reused review text remains intact. The next CLI invocation repairs only
        # rows the strict validator marked, rather than re-reviewing valid rows.
        save(job / 'visual-facts-guide.json', pool_facts_guide(self.category))
        write_job_validator(job)
        agent_id = agent['id'] if agent else 'codex'
        if agent:
            limit=int(agent.get('max_group_size',12))
            if len(rows)>limit:
                raise RuntimeError('Agent 分组必须在创建图片凭据前限制')
        selected=(agent or {}).get('selected_model') or self.config.get('agent_models',{}).get(agent_id)
        model=selected or (self.config.get('model','gpt-6.1-sol') if agent_id=='codex' else 'agent-default')
        if agent:agent={**agent,'selected_model':selected}
        text = prompt(job, len(rows), model, self.category_path)
        text += '\n如果当前 Agent 没有命令执行工具，写完 reviews.jsonl 后结束，由父进程运行严格校验；不要伪造校验通过。'
        env = job_environment(self.config)
        if agent_id=='workbuddy':
            from selection_automation.local_agents import workbuddy_environment
            env.update(workbuddy_environment(agent['executable']))
        if agent:
            from selection_automation.local_agents import analysis_command
            cli = analysis_command(agent,job,model if model!='agent-default' else None,self.config.get('effort','medium'))
        else:
            cli = command(codex_executable(self.config),job,model,self.config.get('effort','medium'))
        command_marker = str(job/'summary.txt') if agent_id=='codex' else str(job/'contacts/contact-001.jpg') if agent_id=='opencode' else str(job)
        with (job / 'events.jsonl').open('ab') as stdout, (job / 'stderr.log').open('ab') as stderr:
            child = subprocess.Popen(cli,
                cwd=job, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        child.stdin.write(text.encode('utf-8'))
        child.stdin.close()
        row = {'task_id':task['task_id'], 'folder':str(job), 'pid':child.pid,
               'created':psutil.Process(child.pid).create_time(), 'started_at':time.time(),
               'state':'running', 'identities':[list(key(value)) for value in rows],
               'digest':digest, 'attempt':attempt, 'schema_revision':POOL_SCHEMA_REVISION,
               'reused_reviews_from':str(reused_from) if reused_from else None,
               'agent_id':agent_id,'model':model,'command_marker':command_marker}
        save(job / 'job.json', row)
        self.jobs[job.name] = row
        self.processes[job.name] = child
        self.attempts[digest] = {'count':attempt, 'updated_at':time.time(),
                                 'schema_revision':POOL_SCHEMA_REVISION}
        save(self.folder / 'attempts.json', self.attempts)
        return len(rows)

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
            if self.config.get('agent_backends'):
                group_size=min(group_size,max(int(a.get('max_group_size',12)) for a in self.config['agent_backends']))
            for group in same_amazon_groups(available, group_size):
                waiting.append((task, group))
        workers = int(self.config.get('workers', 6))
        if self.config.get('agent_backends'):
            workers=min(workers,sum(int(a.get('max_parallel',6)) for a in self.config['agent_backends']))
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
            backends=self.config.get('agent_backends',[])
            available=[a for a in backends if sum(j.get('agent_id','codex')==a['id'] for j in self.jobs.values())<int(a.get('max_parallel',6))]
            if backends and not available:break
            if available:
                agent=min(available,key=lambda a:sum(j.get('agent_id','codex')==a['id'] for j in self.jobs.values())/int(a.get('max_parallel',6)))
                rows=rows[:int(agent.get('max_group_size',12))]
            digest = _group_digest(rows)
            prior = _strict_attempt(self.attempts.get(digest, {}))
            self.attempts[digest] = prior
            if prior.get('count',0) >= int(self.config.get('max_attempts',3)):
                retry_exhausted += len(rows)
                continue
            if time.time() < prior.get('next_retry_at',0):
                continue
            try:
                launched=self.launch(task, rows)
                newly_dispatched += launched if type(launched) is int else len(rows)
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
                'agents':[{'id':a['id'],'name':a['name'],'running':sum(j.get('agent_id','codex')==a['id'] for j in self.jobs.values())} for a in self.config.get('agent_backends',[])],
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
