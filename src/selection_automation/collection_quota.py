"""A collection pass succeeds only after its configured raw-card quota."""


def collected_cards(progress):
    # Distinct goods are a lower bound only for older checkpoints without raw counts.
    value=progress.get('raw_collected_count',progress.get('unique_goods',0))
    return value if type(value) is int and value>=0 else 0


def quota_reached(progress,target):
    return type(target) is int and target>0 and collected_cards(progress)>=target
