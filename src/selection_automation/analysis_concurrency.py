"""Bounded throughput tuning with no speculative or duplicate provider calls."""
import math


def recommended(mode, agents=1):
    # Completed local history: 201 groups at six workers, <=12 pairs per group.
    # D1 has no comparable benchmark yet, so its initial width remains four.
    if mode == 'local_agent':
        import psutil
        memory_cap = max(1, int(psutil.virtual_memory().available / (600 * 1024**2)))
        return min(24, max(1, agents) * 6, memory_cap)
    return 2 if mode == 'local_model' else 4


class ThroughputTuner:
    """Keep the best measured width; stop exploring once gains flatten or fail."""
    def __init__(self, initial=4, maximum=16, state=None):
        self.maximum = maximum
        self.width = min(maximum, max(1, initial))
        self.best_width = self.width
        self.best_rate = 0.0
        self.samples = []
        self.exploring = True
        if state and state.get('best_rate', 0) > 0:
            self.width = self.best_width = min(maximum, max(1, int(state['best_width'])))
            self.best_rate = float(state['best_rate'])

    def observe(self, completed, failed, seconds, saturated=True):
        if not math.isfinite(seconds) or seconds <= 0:
            return self.width
        rate = completed / seconds
        self.samples.append({'workers': self.width, 'completed': completed, 'failed': failed,
                             'seconds': round(seconds, 3), 'pairs_per_second': round(rate, 6)})
        if failed:
            self.width = max(1, min(self.best_width, self.width // 2))
            self.exploring = False
        elif saturated and completed >= self.width * 2:
            if self.best_rate == 0 or rate > self.best_rate * 1.05:
                self.best_rate, self.best_width = rate, self.width
                if self.exploring:
                    self.width = min(self.maximum, self.width * 2)
            else:
                self.width, self.exploring = self.best_width, False
        return self.width

    def snapshot(self):
        return {'workers': self.width, 'best_width': self.best_width,
                'best_rate': self.best_rate, 'samples': self.samples[-20:], 'exploring': self.exploring}
