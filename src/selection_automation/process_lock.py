"""OS-backed process lock: no stale PID-file recovery races."""
import os
from pathlib import Path


class ProcessLock:
    def __init__(self,path): self.path=Path(path)
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        try:
            if self.path.stat().st_size==0: self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close(); raise RuntimeError('Another supervisor owns this installation')
        return self
    def __exit__(self,*args): self.file.close()
