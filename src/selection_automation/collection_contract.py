"""Part-one contract. Does not import or initialize matching/vision code."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def collection_only(category):
    return category.get('execution_mode') == 'collection'


def validate_collection(category):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', category.get('category_key', '')):
        raise ValueError('invalid category_key')
    name = category.get('category_name_zh', '')
    if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name in ('.', '..'):
        raise ValueError('invalid category display name')
    amazon, temu = category['amazon'], category['temu']
    url = urlsplit(amazon.get('bestsellers_url', ''))
    if url.scheme != 'https' or url.netloc != 'www.amazon.com' or '/zgbs/' not in url.path:
        raise ValueError('official Amazon US Best Sellers URL required')
    if type(amazon.get('target_count')) is not int or not 1 <= amazon['target_count'] <= 100:
        raise ValueError('Amazon target must be 1..100')
    if type(temu.get('max_products_per_keyword')) is not int or temu['max_products_per_keyword'] < 1:
        raise ValueError('TEMU target must be positive')
    if not temu.get('base_queries') or not all(isinstance(q, str) and q.strip() for q in temu['base_queries']):
        raise ValueError('TEMU base queries required')
    # Legacy keyword_limit is accepted but no longer constrains collection.
    if temu.get('merge_key') != 'goods_id':
        raise ValueError('TEMU snapshots must merge by goods_id')
    from .workers import category_worker_count
    category_worker_count(category)


def source_completion(base, category):
    """Only source evidence counts; no analysis DB is required."""
    from watch_temu_union import snapshot_signature
    try:
        amazon = json.loads((base/'amazon/amazon_frontend_latest.json').read_text('utf-8'))
        rows = amazon['records']
        target = category['amazon']['target_count']
        if (len(rows) != target or len({r['asin'] for r in rows}) != target
                or sorted(r['rank'] for r in rows) != list(range(1, target + 1))):
            return None
        snapshots = sorted((base/'temu/workers').glob('*/*/products.jsonl'))
        state = json.loads((base/'temu/union/watch_state.json').read_text('utf-8'))
        if not snapshots or state.get('status') != 'PUBLISHED' or state.get('signature') != snapshot_signature(snapshots):
            return None
        if not (base/'temu/union/products.jsonl').is_file():
            return None
        return {'Amazon商品数': len(rows), 'TEMU商品数': state['unique_goods'],
                '缺失或失败': len(amazon.get('missing_details', [])) or int(amazon.get('status') != 'COMPLETE')}
    except (OSError, ValueError, KeyError, TypeError):
        return None
