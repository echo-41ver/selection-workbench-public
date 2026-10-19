"""Read-only recovery checks after an uncertain, serialized driver command."""
import json
import time
from datetime import datetime
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


_VERIFY_HELPER=(Path(__file__).resolve().parents[2]/"scripts/temu_verification.mjs").read_text(encoding="utf-8").replace("export function", "function")

READY_SCRIPT = _VERIFY_HELPER+r"""
const url = new URL(location.href);
const visible = e => {const r=e.getBoundingClientRect(),s=getComputedStyle(e);
  return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';};
if (location.protocol!=='https:' || !/^(?:www\.)?temu\.com$/.test(url.hostname)
    || !['/','/search_result.html'].includes(url.pathname)) return false;
if (!['interactive','complete'].includes(document.readyState)) return false;
const text=(document.body?.innerText||'').slice(0,10000);
if(inspectHumanVerification())return false;
const hasProducts=document.querySelectorAll('[data-goods-id],a[href*="-g-"]').length>0;
if (!hasProducts && /access denied|verify (?:that )?you are human|security verification|验证码|安全验证|unusual traffic|too many requests|proxyconnectfailure|no connection|please check your network connection/i.test(text)) return false;
if ([...document.querySelectorAll('[role="dialog"],iframe[src*="captcha"],input[type="password"]')]
    .some(e=>visible(e) && (e.tagName==='IFRAME'||e.tagName==='INPUT'||/captcha|verify|verification|sign in|log in|access denied/i.test(e.innerText||'')))) return false;
if ([...document.querySelectorAll('button,a,[role="button"]')]
    .some(e=>visible(e)&&/^(?:try again|retry|重试)$/i.test((e.textContent||'').trim()))) return false;
const input=document.querySelector('input[type="search"],input[placeholder*="Search"],input[aria-label*="Search"]');
const usableInput=input && visible(input) && !input.disabled && !input.readOnly;
const target=String(url.searchParams.get('search_key')||'').trim().toLowerCase()===String(arguments[0]||'').trim().toLowerCase();
const goods=[...document.querySelectorAll('[data-goods-id],a[href*="-g-"]')].some(visible);
return Boolean(usableInput || (target && goods));
"""

GATE_SCRIPT = _VERIFY_HELPER+r"""
const path=location.pathname, text=(document.body?.innerText||'').slice(0,10000);
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
const hasProducts=document.querySelectorAll('[data-goods-id],a[href*="-g-"]').length>0;
if (/bgn_verification|captcha|security[-_]?verification/i.test(path) || inspectHumanVerification() || (!hasProducts && /security verification|verify (?:that )?you are human|验证码|安全验证/i.test(text))) return 'WAITING_HUMAN_VERIFICATION';
if (/\/login(?:\.html)?\/?$/i.test(path) || [...document.querySelectorAll('input[type=password],[role=dialog] input[type=email]')].some(visible)) return 'STOPPED_LOGIN_REQUIRED';
if (/access denied|request blocked|unusual traffic/i.test(text)) return 'STOPPED_ACCESS_DENIED';
return null;
"""

class BrowserGateDetected(RuntimeError):
    def __init__(self,status):
        super().__init__(status)
        self.status=status


class BrowserControlRecoveryTimeout(RuntimeError):
    status = 'STOPPED_BROWSER_CONTROL_RECOVERY_TIMEOUT'


CONTROL_RECOVERY_TERMINAL = {
    BrowserControlRecoveryTimeout.status, 'STOPPED_BROWSER_CONTROL_RECOVERY_BUDGET',
}


def control_stop_was_replaced(root, session, progress):
    """A completed replacement invalidates only the previous driver's stop."""
    if progress.get('status') not in CONTROL_RECOVERY_TERMINAL | {'STOPPED_BROWSER_COMMAND_TIMEOUT'}:
        return False
    current = str(session.get('profile_id') or '')
    stopped_profile = progress.get('control_recovery_profile_id')
    if current and stopped_profile:
        return str(stopped_profile) != current
    # Legacy stops require a newer successful replacement of this exact lane.
    try:
        number = int(session['name'].split('-')[1])
        journal = json.loads((Path(root) / 'runtime/proxyshare-chains' /
                              f'replacement-worker-{number}.json').read_text('utf-8'))
        stopped_at = datetime.fromisoformat(str(progress['updated_at']).replace('Z', '+00:00'))
        replaced_at = datetime.fromisoformat(journal['replaced_at'])
        return bool(current and journal.get('phase') == 'REPLACED'
                    and str(journal.get('new_profile_id')) == current
                    and stopped_at < replaced_at)
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return False


