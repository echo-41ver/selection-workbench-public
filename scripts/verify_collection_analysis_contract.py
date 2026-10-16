"""Offline acceptance of real collection inputs; never downloads or runs vision.

Writes only a new acceptance directory and an optional report JSON. This does
not attach analysis to a running task or edit its data/configuration.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]

from selection_workflow.incremental_stage12 import (
    ImageSourceMissing, IncrementalStage12, PathSpec, SourceSpec,
)
from selection_automation.commercial import History, sales_evidence
from export_category_workbooks import export_temu_book
import openpyxl


class NoVision:
    revision = 'offline-contract-no-vision-v1'

    def encode(self, *args):
        raise AssertionError('Offline acceptance must not run image/vector analysis')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def verify(base: Path, destination: Path):
    base, destination = base.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError('Use a new output directory; existing acceptance evidence is preserved')
    if destination == base or base in destination.parents:
        raise ValueError('Acceptance output must be separate from the production run')
    destination.mkdir(parents=True)
    frontend = base / 'temu/union/products.jsonl'
    amazon = base / 'amazon/amazon_frontend_latest.jsonl'
    provider = base / 'market/geekbi.json'
    category = read(base / 'category.json')
    inputs = [frontend, amazon, provider, base / 'category.json']
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    consumer_files = [ROOT / 'src/selection_workflow/incremental_stage12.py',
                      ROOT / 'src/selection_automation/commercial.py',
                      ROOT / 'scripts/export_category_workbooks.py']
    consumer_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in consumer_files}
    rows = jsonl(frontend)
    expected = {str(row['goods_id']): row for row in rows}
    envelope = read(provider)
    provider_rows = envelope.get('records') or []
    if envelope.get('source') != 'GEEKBI' or not provider_rows:
        raise ValueError('A saved real GEEKBI response with product records is required')
    provider_input = destination / 'geekbi_existing_records.jsonl'
    provenance = {key: envelope.get(key) for key in ('source', 'status', 'queried_at', 'site_id', 'category_key')}
    provider_input.write_text(''.join(json.dumps({**row, '_source_envelope': provenance}, ensure_ascii=False)+'\n'
                                      for row in provider_rows), encoding='utf-8')
    sources = [SourceSpec('amazon_existing_frontend', 'AMAZON', 'amazon_generic', amazon, 'replace'),
               SourceSpec('temu_existing_frontend', 'TEMU', 'temu_frontend', frontend, 'replace'),
               SourceSpec('temu_existing_geekbi', 'TEMU', 'temu_geekbi', provider_input, 'replace')]
    paths = [PathSpec('amazon_x_temu_frontend', sources[0].key, sources[1].key, 'Existing frontend input'),
             PathSpec('amazon_x_temu_geekbi', sources[0].key, sources[2].key, 'Saved GEEKBI sample only')]
    engine = IncrementalStage12(destination / 'stage12', vectorizer=NoVision())
    try:
        engine.configure(sources, paths)
        ingestion = {source.key: engine.ingest_source(source.key) for source in sources}
        products = [engine._product(sources[1].key, identity) for identity in expected]
        for product in products:
            row = expected[product['product_id']]
            assert product['raw_record'] == row
            assert product['title'] == row['title'].strip()
            assert product['product_url'] == row['product_url'].strip()
            assert product['main_image_url'] == row['main_image_url'].strip()
            assert product['adapter'] == 'temu_frontend'
        raw_products = [product['raw_record'] for product in products]
        provider_ids = {str(row['goodsId']) for row in provider_rows}
        for identity in provider_ids:
            product = engine._product(sources[2].key, identity)
            assert product['adapter'] == 'temu_geekbi'
            assert product['raw_record']['_source_envelope'] == provenance
        pair_counts = {row[0]: row[1] for row in engine.con.execute('SELECT path_key,count(*) FROM pairs GROUP BY path_key')}
        amazon_count = len({row['asin'] for row in jsonl(amazon)})
        assert pair_counts[paths[0].key] == amazon_count * len(expected)
        assert pair_counts[paths[1].key] == amazon_count * len(provider_ids)
        overlap = sorted(set(expected) & provider_ids)
        for identity in overlap:
            a, b = engine._product(sources[1].key, identity), engine._product(sources[2].key, identity)
            assert a['source_key'] != b['source_key'] and a['raw_record'] != b['raw_record']
        missing_image = {**products[0], 'main_image_url': None, 'local_image_path': None}
        try:
            engine._image_identity(missing_image)
        except ImageSourceMissing:
            missing_image_handling = 'ImageSourceMissing -> WAIT_IMAGE_SOURCE; no different-product verdict'
        else:
            raise AssertionError('Missing image was silently accepted')
    finally:
        engine.close()

    history = History(destination / 'market_history.sqlite3')
    try:
        inserted = history.ingest(category, raw_products)
        consumed = {row['goods_id']: row for row in history.rows(category)}
        assert set(consumed) == set(expected)
        for identity, row in expected.items():
            assert consumed[identity]['price_usd'] == row.get('price_usd')
            assert consumed[identity]['sales_text'] == row.get('sales_text')
            assert consumed[identity]['source'] == row.get('source')
            assert consumed[identity]['sales'] == sales_evidence(row)
        missing_sales = [row for row in raw_products if not row.get('sales_text')]
        assert all(sales_evidence(row)['value'] is None and sales_evidence(row)['lower'] is None for row in missing_sales)
    finally:
        history.close()

    delivered = export_temu_book(destination / 'TEMU_contract.xlsx', raw_products,
                                 category['category_name_zh'], category['temu']['max_products_per_keyword'],
                                 {'unique_goods_id': len(raw_products)})
    workbook_path = Path(delivered['数据源工作簿'])
    book = openpyxl.load_workbook(workbook_path, data_only=True)
    try:
        exported = list(book['商品明细'].iter_rows(min_row=5))
        assert len(exported) == len(expected)
        for workbook_cells in exported:
            cells = [cell.value for cell in workbook_cells]
            row = expected[str(cells[1])]
            assert cells[3] == row.get('price_usd')
            assert (cells[4] or None) == (row.get('sales_text') or None)
            # The workbook intentionally shows short labels; verify URL targets.
            assert workbook_cells[5].hyperlink.target == row['product_url']
            assert workbook_cells[6].hyperlink.target == row['main_image_url']
            assert cells[11] == row.get('source')
    finally:
        book.close()
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    assert consumer_hashes == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in consumer_files}
    result = {
        'schema_version': 'collection-analysis-offline-acceptance-v1', 'status': 'PASS',
        'run_key': base.name, 'input_sha256': before,
        'consumer_code_sha256': consumer_hashes,
        'frontend_products': len(expected), 'geekbi_saved_records': len(provider_rows),
        'geekbi_unique_products': len(provider_ids), 'shared_goods_ids': overlap,
        'source_specs': [{**source.__dict__, 'path': str(source.path)} for source in sources],
        'paths': [path.__dict__ for path in paths], 'pair_counts': pair_counts, 'ingestion': ingestion,
        'six_field_mapping': {'goods_id': 'product_id', 'title': 'title (strip whitespace)',
            'product_url': 'product_url', 'main_image_url': 'main_image_url',
            'price_usd': 'raw_record.price_usd', 'sales_text': 'raw_record.sales_text'},
        'market_consumer': {'implementation': 'selection_automation.commercial.History.ingest/rows',
            'verified_rows': inserted, 'preserved_price_sales_source': True, 'missing_sales_unknown': len(missing_sales)},
        'excel_consumer': {'implementation': 'scripts/export_category_workbooks.py:export_temu_book',
            'verified_rows': len(exported), 'workbook': str(workbook_path), 'price_sales_source_equal': True},
        'missing_image_handling': missing_image_handling,
        'provenance_preserved': ['raw_record.source', 'raw_record.source_url', 'raw_record.source_queries',
                                 'raw_record.search_sort', 'raw_record.observation_sources', 'source_key', 'adapter'],
        'production_inputs_unchanged': True, 'image_or_vector_analysis_executed': False,
        'new_collection_or_paid_requests_executed': False,
        'scope_limits': ['No image downloading, vector scoring, layer-3 accuracy or long-run stability acceptance.',
            'GEEKBI records are a saved market sample; this verifies offline ingestion/isolation, not a deployed continuous GEEKBI matching pipeline.',
            'Production market/Excel consumers read the raw collection JSONL; normalized-product consumers must unwrap raw_record for price and sales.'],
    }
    save(destination / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = verify(args.run, args.output)
    if args.report:
        save(args.report, result)
    print(json.dumps({'status': result['status'], 'frontend': result['frontend_products'],
                      'geekbi': result['geekbi_unique_products'], 'pairs': result['pair_counts'],
                      'missing_sales': result['market_consumer']['missing_sales_unknown']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
