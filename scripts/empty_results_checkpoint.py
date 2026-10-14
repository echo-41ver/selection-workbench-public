"""Shared evidence contract for a confirmed empty search across both sort passes."""
from urllib.parse import urlsplit, parse_qs


def confirmed_empty_checkpoint(progress, query):
    evidence = progress.get('no_results_evidence') or {}
    try:
        url = urlsplit(evidence.get('url', ''))
        url_query = parse_qs(url.query).get('search_key', [''])[0]
        return (progress.get('status') == 'KEYWORD_NO_RESULTS' and progress.get('unique_goods') == 0
            and progress.get('manual_action_required') is not True
            and progress.get('completion_basis') == 'EXPLICIT_EMPTY_QUERY_RESULTS' and evidence.get('confirmed') is True
            and evidence.get('protocol') == 'VISIBLE_EMPTY_RESULTS_STABLE_V2' and evidence.get('loading') is False
            and type(evidence.get('stable_ms')) in (int, float) and evidence['stable_ms'] >= 15000
            and type(evidence.get('samples')) is int and evidence['samples'] >= 3
            and evidence.get('query') == query and url.scheme == 'https' and url.hostname in ('temu.com', 'www.temu.com')
            and url.path == '/search_result.html' and url_query.lower().strip() == query.lower().strip())
    except (TypeError, ValueError):
        return False
