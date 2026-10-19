"""Read-only presentation of the actual third-layer controller and its gates."""


def stopped_state(base, runtime, *, task_running=False, stage3_running=False):
    """A confirmed user stop acknowledges old alerts without deleting evidence."""
    import json
    from pathlib import Path
    base = Path(base)
    def read(path):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    stop = base / 'STOP'
    authorization = base / 'stage123/paid_enabled.json'
    try:
        restarted = authorization.is_file() and authorization.stat().st_mtime_ns > stop.stat().st_mtime_ns
        stopped = (stop.is_file() and not task_running and not stage3_running and not restarted
                   and read(base / 'status.json').get('状态') == '已停止')
    except OSError:
        stopped = False
    analysis_stop = base / 'stage123/STOP'
    requested = (analysis_stop.is_file() and
                 (read(analysis_stop).get('reason') == 'user-request' or
                  (runtime.get('paused') and runtime.get('switch_cancelled'))))
    return {'task_stopped': stopped, 'stage3_stopped': bool(not stage3_running and (stopped or requested))}


def stage3_display(control, runtime, provider):
    progress = control.get('progress') or {}
    review = control.get('codex_review') or {}
    pool = review.get('pool') or {}
    agent_mode = provider.get('mode', 'local_agent') == 'local_agent'
    selected = (provider.get('agent_ids') or []) if agent_mode else []
    blocks = [block for agent, block in (runtime.get('agent_blocks') or {}).items()
              if agent in selected]
    completed = int(progress.get('completed') or 0)
    if progress.get('api_completed') is None:
        completed += int(progress.get('codex_completed') or 0)
    total = progress.get('total')
    pending = progress.get('pending_after_codex', progress.get('pending'))
    row = {'completed': completed, 'total': total, 'remaining': pending,
           'scope_label': '当前候选版本；已保存的实际分析结果',
           '分析方式': provider.get('mode'), '动态快照': bool(progress.get('refreshing'))}
    if control.get('stopped') and not control.get('running') and not review.get('running'):
        row['状态'] = '已停止，已完成结果和历史记录已保留'
    elif blocks:
        message = '；'.join(str(block.get('message') or block.get('code') or 'Agent 已阻塞') for block in blocks)
        row.update(状态=message, 阻断=True, 需人工介入=True,
                   建议动作='请在对应 Agent 完成验证码、登录或提示要求，再点击继续此 Agent；不会自动绕过验证。')
    elif runtime.get('switch_error'):
        row.update(状态='第三层切换失败，需要处理', 阻断=True, 需人工介入=True, 建议动作=runtime['switch_error'])
    elif progress.get('billing_hold_fail_closed') or progress.get('billing_unknown_pairs'):
        row.update(状态='第三层有发送或费用记录待核对', 阻断=True, 需人工介入=True,
                   建议动作='已有结果继续保留，状态不明的请求不会自动重发。请核对原会话和费用记录。')
    elif runtime.get('switch_pending'):
        row['状态'] = '正在保存断点并切换分析方案'
    elif not agent_mode and control.get('error') and not control.get('running'):
        row.update(状态=control.get('phase') or '第三层分析异常，已暂停',阻断=True,需人工介入=True,
                   建议动作='原返回与已完成结果已保留；请检查错误提示，更换可用模型或修复服务后继续。')
    elif runtime.get('paused') or (provider.get('mode') == 'local_agent' and selected
                                   and all(agent in runtime.get('paused_agents', []) for agent in selected)):
        row['状态'] = '已暂停第三层派发，已发出的结果继续保存'
    elif control.get('running') or (agent_mode and review.get('running')):
        agent_running = agent_mode and review.get('running')
        row['状态'] = ((pool.get('phase') or review.get('phase')) if agent_running else control.get('phase')) or '第三层分析运行中'
        row['running'] = True
        row['并发组数'] = pool.get('running_groups') if agent_running else progress.get('inflight_requests', 0)
    elif total and pending == 0 and not progress.get('refreshing'):
        row['状态'] = '当前第三层候选已全部处理'
    elif progress.get('counting') or progress.get('refreshing'):
        row['状态'] = '正在核对第三层进度快照'
    else:
        row['状态'] = '等待点击启动第三层分析' if not control.get('enabled') else control.get('phase') or '等待第三层调度'
    return row


def local_stream_activity(folder, since=0, now=None):
    """Read count-only receipts, bounded independently of the result journal."""
    import json,math,time
    from pathlib import Path
    now=time.time() if now is None else now
    try:
        with (Path(folder)/'local_stream_progress.jsonl').open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-8192))
            lines=stream.read(8192).decode('utf-8',errors='replace').splitlines()
    except OSError:return {}
    for line in reversed(lines):
        try:
            row=json.loads(line);at=row['time'];chars=row['received_chars']
            if (type(at) not in (int,float) or not math.isfinite(at) or not since<=at<=now+5
                    or type(chars) is not int or not 0<=chars<=1500000
                    or row['status'] not in ('waiting','receiving','completed','incomplete')
                    or not isinstance(row['request_id'],str) or len(row['request_id'])>128):continue
            return {'request_id':row['request_id'],'status':row['status'],
                    'received_chars':chars,'updated_at':at}
        except (ValueError,KeyError,TypeError):continue
    return {}
