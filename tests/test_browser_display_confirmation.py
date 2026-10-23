import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/confirm_browser_display.py'
spec = importlib.util.spec_from_file_location('browser_display_confirmation', SCRIPT)
confirmation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(confirmation)


class Parent:
    pid = 8123
    def __init__(self, changed=False):
        self.checks = 0
        self.changed = changed
    def verify(self):
        self.checks += 1
        if self.changed and self.checks == 2:
            raise RuntimeError('Calling deployment process has exited')


class User:
    def __init__(self, answer=1, granted=True):
        self.answer = answer
        self.granted = granted
        self.recipients = []
    def MessageBoxW(self, owner, message, title, flags):
        assert owner is None and '取消' in message and '浏览器' in title
        return self.answer
    def AllowSetForegroundWindow(self, pid):
        self.recipients.append(pid)
        return self.granted


class Timer:
    def __init__(self, seconds, callback):
        assert seconds == 60
        self.started = self.cancelled = False
    def start(self):
        self.started = True
    def cancel(self):
        self.cancelled = True


@pytest.mark.parametrize('answer,granted,expected,recipients', [
    (1, True, 0, [8123]), (2, True, 2, []), (1, False, 3, [8123]),
])
def test_confirmation_requires_real_ok_and_grants_only_live_caller(answer, granted, expected, recipients):
    parent, user, timers = Parent(), User(answer, granted), []
    def timer_factory(seconds, callback):
        timer = Timer(seconds, callback); timers.append(timer); return timer
    assert confirmation.confirm(parent, user, timer_factory=timer_factory) == expected
    assert user.recipients == recipients
    assert parent.checks == (2 if answer == 1 else 1)
    assert len(timers) == 1 and timers[0].started and timers[0].cancelled


def test_caller_exit_during_confirmation_cannot_grant_reused_pid():
    parent, user, timers = Parent(changed=True), User(), []
    def timer_factory(seconds, callback):
        timer = Timer(seconds, callback); timers.append(timer); return timer
    with pytest.raises(RuntimeError, match='has exited'):
        confirmation.confirm(parent, user, timer_factory=timer_factory)
    assert user.recipients == []
    assert timers[0].cancelled


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native process identity')
def test_native_helper_refuses_wrong_creation_time_before_showing_dialog():
    result = subprocess.run([sys.executable, str(SCRIPT), str(__import__('os').getpid()), '1'],
                            capture_output=True, timeout=10)
    assert result.returncode == 4
    assert not result.stdout and not result.stderr
