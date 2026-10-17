import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from runtime_overview import stage3_display


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
