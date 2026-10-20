"""A stable empty page is one observation; skipping needs independent re-searches."""
import json
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .merge_temu_snapshots import atomic_write

PROTOCOL='INDEPENDENT_EMPTY_SEARCH_V3'


def _valid_observation(evidence,query):
    try:
        url=urlsplit(evidence.get('url',''))
        actual=parse_qs(url.query).get('search_key',[''])[0]
        return (evidence.get('confirmed') is True and evidence.get('protocol')=='VISIBLE_EMPTY_RESULTS_STABLE_V2'
            and evidence.get('loading') is False and type(evidence.get('stable_ms')) in (int,float)
            and evidence['stable_ms']>=15000 and type(evidence.get('samples')) is int and evidence['samples']>=3
            and evidence.get('query')==query and url.scheme=='https' and url.hostname in ('temu.com','www.temu.com')
            and url.path=='/search_result.html' and actual.strip().casefold()==query.strip().casefold())
    except (TypeError,ValueError,AttributeError):return False


def verified_empty_evidence(evidence,query):
    if not isinstance(evidence,dict) or evidence.get('protocol')!=PROTOCOL or evidence.get('query')!=query:return False
    checks=evidence.get('checks')
    if not isinstance(checks,list) or len(checks)<2:return False
    profiles=set()
    for check in checks:
        if not isinstance(check,dict) or not isinstance(check.get('profile_id'),str) or not check['profile_id']:return False
        if not _valid_observation(check.get('evidence'),query):return False
        profiles.add(check['profile_id'])
    return evidence.get('confirmed') is True and len(profiles)>=2


def confirmed_empty_observation(progress,query):
    return (progress.get('status') in {'KEYWORD_NO_RESULTS','STOPPED_EMPTY_QUERY_RESULTS'}
        and progress.get('unique_goods')==0 and progress.get('raw_collected_count',0)==0
        and progress.get('manual_action_required') is not True
        and _valid_observation(progress.get('no_results_evidence'),query))


def confirmed_empty_checkpoint(progress,query):
    return (progress.get('status')=='KEYWORD_NO_RESULTS' and progress.get('unique_goods')==0
        and progress.get('raw_collected_count',0)==0 and progress.get('manual_action_required') is not True
        and progress.get('completion_basis')=='EXPLICIT_EMPTY_QUERY_RESULTS'
        and verified_empty_evidence(progress.get('no_results_evidence'),query))


def record_empty_observation(path,progress,query,profile_id):
    if not profile_id or not confirmed_empty_observation(progress,query):raise ValueError('Missing owned empty-search observation')
    path=Path(path)
    try:previous=json.loads(path.read_text('utf-8'))
    except (OSError,ValueError):previous={}
    if not isinstance(previous,dict):previous={}
    previous_checks=previous.get('checks',[])
    if not isinstance(previous_checks,list):previous_checks=[]
    checks=[check for check in previous_checks if isinstance(check,dict)
        and check.get('profile_id') and _valid_observation(check.get('evidence'),query)]
    if not any(check['profile_id']==profile_id for check in checks):
        checks.append({'profile_id':str(profile_id),'evidence':progress['no_results_evidence']})
    evidence={'confirmed':True,'protocol':PROTOCOL,'query':query,'checks':checks}
    atomic_write(path,json.dumps(evidence,ensure_ascii=False,indent=2))
    return evidence if verified_empty_evidence(evidence,query) else None
