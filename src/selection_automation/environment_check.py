"""Read-only machine inventory; no credentials or network traffic included."""
import ctypes
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys


def file_version(path):
    if os.name!='nt' or not path: return None
    try:
        api=ctypes.windll.version;unused=ctypes.c_ulong()
        size=api.GetFileVersionInfoSizeW(str(path),ctypes.byref(unused))
        if not size: return None
        buffer=ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path),0,size,buffer): return None
        pointer=ctypes.c_void_p();length=ctypes.c_uint()
        if not api.VerQueryValueW(buffer,'\\',ctypes.byref(pointer),ctypes.byref(length)): return None
        info=ctypes.cast(pointer,ctypes.POINTER(ctypes.c_uint32))
        ms,ls=info[4],info[5]  # ProductVersion MS/LS in VS_FIXEDFILEINFO.
        return '.'.join(map(str,[ms>>16,ms&65535,ls>>16,ls&65535]))
    except (OSError,ValueError): return None


def command_version(args):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=10)
        return result.stdout.strip().lstrip('v') if result.returncode==0 else None
    except (OSError,subprocess.SubprocessError): return None


def compare(inventory, baseline):
    missing=[];warnings=[]
    if inventory.get('platform')!=baseline['platform']: missing.append('当前一键安装仅支持 Windows')
    if '.'.join(str(inventory.get('python','')).split('.')[:2])!=baseline['python_minor']: missing.append('Python 版本与安装基线不一致')
    try: node_major=int(str(inventory.get('node')).split('.')[0])
    except ValueError: node_major=0
    if node_major<baseline['node_min_major']: missing.append('Node.js 版本过低或未找到')
    if inventory.get('adspower') not in baseline['adspower_compatible_versions']:
        missing.append('AdsPower 版本未通过当前发布包兼容验收：'+str(inventory.get('adspower') or '无法读取')+'；请使用基线版本 '+', '.join(baseline['adspower_compatible_versions']))
    for component in ('node','powershell'):
        if inventory.get(component)!=baseline['observed_reference'][component]: warnings.append(component+' 与参考电脑版本不同，需通过部署自检')
    return {'compatible':not missing,'missing':missing,'warnings':warnings,'inventory':inventory,
            'baseline':baseline['observed_reference'],'scope':baseline['scope']}


def inspect_environment(root, adspower_path=None):
    baseline=json.loads((Path(root)/'config/runtime_baseline.json').read_text('utf-8'))
    inventory={'platform':platform.system(),'os_version':platform.version(),'architecture':platform.machine(),
        'python':platform.python_version(),'node':command_version([shutil.which('node') or 'node','--version']),
        'powershell':command_version([shutil.which('pwsh') or 'pwsh','-NoProfile','-Command','$PSVersionTable.PSVersion.ToString()']),
        'adspower':file_version(adspower_path)}
    return compare(inventory,baseline)
