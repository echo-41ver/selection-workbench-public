"""Stable export inputs from one SQLite read transaction, excluding polling timestamps."""
import hashlib
import pickle
from pathlib import Path
import sqlite3


def database_signature(paths):
    paths=[Path(p) for p in paths]
    if not paths:return hashlib.sha256(b'no-database').hexdigest()
    database=paths[0]
    queries={
        'paths':'SELECT * FROM paths ORDER BY path_key',
        'products':'SELECT source_key,product_id,evidence_hash,record_json FROM products ORDER BY source_key,product_id',
        'pairs':'SELECT path_key,asin,goods_id,pair_revision,stage1_status,stage1_json,vector_score,status,amazon_content_hash,temu_content_hash,last_error,retry_count FROM pairs ORDER BY path_key,asin,goods_id',
        'candidates':'SELECT path_key,asin,goods_id,pair_revision,payload_json FROM candidates ORDER BY path_key,asin,goods_id',
        'images':'SELECT image_key,source_ref,content_hash,local_path,status,retry_count,last_error FROM images ORDER BY image_key',
        'sources':'SELECT source_key,platform,received_records,last_error FROM sources ORDER BY source_key',
        'processing_metadata':'SELECT key,value FROM processing_metadata ORDER BY key',
    }
    try:
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=8) as connection:
            connection.execute('BEGIN')
            tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'products','pairs'}.issubset(tables):raise sqlite3.DatabaseError('Unknown publication schema')
            digest=hashlib.sha256()
            digest.update(str(database).encode())
            for table,query in queries.items():
                if table not in tables:continue
                digest.update((table+'\n').encode())
                for row in connection.execute(query):
                    # Serialize basic SQLite values only; never deserialize pickle.
                    # C serialization avoids re-escaping hundreds of MB of JSON text.
                    digest.update(pickle.dumps(tuple(row),protocol=4))
                    digest.update(b'\n')
            return 'data-v1:'+digest.hexdigest()
    except (sqlite3.Error,ValueError,OSError):
        # Unknown/temporarily unreadable schemas stay conservatively dirty.
        # The real snapshot/export retains responsibility for reporting failure.
        rows=[]
        for path in paths:
            try:
                stat=path.stat();rows.append(f'{path}:{stat.st_mtime_ns}:{stat.st_size}')
            except FileNotFoundError:rows.append(f'{path}:missing')
        return 'file-v1:'+hashlib.sha256('|'.join(rows).encode()).hexdigest()
