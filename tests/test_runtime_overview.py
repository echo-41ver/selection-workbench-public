import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from runtime_overview import stage3_display


def test_confirmed_stop_acknowledges_old_failure_without_deleting_it(tmp_path):
    import json
    import os
    from runtime_overview import stopped_state
    folder=tmp_path/'stage123';folder.mkdir()
    controller={'error':True,'phase':'分析暂停，请查看错误记录'}
    (folder/'controller.json').write_text(json.dumps(controller),encoding='utf-8')
    (tmp_path/'status.json').write_text(json.dumps({'状态':'已停止'}),encoding='utf-8')
    (tmp_path/'STOP').write_text('用户请求停止',encoding='utf-8')
    assert stopped_state(tmp_path,{})=={'task_stopped':True,'stage3_stopped':True}
    assert not stopped_state(tmp_path,{},task_running=True)['task_stopped']
    assert not stopped_state(tmp_path,{},stage3_running=True)['stage3_stopped']
    row=stage3_display({**controller,'stopped':True}, {}, {'mode':'local_model'})
    assert '已停止' in row['状态'] and not row.get('需人工介入')
    assert json.loads((folder/'controller.json').read_text(encoding='utf-8'))==controller
    # A separate restart of layer three must not inherit the old task stop.
    (folder/'paid_enabled.json').write_text('{}')
    stopped_at=(tmp_path/'STOP').stat().st_mtime_ns
    os.utime(folder/'paid_enabled.json',ns=(stopped_at+1_000_000,stopped_at+1_000_000))
    assert not stopped_state(tmp_path,{})['task_stopped']


def test_stop_failures_and_automatic_switches_are_not_acknowledged(tmp_path):
    import json
    from runtime_overview import stopped_state
    folder=tmp_path/'stage123';folder.mkdir()
    (tmp_path/'STOP').write_text('用户请求停止',encoding='utf-8')
    (tmp_path/'status.json').write_text(json.dumps({'状态':'启动或运行失败'}),encoding='utf-8')
    (folder/'STOP').write_text(json.dumps({'reason':'provider-switch'}),encoding='utf-8')
    assert not any(stopped_state(tmp_path,{}).values())
    (folder/'STOP').write_text(json.dumps({'reason':'user-request'}),encoding='utf-8')
    assert stopped_state(tmp_path,{})['stage3_stopped']
    assert not stopped_state(tmp_path,{},stage3_running=True)['stage3_stopped']


def test_stopped_agent_block_is_historical_until_analysis_restarts():
    runtime={'agent_blocks':{'doubao':{'message':'请完成验证码'}}}
    control={'stopped':True,'error':True,'codex_review':{'running':False}}
    assert not stage3_display(control,runtime,{'agent_ids':['doubao']}).get('需人工介入')
    control['codex_review']['running']=True
    assert stage3_display(control,runtime,{'agent_ids':['doubao']})['需人工介入']


def test_native_agent_progress_overrides_legacy_api_idle_message():
    row=stage3_display({'enabled':True,'running':False,'phase':'等待手动启动',
        'progress':{'api_completed':0,'completed':1419,'codex_completed':1419,
                    'total':6056,'pending_after_codex':4637},
        'codex_review':{'running':True,'pool':{'phase':'并行看图并自动接续下一组'}}},
        {},{'mode':'local_agent','agent_ids':['doubao']})
    assert row['running'] and row['completed']==1419 and row['remaining']==4637
    assert '等待' not in row['状态'] and 'API' not in row['状态']


def test_selected_agent_verification_is_red_even_if_pool_is_running():
    runtime={'agent_blocks':{'doubao':{'code':'HUMAN_VERIFICATION_REQUIRED','message':'请完成验证码'}}}
    row=stage3_display({'codex_review':{'running':True}},runtime,{'agent_ids':['doubao']})
    assert row['需人工介入'] and row['阻断'] and '验证码' in row['状态']
    row=stage3_display({'running':True},runtime,{'agent_ids':['codex']})
    assert row['running'] and not row.get('需人工介入')


def test_user_pause_is_distinct_from_human_block():
    row=stage3_display({'codex_review':{'running':True}},
        {'paused_agents':['doubao']},{'mode':'local_agent','agent_ids':['doubao']})
    assert '已暂停' in row['状态'] and not row.get('需人工介入')


def test_unknown_billing_is_held_and_never_presented_as_ready():
    row=stage3_display({'progress':{'billing_unknown_pairs':12}}, {}, {})
    assert row['需人工介入'] and '不会自动重发' in row['建议动作']


def test_local_model_and_api_ignore_stopped_agent_state_and_show_actual_inflight_groups():
    control={'running':True,'phase':'分析中','progress':{'inflight_requests':2},
             'codex_review':{'running':False,'phase':'旧复核已暂停',
                             'pool':{'running':False,'phase':'已停止，结果保留','running_groups':0}}}
    for mode in ('local_model','openai_compatible'):
        row=stage3_display(control,{}, {'mode':mode})
        assert row['状态']=='分析中' and row['running'] and row['并发组数']==2
        control['codex_review']['running']=True
        assert stage3_display(control,{}, {'mode':mode})['状态']=='分析中'


def test_unselected_agent_block_does_not_override_local_model():
    row=stage3_display({'running':True,'phase':'分析中'},
        {'agent_blocks':{'doubao':{'message':'请完成验证码'}}},
        {'mode':'local_model','agent_ids':['doubao']})
    assert row['状态']=='分析中' and not row.get('阻断')


def test_current_controller_failure_is_red_and_running_recovery_clears_it():
    control={'running':False,'error':True,'phase':'本地模型连续返回不合格结果，已暂停'}
    row=stage3_display(control,{}, {'mode':'local_model'})
    assert row['需人工介入'] and row['阻断'] and '不合格' in row['状态']
    control.update(running=True,phase='分析中')
    assert not stage3_display(control,{}, {'mode':'local_model'}).get('需人工介入')


def test_local_activity_uses_only_current_count_receipts_and_ignores_partial_tail(tmp_path):
    import json
    from runtime_overview import local_stream_activity
    path=tmp_path/'local_stream_progress.jsonl'
    path.write_text('x'*20000+'\n'+json.dumps({'request_id':'old','time':50,'status':'receiving','received_chars':9})+'\n'+
        json.dumps({'request_id':'current','time':100,'status':'receiving','received_chars':1234,'output_text':'DO NOT EXPOSE'})+'\n{"partial":',encoding='utf-8')
    assert local_stream_activity(tmp_path,90,120)=={'request_id':'current','updated_at':100,'status':'receiving','received_chars':1234}
    assert local_stream_activity(tmp_path,101,120)=={}
    path.write_text(json.dumps({'request_id':'future','time':200,'status':'receiving','received_chars':1}),encoding='utf-8')
    assert local_stream_activity(tmp_path,90,120)=={}


def test_malformed_activity_never_breaks_monitor_or_looks_like_real_output(tmp_path):
    import json
    from runtime_overview import local_stream_activity
    path=tmp_path/'local_stream_progress.jsonl'
    for value in ([],{'time':100,'request_id':'r','received_chars':True,'status':'receiving'},
                  {'time':100,'request_id':'r','received_chars':-1,'status':'receiving'}):
        path.write_text(json.dumps(value),encoding='utf-8')
        assert local_stream_activity(tmp_path,90,120)=={}
