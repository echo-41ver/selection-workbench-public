"""Paid queue admission is separate from the retained local recall checkpoint."""
import math


def concurrency(stage):
    value = stage.get('max_concurrency', 4)
    if type(value) is not int or value < 1:
        raise ValueError('stage3.max_concurrency must be a positive integer')
    return value


def threshold(category):
    value = category.get('stage3', {}).get('candidate_admission_threshold')
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or
                              not category['stage2']['candidate_threshold'] <= value <= 100):
        raise ValueError('third-layer admission threshold must be finite and not below local recall threshold')
    return value


def admitted(category, pair):
    value = threshold(category)
    if value is None:
        return True
    score = pair.get('vector_score')
    return type(score) in (int, float) and math.isfinite(score) and score >= value
