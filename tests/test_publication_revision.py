import sqlite3
from pathlib import Path
from selection_automation.publication_revision import database_signature


def make_database(tmp_path):
    path=tmp_path/'stage12.sqlite3'
    with sqlite3.connect(path) as con:
        con.executescript('''
        CREATE TABLE products(source_key TEXT,product_id TEXT,evidence_hash TEXT,record_json TEXT,last_seen TEXT);
        CREATE TABLE pairs(path_key TEXT,asin TEXT,goods_id TEXT,pair_revision TEXT,stage1_status TEXT,stage1_json TEXT,vector_score REAL,status TEXT,amazon_content_hash TEXT,temu_content_hash TEXT,last_error TEXT,retry_count INTEGER,updated_at TEXT);
        CREATE TABLE observations(receive_count INTEGER);
        INSERT INTO products VALUES('temu','1','v1','{"title":"pump"}','old');
        INSERT INTO pairs VALUES('pump','A','1','v1','PASS','{}',85,'CANDIDATE','a','t',NULL,0,'old');
        INSERT INTO observations VALUES(1);
        ''')
    return path


def test_polling_metadata_does_not_invalidate_large_exports(tmp_path):
    path=make_database(tmp_path);before=database_signature([path])
    with sqlite3.connect(path) as con:
        con.execute("UPDATE products SET last_seen='new'")
        con.execute('UPDATE observations SET receive_count=receive_count+1')
    assert database_signature([path])==before


def test_changed_results_and_same_count_product_edits_invalidate_exports(tmp_path):
    path=make_database(tmp_path);before=database_signature([path])
    with sqlite3.connect(path) as con:con.execute("UPDATE pairs SET status='NOT_SAME',last_error='new reason'")
    changed=database_signature([path]);assert changed!=before
    with sqlite3.connect(path) as con:con.execute("UPDATE products SET evidence_hash='v2',record_json='{\"title\":\"different pump\"}'")
    assert database_signature([path])!=changed


def test_unrecognized_database_uses_conservative_file_change_fallback(tmp_path):
    path=tmp_path/'stage12.sqlite3';path.write_bytes(b'unknown database')
    before=database_signature([path]);assert before.startswith('file-v1:')
    path.write_bytes(b'changed unknown database')
    assert database_signature([path])!=before
