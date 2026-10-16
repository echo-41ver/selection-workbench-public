"""Bounded throughput tuning with no speculative or duplicate provider calls."""
import math
import hashlib
import json
from pathlib import Path
import time


def profile(provider):
    if provider['mode']=='local_agent':
        ids=sorted(provider.get('agent_ids') or ['codex'])
        return {'mode':'local_agent','agent_ids':ids,
                'agent_models':{key:provider.get('agent_models',{}).get(key,'') for key in ids}}
    return {key:provider.get(key,'') for key in ('mode','base_url','model','protocol')}


def profile_path(provider,root=None):
    identity=json.dumps(profile(provider),sort_keys=True,ensure_ascii=False)
    return Path(root or Path(__file__).resolve().parents[2])/'runtime/analysis-concurrency'/(
        hashlib.sha256(identity.encode()).hexdigest()+'.json')


def read_samples(path):
    try:
        value=json.loads(path.read_text('utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def stable_best(samples):
    """Require two full, successful windows; a failure resets that width."""
    widths={}
    for sample in samples:
        if not isinstance(sample,dict):continue
        width=sample.get('workers');completed=sample.get('completed');failed=sample.get('failed');seconds=sample.get('seconds')
        if type(width) is not int or width<1:continue
        if (type(completed) is not int or completed<0 or type(failed) is not int or failed<0
                or type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<=0):continue
        if failed:widths[width]=[];continue
        if sample.get('saturated') is True and completed>=max(8,width*2):
            widths.setdefault(width,[]).append((completed,seconds))
    candidates=[{'best_width':width,'best_rate':sum(n for n,_ in rows)/sum(s for _,s in rows)}
                for width,rows in widths.items() if len(rows)>=2]
    return max(candidates,key=lambda row:(row['best_rate'],-row['best_width']),default=None)


def profile_samples(provider,root=None):
    path=profile_path(provider,root);saved=read_samples(path)
    samples=saved.get('samples',[]) if saved.get('profile')==profile(provider) else []
    # Existing per-task tuning is also evidence, but an old best_rate alone is
    # insufficient: it may survive a series of failed requests.
    if not path.exists():
        run_root=Path(root or Path(__file__).resolve().parents[2])/'runs'
        pattern='*/stage123/codex_pool/concurrency.json' if provider['mode']=='local_agent' else '*/stage123/concurrency.json'
        for prior in sorted(run_root.glob(pattern),key=lambda p:p.stat().st_mtime):
            value=read_samples(prior)
            if provider['mode']=='local_agent':
                config=read_samples(prior.parent.parent/'codex_pool_config.json')
                source={'mode':'local_agent','agent_ids':config.get('agent_ids') or [a['id'] for a in config.get('agent_backends',[])],
                        'agent_models':config.get('agent_models',{})}
                try:configuration=json.loads(value.get('configuration','null'))
                except ValueError:continue
                if not isinstance(configuration,list) or not configuration or configuration[0]!=source['agent_models']:continue
            else:source=value.get('provider',{})
            if source.get('mode')==provider['mode'] and profile(source)==profile(provider):samples.extend(value.get('samples',[]))
    return samples


def recommendation(provider,root=None):
    best=stable_best(profile_samples(provider,root))
    fallback=6*max(1,len(provider.get('agent_ids') or [])) if provider['mode']=='local_agent' else 2 if provider['mode']=='local_model' else 4
    width=best['best_width'] if best else fallback
    return {'workers':width,'measured':bool(best),
            'message':f'默认 {width} 路：此前稳定成功记录中吞吐量最高。' if best else f'默认 {width} 路：暂无足够稳定实测，沿用起始值；可手动修改。'}


def recommended(mode, agents=1, *, provider=None,root=None):
    provider=provider or {'mode':mode,'agent_ids':['codex'] if mode=='local_agent' and agents==1 else [str(i) for i in range(agents)]}
    return recommendation(provider,root)['workers']


def record_sample(provider,width,completed,failed,seconds,*,saturated=True,root=None):
    if not saturated or not math.isfinite(seconds) or seconds<=0:return
    from .process_lock import ProcessLock
    from .merge_temu_snapshots import atomic_write
    path=profile_path(provider,root)
    try:
        with ProcessLock(path.with_suffix('.lock')):
            previous=read_samples(path)
            rows=previous.get('samples',[]) if previous.get('profile')==profile(provider) else profile_samples(provider,root)
            rows.append({'workers':width,'completed':completed,'failed':failed,'seconds':seconds,'saturated':True})
            atomic_write(path,json.dumps({'profile':profile(provider),'samples':rows[-120:],'updated_at':time.time()}))
    except (OSError,RuntimeError):pass  # Statistics must never block real results.


class ThroughputTuner:
    """Keep the best measured width; stop exploring once gains flatten or fail."""
    def __init__(self, initial=4, maximum=16, state=None):
        self.maximum = maximum
        self.width = min(maximum, max(1, initial))
        self.best_width = self.width
        self.best_rate = 0.0
        self.samples = []
        self.exploring = True
        if state and state.get('best_rate', 0) > 0:
            self.width = self.best_width = min(maximum, max(1, int(state['best_width'])))
            self.best_rate = float(state['best_rate'])

    def observe(self, completed, failed, seconds, saturated=True):
        if not math.isfinite(seconds) or seconds <= 0:
            return self.width
        rate = completed / seconds
        self.samples.append({'workers': self.width, 'completed': completed, 'failed': failed,
                             'seconds': round(seconds, 3), 'pairs_per_second': round(rate, 6),'saturated':bool(saturated)})
        if failed:
            self.width = max(1, min(self.best_width, self.width // 2))
            self.exploring = False
        elif saturated and completed >= self.width * 2:
            if self.best_rate == 0 or rate > self.best_rate * 1.05:
                self.best_rate, self.best_width = rate, self.width
                if self.exploring:
                    self.width = min(self.maximum, self.width * 2)
            else:
                self.width, self.exploring = self.best_width, False
        return self.width

    def snapshot(self):
        return {'workers': self.width, 'best_width': self.best_width,
                'best_rate': self.best_rate, 'samples': self.samples[-20:], 'exploring': self.exploring}
