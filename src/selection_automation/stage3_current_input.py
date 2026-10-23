"""Read-only dispatch admission against current published inputs, not frozen iterators."""
import json
from pathlib import Path
import threading

from selection_workflow.incremental_stage12 import normalize_record


class CurrentInputUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('当前输入暂不可核对，已暂停未派发请求并保留在途结果；请确认前两层输入完整后从网页继续')


class CurrentInputGuard:
    def __init__(self, base, candidates, *, root, required=False):
        self.base = Path(base)
        self.candidates = Path(candidates)
        self.root = Path(root)
        self.required = required
        self._lock = threading.Lock()
        self._cache = {}
        self.skipped = 0

    @staticmethod
    def _signature(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _read(self, path, kind, build):
        signature = self._signature(path)
        self._checked[path] = signature
        prior = self._cache.get((path, kind))
        if prior is not None and prior[0] == signature:
            return prior[1]
        data = path.read_text(encoding='utf-8-sig')
        value = build(data)
        if self._signature(path) != signature:
            raise CurrentInputUnavailable()
        self._cache[(path, kind)] = signature, value
        return value

    @staticmethod
    def _rows(data):
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise CurrentInputUnavailable()
        return [row for row in rows if row.get('_recordType') not in ('metadata', 'category_validation')]

    def _source_version(self, source, product_id):
        path = Path(source['path'])
        if not path.is_absolute(): path = self.root / path
        def build(data):
            by_id = {}
            for row in self._rows(data):
                facts = normalize_record(row, platform=source['platform'], source_key=source['key'],
                                         adapter=source['adapter'], source_path=path)
                # Match the stage 1/2 snapshot's last-record-wins identity rule.
                by_id[facts['product_id']] = ((facts['evidence_hash'], row)
                    if source['adapter'] == 'amazon_facts' else facts['evidence_hash'])
            return by_id
        # Include adapter/source identity when a config republishes the same path.
        kind = ('source', source['key'], source['platform'], source['adapter'])
        value = self._read(path, kind, build).get(product_id)
        if value is not None and source['adapter'] == 'amazon_facts':
            # This adapter can discover a newly downloaded local image without
            # the source JSONL changing; preserve its normalizer's semantics.
            return normalize_record(value[1], platform=source['platform'], source_key=source['key'],
                                    adapter=source['adapter'], source_path=path)['evidence_hash']
        return value

    def allows(self, batch):
        """An unchanged semantic record survives timestamp-only republication."""
        with self._lock:
            try:
                self._checked = {}
                current = self._read(self.candidates, 'candidates', self._rows)
                revisions = {}
                for row in current:
                    identity = row.get('source_path'), row.get('asin'), row.get('goods_id')
                    revision = row.get('pair_revision')
                    if not all(isinstance(value, str) and value for value in identity[1:]) or not revision:
                        raise CurrentInputUnavailable()
                    if identity in revisions and revisions[identity] != revision:
                        raise CurrentInputUnavailable()
                    revisions[identity] = revision
                config_path = self.base / 'stage12_config.json'
                if config_path.exists() or self.required:
                    config = self._read(config_path, 'config', json.loads)
                    sources = {row['key']: row for row in config['sources']}
                    paths = {row['key']: row for row in config['paths']}
                    if len(sources) != len(config['sources']) or len(paths) != len(config['paths']):
                        raise CurrentInputUnavailable()
                    if self.required:
                        output = Path(config.get('output_dir') or self.base / 'stage12')
                        if not output.is_absolute(): output = self.base / output
                        expected = output / f'layer3_candidates_{self.base.name}.jsonl'
                        if (not output.resolve().is_relative_to(self.base.resolve())
                                or self.candidates.resolve() != expected.resolve()):
                            raise CurrentInputUnavailable()
                else:
                    # The standalone --candidates CLI predates managed task
                    # sources; its current published file remains authoritative.
                    sources = paths = None
                allowed = True
                for row in batch:
                    identity = row.get('source_path'), row.get('asin'), row.get('goods_id')
                    if revisions.get(identity) != row.get('pair_revision'):
                        allowed = False
                    if sources is not None:
                        path = paths[row['source_path']]
                        for side, field, platform in (('amazon', 'asin', 'AMAZON'), ('temu', 'goods_id', 'TEMU')):
                            source = sources[path[side + '_source']]
                            version = row.get(side + '_fact_version')
                            if source['platform'] != platform or not isinstance(version, str) or not version:
                                raise CurrentInputUnavailable()
                            if self._source_version(source, row[field]) != version:
                                allowed = False
                # Atomic producer replacements during this multi-file check
                # cannot silently authorize a mixture of input generations.
                if any(self._signature(path) != signature for path, signature in self._checked.items()):
                    raise CurrentInputUnavailable()
                if not allowed:
                    self.skipped += 1
                    if self.skipped == 1:
                        print('当前输入已更新，过期候选未派发；等待前两层发布当前候选后继续', flush=True)
                return allowed
            except CurrentInputUnavailable:
                raise
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise CurrentInputUnavailable() from None
