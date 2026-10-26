import pytest
from selection_automation.profile_single_use import reserve_start


def test_closed_profile_is_never_opened_twice(tmp_path):
    reserve_start(tmp_path, 'fresh', lambda: {})
    with pytest.raises(RuntimeError, match='关闭后禁止'):
        reserve_start(tmp_path, 'fresh', lambda: {'data': {'status': 'Inactive'}}, lambda _: False)


def test_stale_active_flag_does_not_reopen_closed_browser(tmp_path):
    reserve_start(tmp_path, 'fresh', lambda: {})
    with pytest.raises(RuntimeError, match='关闭后禁止'):
        reserve_start(tmp_path, 'fresh', lambda: {'data': {'status': 'Active'}}, lambda _: False)


def test_live_browser_driver_reconnection_does_not_need_another_open(tmp_path):
    reserve_start(tmp_path, 'fresh', lambda: {})
    reserve_start(tmp_path, 'fresh', lambda: {'data': {'status': 'Active'}}, lambda _: True)


def test_closed_used_binding_requires_fresh_replacement(tmp_path):
    from selection_automation.profile_single_use import closed_used_profile
    assert not closed_used_profile(tmp_path, 'profile1', lambda _: False)
    reserve_start(tmp_path, 'profile1', lambda: {})
    assert closed_used_profile(tmp_path, 'profile1', lambda _: False)
    assert not closed_used_profile(tmp_path, 'profile1', lambda _: True)


@pytest.mark.parametrize('arguments', [
    ['--user-data-dir', r'C:\.ADSPOWER_GLOBAL\cache\profile1_machine_flower'],
    ['--user-data-dir', 'C:/cache/profile1_machine'],
    ['--user-data-dir=C:/cache/profile1'],
])
def test_running_profile_recognizes_separate_and_equals_arguments_without_matching_other_ids(monkeypatch, arguments):
    from types import SimpleNamespace
    from selection_automation import profile_single_use as profiles
    monkeypatch.setattr(profiles.psutil, 'process_iter', lambda _fields: [
        SimpleNamespace(info={'name': 'flowerbrowser.exe', 'cmdline': ['flowerbrowser.exe', *arguments]})])
    assert profiles.running_profile('profile1')
    assert not profiles.running_profile('profile')
    assert not profiles.running_profile('profile2')
