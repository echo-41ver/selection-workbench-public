"""Current-user Windows DPAPI. No portable ciphertext or embedded company keys."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def transform(data, encrypt=True):
    if os.name!='nt': raise RuntimeError('Company credential storage requires Windows')
    raw=ctypes.create_string_buffer(data); source=Blob(len(data),ctypes.cast(raw,ctypes.POINTER(ctypes.c_ubyte))); target=Blob()
    dll=ctypes.windll.crypt32
    func=dll.CryptProtectData if encrypt else dll.CryptUnprotectData
    if not func(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)): raise ctypes.WinError()
    try: return ctypes.string_at(target.data,target.size)
    finally: ctypes.windll.kernel32.LocalFree(target.data)


def store(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp'); temp.write_bytes(transform(json.dumps(value).encode())); os.replace(temp,path)


def load(path): return json.loads(transform(Path(path).read_bytes(),False))
