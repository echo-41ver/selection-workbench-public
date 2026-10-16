"""Find this installation's portable tools in terminals, MCP and background jobs."""
import os
from pathlib import Path


def activate(root=None, environ=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    environ = os.environ if environ is None else environ
    paths = []
    for parent in ('runtime/tools', 'deployment-assets/tools'):
        for tool, binary in (('node', 'node.exe'), ('powershell', 'pwsh.exe'), ('codex', 'codex.exe')):
            folder = root / parent / tool
            if (folder / binary).is_file():
                paths.append(str(folder))
    existing = environ.get('PATH', '').split(os.pathsep)
    seen = {p.casefold() for p in paths}
    environ['PATH'] = os.pathsep.join(paths + [p for p in existing if p.casefold() not in seen])
    environ['TORCH_HOME'] = str(root / 'runtime/models/torch')
    browsers = root / 'deployment-assets/browsers'
    if browsers.is_dir():
        environ['PLAYWRIGHT_BROWSERS_PATH'] = str(browsers)
    return environ
