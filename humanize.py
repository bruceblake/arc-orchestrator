"""Small humanizing helper: turn a number of seconds into a short string."""


def humanize_seconds(n):
    """Format a number of seconds as a short human string.

    - under 60     -> '45s'
    - under 3600   -> '12m'
    - otherwise    -> '1.5h' (hours, one decimal)
    - None         -> '-'
    - negative     -> ValueError
    """
    if n is None:
        return "-"
    if n < 0:
        raise ValueError("seconds must be non-negative")
    if n < 60:
        return f"{int(n)}s"
    if n < 3600:
        return f"{int(n // 60)}m"
    return f"{n / 3600:.1f}h"
