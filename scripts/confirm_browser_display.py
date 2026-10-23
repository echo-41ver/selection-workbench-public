"""Bounded, ordinary user confirmation for Windows foreground restrictions.

Grant only the live calling binder transient foreground permission. The binder
still verifies the selected browser window before creating a WebDriver session.
"""
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import sys
import threading

DOTNET_FILETIME_OFFSET = 504911232000000000
CONFIRMATION_SECONDS = 60


class NativeParent:
    def __init__(self, pid, started_ticks):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        k.OpenProcess.restype = W.HANDLE
        k.GetProcessTimes.argtypes = [W.HANDLE, *([ctypes.POINTER(W.FILETIME)] * 4)]
        k.GetProcessTimes.restype = W.BOOL
        k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        k.WaitForSingleObject.restype = W.DWORD
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        k.CloseHandle.argtypes = [W.HANDLE]
        self.pid = pid
        self.started_ticks = started_ticks
        self.handle = k.OpenProcess(0x101000, False, pid)
        if not self.handle:
            raise RuntimeError('Calling deployment process is unavailable')
        try:
            self.verify()
            immediate = os.getppid()
            if immediate != pid:
                # Windows venv pythonw.exe may use one redirector process.
                # Only this installation's exact redirector may intervene.
                middle = k.OpenProcess(0x101000, False, immediate)
                if not middle:
                    raise RuntimeError('Calling deployment process is not the parent')
                try:
                    path = ctypes.create_unicode_buffer(32768)
                    size = W.DWORD(len(path))
                    if not k.QueryFullProcessImageNameW(middle, 0, path, ctypes.byref(size)):
                        raise RuntimeError('Python redirector identity is unavailable')
                    expected = Path(__file__).resolve().parents[1] / '.venv/Scripts/pythonw.exe'
                    if Path(path.value).resolve() != expected.resolve() or _parent_pid(immediate) != pid:
                        raise RuntimeError('Calling deployment process is not the parent')
                finally:
                    k.CloseHandle(middle)
        except Exception:
            self.close()
            raise

    def verify(self):
        times = [W.FILETIME() for _ in range(4)]
        if (self.kernel.WaitForSingleObject(self.handle, 0) != 258
                or not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times))):
            raise RuntimeError('Calling deployment process has exited')
        ticks = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime + DOTNET_FILETIME_OFFSET
        if ticks != self.started_ticks:
            raise RuntimeError('Calling deployment process identity changed')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _parent_pid(pid):
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', W.DWORD),
                    ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                    ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(Entry)]
    k.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(Entry)]
    k.CloseHandle.argtypes = [W.HANDLE]
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError('Parent process identity is unavailable')
    try:
        entry = Entry(); entry.dwSize = ctypes.sizeof(entry)
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ProcessID == pid:
                return entry.th32ParentProcessID
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
        raise RuntimeError('Parent process has exited')
    finally:
        k.CloseHandle(snapshot)


def confirm(parent, user, *, timer_factory=threading.Timer):
    deadline = timer_factory(CONFIRMATION_SECONDS, lambda: os._exit(124))
    deadline.daemon = True
    deadline.start()
    try:
        parent.verify()
        answer = user.MessageBoxW(None,
            'Windows 限制了后台窗口切换。\n请点击确定，继续显示本次检查的浏览器。\n选择取消会保留浏览器并停止检查。',
            '选品工作台：继续显示浏览器', 0x00050001)
        if answer != 1:
            return 2
        parent.verify()
        return 0 if user.AllowSetForegroundWindow(parent.pid) else 3
    finally:
        deadline.cancel()


def main():
    if os.name != 'nt' or len(sys.argv) != 3:
        return 4
    parent = None
    try:
        parent = NativeParent(int(sys.argv[1]), int(sys.argv[2]))
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.MessageBoxW.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT]
        user.MessageBoxW.restype = ctypes.c_int
        user.AllowSetForegroundWindow.argtypes = [W.DWORD]
        user.AllowSetForegroundWindow.restype = W.BOOL
        return confirm(parent, user)
    except Exception:
        return 4
    finally:
        if parent:
            parent.close()


if __name__ == '__main__':
    sys.exit(main())
