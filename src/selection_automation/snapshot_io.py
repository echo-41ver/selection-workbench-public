"""Read a private snapshot without holding Windows publication paths open."""
from contextlib import contextmanager
import io
import os
from pathlib import Path
import shutil
import tempfile


@contextmanager
def open_snapshot_text(path, *, encoding='utf-8'):
    """Keep parsing lazy and memory bounded while closing the published input.

    This Windows deployment rejects os.replace of an open destination even
    with delete sharing. Use a private copy for Windows compatibility, following
    the layer-3 consumer's bounded-memory pattern. The producer's brief retry only
    contends with this copy, rather than report computation or image hashing.
    The temporary file is private, auto-deleted on close, and never reused by
    another read. POSIX readers retain their normal open-file snapshot behavior.
    """
    path = Path(path)
    if os.name != 'nt':
        with path.open('r', encoding=encoding) as stream:
            yield stream
        return
    with tempfile.TemporaryFile(mode='w+b') as snapshot:
        with path.open('rb') as source:
            shutil.copyfileobj(source, snapshot, length=1024 * 1024)
        snapshot.seek(0)
        with io.TextIOWrapper(snapshot, encoding=encoding) as stream:
            yield stream
