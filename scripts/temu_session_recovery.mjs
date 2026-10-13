// A collector hands site stops to its owning supervisor after saving snapshots.
const CAPTCHA_STATUSES = new Set(['WAITING_CAPTCHA', 'CAPTCHA_REQUIRED', 'WAITING_SEARCH_VERIFICATION',
  'WAITING_EMPTY_SEARCH_VERIFICATION', 'WAITING_HUMAN_VERIFICATION']);

export function captchaNeedsManualVerification(status, progress = {}) {
  return CAPTCHA_STATUSES.has(status) && progress.scroll_collection_started !== true;
}

export function sessionRecoveryRequest(status, progress = {}, enabled = false) {
  if (!enabled || ['STOPPED_BROWSER_COMMAND_TIMEOUT', 'STOPPED_USER_REQUEST',
    'KEYWORD_COMPLETE', 'KEYWORD_LIMIT_REACHED', 'WORKER_COMPLETE'].includes(status)) return null;
  // A prior checkpoint's goods/round count is not proof this session has scrolled.
  if (captchaNeedsManualVerification(status, progress)) return null;
  let kind = null;
  if (CAPTCHA_STATUSES.has(status)) kind = 'CAPTCHA';
  else if (['STOPPED_LOGIN_REQUIRED', 'WAITING_LOGIN', 'LOGIN_REQUIRED'].includes(status)
    || progress.confirmed_login_redirect === true) kind = 'LOGIN';
  else if (status === 'STOPPED_HTTP_429' || Number(progress.http_block?.status || progress.http_status) === 429) kind = 'RATE_LIMIT';
  else if (['STOPPED_HTTP_403', 'STOPPED_ACCESS_DENIED'].includes(status)
    || Number(progress.http_block?.status || progress.http_status) === 403) kind = 'ACCESS';
  return kind ? { session_recovery_kind: kind, manual_action_required: false,
    automatic_retry: true, automatic_recovery_eligible: true,
    resume_requires_session_rotation: true, automatic_resume_pending: true } : null;
}
