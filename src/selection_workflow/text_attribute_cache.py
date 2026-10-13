"""Bounded, process-local memoization without changing the rule engine file."""
from functools import lru_cache, wraps


def cached_attribute_values(original, maxsize=256):
    @lru_cache(maxsize=maxsize)
    def compute(text, aliases, context_functions):
        values, uncertain = original(text, dict(aliases))
        return frozenset(values), uncertain

    @wraps(original)
    def attribute_values(text, aliases):
        # Content and original iteration order matter; mutable dictionaries
        # cannot serve as cache keys. No normalized or shortened text is used.
        items = tuple((name, tuple(phrases)) for name, phrases in aliases.items())
        contexts = tuple(original.__globals__.get(name) for name in (
            "related_product_alias_context", "uncertain_alias_context",
        ))
        values, uncertain = compute(text, items, contexts)
        return set(values), uncertain  # Caller mutations never poison the cache.

    attribute_values.cache_info = compute.cache_info
    attribute_values.cache_clear = compute.cache_clear
    attribute_values._selection_attribute_cache = True
    return attribute_values


def install_attribute_cache():
    """Install once in the current worker; no files or persistent state change."""
    from selection_automation import rules
    if not getattr(rules.attribute_values, "_selection_attribute_cache", False):
        rules.attribute_values = cached_attribute_values(rules.attribute_values)
    return rules.attribute_values
