from collections.abc import Iterable


def header_indices(headers: Iterable[str], required: Iterable[str]) -> dict[str, int]:
    """Return column indexes by header text, independent of column order."""
    positions = {str(value).strip(): index for index, value in enumerate(headers)}
    missing = [name for name in required if name not in positions]
    if missing:
        raise ValueError(f"缺少表头：{', '.join(missing)}")
    return {name: positions[name] for name in required}
