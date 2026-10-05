"""Payload-free reasons; retry only changes proven to be presentation-only."""

RETRYABLE = frozenset({"viewport", "scroll", "public_frame_geometry"})


def changed(before, after):
    return [key for key in before if before[key] != after[key]]


def may_retry(reasons):
    return bool(reasons) and set(reasons) <= RETRYABLE
