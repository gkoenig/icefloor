"""Small formatting helpers shared by the views."""

from __future__ import annotations

import datetime as _dt
from typing import Any

from rich.text import Text

# Partial-block glyphs let a bar grow in 1/8th-cell steps.
_BLOCKS = "▏▎▍▌▋▊▉█"

BAR_COLORS = ("cyan", "green", "yellow", "magenta", "blue", "red")


def bar(value: float, maximum: float, width: int = 18, color: str = "cyan") -> Text:
    """Horizontal bar of `width` cells representing value/maximum."""
    if maximum <= 0 or value <= 0:
        return Text("─" * 1, style="dim")
    frac = min(value / maximum, 1.0)
    cells = frac * width
    full = int(cells)
    out = "█" * full
    remainder = cells - full
    if full < width and remainder > 0:
        out += _BLOCKS[min(int(remainder * 8), 7)]
    return Text(out.ljust(width), style=color)


def pct(value: float, total: float) -> str:
    if not total:
        return "–"
    return f"{value / total * 100:.1f}%"


def human_bytes(n: Any) -> str:
    if n is None:
        return "–"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def human_count(n: Any) -> str:
    if n is None:
        return "–"
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def ident(value: Any) -> str:
    """Identifiers (snapshot ids, UUIDs) print bare -- no digit grouping."""
    return "–" if value is None else str(value)


def ts_ms(ms: Any) -> str:
    """Iceberg timestamps are epoch milliseconds."""
    if ms is None:
        return "–"
    if isinstance(ms, _dt.datetime):
        return ms.replace(microsecond=0).isoformat(sep=" ")
    try:
        return (
            _dt.datetime.fromtimestamp(int(ms) / 1000, tz=_dt.UTC)
            .replace(tzinfo=None)
            .isoformat(sep=" ", timespec="seconds")
        )
    except (TypeError, ValueError, OSError):
        return str(ms)


def short(value: Any, limit: int = 60) -> str:
    """Stringify, keeping the tail of long paths (that is the useful end)."""
    if value is None:
        return "–"
    s = str(value)
    if len(s) <= limit:
        return s
    return "…" + s[-(limit - 1) :]


def head(value: Any, limit: int = 40) -> str:
    """Truncate from the right -- for strings whose start carries the meaning."""
    if value is None:
        return "–"
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def scalar(value: Any) -> str:
    """Best-effort one-line rendering of a value pulled out of Arrow/Avro."""
    if value is None:
        return "–"
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()[:32]
    if isinstance(value, float):
        return f"{value:,.6g}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, dict):
        return ", ".join(f"{k}={scalar(v)}" for k, v in value.items()) or "–"
    if isinstance(value, (list, tuple)):
        return ", ".join(scalar(v) for v in value) or "–"
    return str(value)
