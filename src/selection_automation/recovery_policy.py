"""Durable recovery history; known nonhuman failures remain automatic."""
import time
import math

CAPTCHA_STATUSES = {'WAITING_CAPTCHA', 'CAPTCHA_REQUIRED', 'WAITING_SEARCH_VERIFICATION',
                   'WAITING_EMPTY_SEARCH_VERIFICATION', 'WAITING_HUMAN_VERIFICATION'}


def captcha_needs_manual_verification(status, progress):
    return status in CAPTCHA_STATUSES and progress.get('scroll_collection_started') is not True

def recovery_budget(previous, goods, profile_id, kind, limit=None):
    before = previous.get('unique_goods_before')
    progressed = isinstance(before, (int, float)) and goods > before
    used = 0 if progressed else int(previous.get('attempts_without_progress', 0))
    # Keep history for cooldown and diagnostics; no lifetime session ceiling.
    return kind is not None and (limit is None or used < limit), {
        'attempts_without_progress': used + 1, 'unique_goods_before': goods,
        'profile_id': profile_id, 'kind': kind, 'started_at': time.time(),
        'progress_since_previous_recovery': progressed, 'limit': limit,
    }


def site_access_wait(kind, previous, worker_number=1):
    # A login redirect cannot heal by waiting in that environment. Only an
    # access refusal without a login redirect uses the site cooldown.
    if kind != 'ACCESS': return 0
    used = max(0, int(previous.get('attempts_without_progress', 0)))
    delay = (30, 120, 600, 1800, 3600, 7200, 21600)[min(used, 6)]
    return delay + max(0, worker_number - 1) * 11

def session_recovery_kind(status, progress):
    """CAPTCHA requests replacement only after this collector has begun scrolling."""
    if status in {'STOPPED_BROWSER_COMMAND_TIMEOUT', 'STOPPED_USER_REQUEST',
                  'KEYWORD_COMPLETE', 'KEYWORD_LIMIT_REACHED', 'WORKER_COMPLETE'}:
        return None
    if status in CAPTCHA_STATUSES:
        return None if captcha_needs_manual_verification(status, progress) else 'CAPTCHA'
    if status in {'STOPPED_LOGIN_REQUIRED', 'WAITING_LOGIN', 'LOGIN_REQUIRED'} or progress.get('confirmed_login_redirect') is True:
        return 'LOGIN'
    http = progress.get('http_block') or {}
    code = str(http.get('status') or progress.get('http_status') or '')
    if status == 'STOPPED_HTTP_429' or code == '429':
        return 'RATE_LIMIT'
    if status in {'STOPPED_HTTP_403', 'STOPPED_ACCESS_DENIED'} or code == '403':
        return 'ACCESS'
    return None


def automatic_retry_stopped(progress):
    if captcha_needs_manual_verification(progress.get('status'), progress):
        return True
    if session_recovery_kind(progress.get('status'), progress):
        return False
    return progress.get('manual_action_required') is True or progress.get('automatic_retry') is False


def recovery_kind(status, progress, transport_allowed=False):
    if captcha_needs_manual_verification(status, progress):
        return None
    site_kind = session_recovery_kind(status, progress)
    if site_kind:
        return site_kind
    if status == 'STOPPED_TRY_AGAIN_STALLED' and progress.get('automatic_recovery_eligible') is True:
        return 'TRANSPORT'
    if automatic_retry_stopped(progress):
        return None
    if progress.get('automatic_recovery_eligible') is False:
        return None
    if status == 'STOPPED_SALES_SORT_UNCONFIRMED': return 'SORT'
    if status == 'STOPPED_INITIAL_PRODUCTS_TIMEOUT': return 'INITIAL_PRODUCTS'
    if status == 'STOPPED_SEE_MORE_UNAVAILABLE' and not progress.get('http_block'): return 'CONTROL'
    if status == 'STOPPED_BOOTSTRAP_NAVIGATION':
        return 'TRANSPORT' if transport_allowed else None
    if transport_allowed: return 'TRANSPORT'
    return None


def rate_limit_wait(progress, previous, now=None):
    """Persist server cooldown before replacing a rate-limited session."""
    code = str((progress.get('http_block') or {}).get('status') or progress.get('http_status') or '')
    if progress.get('status') != 'STOPPED_HTTP_429' and code != '429':
        return None
    goods = int(progress.get('unique_goods') or 0)
    prior_goods = previous.get('unique_goods_before')
    attempts = 0 if isinstance(prior_goods,(int,float)) and goods > prior_goods else int(previous.get('attempts', 0))
    now = time.time() if now is None else now
    try: server_delay = max(0, float((progress.get('http_block') or {}).get('retry_after_seconds') or 0))
    except (TypeError, ValueError): server_delay = 0
    if not math.isfinite(server_delay): server_delay = 0
    delay = max(min(900, 60 * (2 ** min(attempts, 4))), server_delay, float(previous.get('retry_at', 0)) - now)
    return {'attempts': attempts + 1, 'unique_goods_before': goods,
            'retry_at': now + delay, 'wait_seconds': delay}


def sort_retry_wait(progress, previous):
    """Retry a local sorting failure independently of network/login history."""
    if progress.get('status') != 'STOPPED_SALES_SORT_UNCONFIRMED' or progress.get('http_block'):
        return None
    goods=int(progress.get('unique_goods') or 0)
    attempts=0 if goods>int(previous.get('unique_goods',0)) else int(previous.get('attempts',0))
    if attempts>=3: return None
    return {'attempts':attempts+1,'unique_goods':goods,'wait_seconds':(5,15,30)[attempts]}