def reserve_control_recovery(path, unique_goods, *, limit=3, now=time.time):
    """Reserve before probing; the worker lease serializes this durable budget.

    Use a high-water mark so resetting a collector's counters or changing its
    session cannot grant another no-progress recovery allowance.
    """
    from .merge_temu_snapshots import atomic_write
    path = Path(path)
    goods = max(0, int(unique_goods or 0))
    try:
        previous = json.loads(path.read_text('utf-8')) if path.exists() else {}
        used = int(previous.get('attempts_without_progress', 0))
        high_water = int(previous.get('unique_goods_high_water', goods))
        if used < 0 or high_water < 0:
            raise ValueError('invalid recovery counters')
    except (OSError, ValueError, TypeError, AttributeError):
        # A damaged journal must not silently reset a spent allowance.
        return False, {'attempts_without_progress': limit, 'limit': limit,
                       'reason': 'RECOVERY_RECORD_INVALID'}
    progressed = goods > high_water
    if progressed:
        used = 0
    record = {**previous, 'attempts_without_progress': used,
              'unique_goods_high_water': max(goods, high_water), 'limit': limit,
              'progress_since_previous_recovery': progressed}
    if used >= limit:
        return False, record
    record.update(attempts_without_progress=used + 1, started_at=now())
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
    return True, record

def recovery_gate(root,session):
    """Read a fenced browser only; never navigate, submit, or interact with a gate."""
    try:
        sid=(Path(root)/session['session_id_file']).read_text(encoding='utf-8').strip()
        request=urllib.request.Request(session['webdriver_url']+'/session/'+sid+'/execute/sync',
            data=json.dumps({'script':GATE_SCRIPT,'args':[]}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
            status=json.load(response).get('value')
        return status if status in {'WAITING_HUMAN_VERIFICATION','STOPPED_LOGIN_REQUIRED','STOPPED_ACCESS_DENIED'} else None
    except (OSError,ValueError,TypeError,KeyError):
        return None


def control_fenced(root, session):
    """GET /url is queued behind the previous command; it never repeats that command."""
    try:
        sid=(Path(root)/session['session_id_file']).read_text(encoding='utf-8').strip()
        request=urllib.request.Request(session['webdriver_url']+'/session/'+sid+'/url')
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
            value=json.load(response).get('value')
        if not isinstance(value,str): return False
        url=urlparse(value)
        return url.scheme=='https' and url.hostname in {'temu.com','www.temu.com'}
    except (OSError,ValueError,TypeError,KeyError):
        return False


def recovery_page_ready(root, session, query):
    try:
        sid=(Path(root)/session['session_id_file']).read_text(encoding='utf-8').strip()
        body=json.dumps({'script':READY_SCRIPT,'args':[query]}).encode()
        request=urllib.request.Request(session['webdriver_url']+'/session/'+sid+'/execute/sync',
            data=body,headers={'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
            return json.load(response).get('value') is True
    except (OSError,ValueError,TypeError,KeyError):
        return False


def wait_until_ready(root, session, query, *, lease, stopped, observe, interval=10,
                     fence=control_fenced, ready=recovery_page_ready, wait=None, gate_probe=recovery_gate,
                     timeout_seconds=120, clock=time.monotonic):
    from .worker_runtime import WorkerStopRequested, interruptible_wait
    wait=wait or interruptible_wait
    checks=0
    deadline=clock()+timeout_seconds
    def check_pending():
        lease.check_stop()
        if stopped(): raise WorkerStopRequested('task stopped during browser recovery')
        if clock() >= deadline:
            raise BrowserControlRecoveryTimeout('browser control did not recover within the bounded wait')
    while True:
        check_pending()
        checks+=1
        fenced=fence(root,session)
        check_pending()
        # A visible search input can survive beneath a verification overlay.
        # Read the gate first, after fencing the uncertain earlier command.
        if fenced:
            gate=gate_probe(root,session)
            check_pending()
            if gate:raise BrowserGateDetected(gate)
        usable=fenced and ready(root,session,query)
        check_pending()
        observe(checks,fenced,usable)
        if usable:
            check_pending()
            return
        wait(min(interval,max(0,deadline-clock())),lease)
