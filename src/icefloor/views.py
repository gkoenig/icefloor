"""Turn `meta.Block`s into Textual widgets."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.table import Table as RichTable
from rich.text import Text
from textual.widget import Widget
from textual.widgets import DataTable, Sparkline, Static

from . import fmt
from .meta import Block, Col


class BlockTable(DataTable):
    """DataTable that remembers which block it came from."""

    def __init__(self, block: Block, **kwargs: Any):
        super().__init__(**kwargs)
        self.block = block
        self.cursor_type = "row"
        self.zebra_stripes = True


def _cell(col: Col, value: Any) -> Text:
    try:
        text = col.fmt(value)
    except Exception:
        text = fmt.scalar(value)
    style = ""
    if value is None:
        style = "dim"
    return Text(text, style=style, justify="right" if col.right else "left")


def _matches(needle: str, cells: Iterable[str]) -> bool:
    return any(needle in c.lower() for c in cells)


def _filter_rows(block: Block, needle: str) -> list[list[Any]]:
    if not needle:
        return block.rows
    needle = needle.lower()
    kept = []
    for row in block.rows:
        rendered = [str(_cell(c, v)) for c, v in zip(block.cols, row, strict=False)]
        if _matches(needle, rendered):
            kept.append(row)
    return kept


def _table_widget(block: Block, needle: str) -> BlockTable:
    rows = _filter_rows(block, needle)
    table = BlockTable(block)
    for i, col in enumerate(block.cols):
        table.add_column(col.name or " ", key=f"c{i}")
        if col.bar:
            table.add_column("", key=f"b{i}")

    # Bars are normalised per column against the largest value on screen, so a
    # filtered view re-scales to what is actually visible.
    maxima: dict[int, float] = {}
    for i, col in enumerate(block.cols):
        if col.bar and not col.frac:
            vals = [r[i] for r in rows if isinstance(r[i], (int, float))]
            maxima[i] = max((abs(v) for v in vals), default=0.0)

    for n, row in enumerate(rows):
        cells: list[Any] = []
        for i, col in enumerate(block.cols):
            value = row[i] if i < len(row) else None
            cells.append(_cell(col, value))
            if col.bar:
                numeric = value if isinstance(value, (int, float)) else 0
                ceiling = 1.0 if col.frac else maxima.get(i, 0.0)
                cells.append(fmt.bar(float(numeric), ceiling, 14, col.bar))
        key = None
        if block.key_col is not None and block.key_col < len(row):
            key = f"{n}|{row[block.key_col]}"
        table.add_row(*cells, key=key)

    if not rows:
        table.add_row(*[Text("no matching rows", style="dim")] * (len(table.columns) or 1))
    return table


def _kv_widget(block: Block, needle: str) -> Static:
    grid = RichTable.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", no_wrap=True)
    grid.add_column(overflow="fold")
    shown = 0
    for key, value in block.pairs:
        text = fmt.scalar(value) if not isinstance(value, str) else value
        if needle and needle.lower() not in f"{key} {text}".lower():
            continue
        grid.add_row(str(key), Text(text or "–", style="" if text else "dim"))
        shown += 1
    if not shown:
        grid.add_row("", Text("no matching entries", style="dim"))
    return Static(grid, classes="kv")


def _spark_widget(block: Block) -> Widget:
    values = block.values or [0.0]
    spark = Sparkline(values, summary_function=max, classes="spark")
    spark.tooltip = block.title
    return spark


def render_block(block: Block, needle: str = "") -> list[Widget]:
    """Widgets for one block, including its title and footnote."""
    out: list[Widget] = []
    if block.kind == "note":
        return [Static(block.text or "", classes="note")]
    if block.title:
        out.append(Static(Text(block.title, style="bold"), classes="block-title"))
    if block.kind == "table":
        out.append(_table_widget(block, needle))
    elif block.kind == "kv":
        out.append(_kv_widget(block, needle))
    elif block.kind == "spark":
        out.append(_spark_widget(block))
        lo = min(block.values) if block.values else 0
        hi = max(block.values) if block.values else 0
        out.append(
            Static(
                f"min {fmt.human_count(round(lo))} · max {fmt.human_count(round(hi))} "
                f"· n={len(block.values)}",
                classes="footnote",
            )
        )
    if block.note:
        out.append(Static(block.note, classes="footnote", markup=True))
    return out


def render_blocks(blocks: list[Block], needle: str = "") -> list[Widget]:
    out: list[Widget] = []
    for block in blocks:
        out.extend(render_block(block, needle))
    return out
