"""Reload one published installation only after its console and jobs are idle."""
import time
from .runtime_audit import disk_version


def idle_for_reload(state, console):
    generation=state.get('generation')
    stage3=state.get('stage3_control')
    return bool(
        state.get('ok') is True and state.get('active') is False
        and state.get('console_busy') is False and state.get('market_busy') is False
        and isinstance(generation,dict) and not generation.get('busy')
        and generation.get('stage') in (None,'done','complete','completed','error','idle')
        and isinstance(stage3,dict) and stage3.get('running') is False
        and not (stage3.get('codex_review') or {}).get('running')
        and not console.busy and not console.market_busy and not console.stage3_busy
        and not console.owner()
        and not console._stage3_refreshing and not console._frontend_refreshing
        and not console._codex_review_refreshing
    )


class ReleaseReload:
    def __init__(self,root,loaded,read_version=disk_version,clock=time.monotonic):
        self.root=root;self.loaded=loaded;self.read_version=read_version;self.clock=clock
        self.last_check=None;self.changed_at=None;self.disk=None;self.error=False
        self.restarting=False;self.waiting=False

    def check(self,force=False):
        now=self.clock()
        if not force and self.last_check is not None and now-self.last_check<3:
            return self.snapshot()
        self.last_check=now
        try:
            disk=self.read_version(self.root)
            if disk!=self.disk:self.changed_at=now
            self.disk=disk;self.error=False
        except (OSError,ValueError):
            self.error=True
        return self.snapshot()

    def snapshot(self):
        pending=self.error or self.restarting or bool(self.disk and any(
            self.disk.get(key)!=self.loaded.get(key) for key in ('commit','source_sha256')))
        target=(self.disk or {}).get('commit')
        label='新版 '+target[:7] if target else '新版'
        message=('无法确认发布目录版本，暂不能启动新任务' if self.error else
                 '正在加载最新发布版本，稍后自动恢复连接' if self.restarting else
                 '已发现'+label+'，当前任务结束后自动更新；新任务暂不可启动' if pending and self.waiting else
                 '已发现'+label+'，正在等待空闲并自动更新；新任务暂不可启动' if pending else
                 '已连接当前发布目录的最新版本')
        return {'pending':pending,'restarting':self.restarting,'disk_runtime':self.disk,
                'message':message,'installation_root':str(self.root)}

    def stable(self):
        return (not self.error and self.changed_at is not None
                and self.clock()-self.changed_at>=5)
