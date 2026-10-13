"""Strict records with opt-in, evidenced append-log EOF tolerance."""
import json

def read_jsonl(path, *, allow_incomplete_tail=False, warnings=None):
    """Only append-only display logs may omit an unfinished physical EOF line.

    Frozen inputs and newline-terminated/middle corruption remain strict.
    The source is never repaired or rewritten by an exporter.
    """
    if not path.exists():
        return
    with path.open('rb') as stream:
        line = stream.readline()
        number = 0
        while line:
            number += 1
            following = stream.readline()
            if line.strip():
                try:
                    row = json.loads(line.decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    if allow_incomplete_tail and not following and not line.endswith(b'\n'):
                        if warnings is not None:
                            warnings.append({'file': path.name, 'line': number})
                    else:
                        raise
                else:
                    if not isinstance(row, dict):
                        raise ValueError('JSONL record must be an object')
                    yield row
            line = following

