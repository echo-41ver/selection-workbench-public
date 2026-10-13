"""Emit local stdio MCP config; do not overwrite any Agent's global settings."""
import json
import shutil
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
out=ROOT/'runtime'/'agent-config'; out.mkdir(parents=True,exist_ok=True)
server={'command':str(ROOT/'.venv'/'Scripts'/'python.exe'),'args':[str(ROOT/'scripts'/'mcp_server.py')],
        'env':{'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}}
(out/'mcp.json').write_text(json.dumps({'mcpServers':{'amazon-temu-selection':server}},ensure_ascii=False,indent=2),encoding='utf-8')
(out/'codex.toml').write_text('[mcp_servers.amazon-temu-selection]\ncommand = '+json.dumps(server['command'])+'\nargs = '+json.dumps(server['args'])+'\n[mcp_servers.amazon-temu-selection.env]\nPYTHONUTF8 = "1"\nPYTHONIOENCODING = "utf-8"\n',encoding='utf-8')
skill=out/'skills'/'amazon-temu-automation'
shutil.copytree(ROOT/'skills'/'amazon-temu-automation',skill,dirs_exist_ok=True)
entry=skill/'SKILL.md'
entry.write_text(entry.read_text(encoding='utf-8').replace('<!-- LOCAL_PROJECT_ROOT -->',
    f'本机项目根目录固定为 `{ROOT.as_posix()}`。即使此Skill被复制到个人skills目录，所有脚本与配置仍必须使用此绝对目录。执行Python时使用 `{(ROOT/".venv/Scripts/python.exe").as_posix()}`；工作目录设为项目根目录。'),encoding='utf-8')
print(str(out))
