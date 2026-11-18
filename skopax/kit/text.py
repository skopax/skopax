from skopax.redact import redact


def duration(seconds: float) -> str:
    s = round(seconds or 0)
    return f'{s} s' if s < 120 else f'{s // 60} min' if s < 7200 else f'{s // 3600} h {s % 3600 // 60} min'


def cut(text: str, limit: int) -> str:
    return redact(' '.join(str(text or '').split()))[:limit]
