"""Metadata extraction for Iceberg tables and Parquet files.

Everything here is UI-free: a source exposes a list of sections, and each
section is a list of `Block`s made of plain Python values. `views.py` turns
those into Textual widgets.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow.parquet as pq

from . import fmt

# --------------------------------------------------------------------------- #
# Block model
# --------------------------------------------------------------------------- #


@dataclass
class Col:
    name: str
    fmt: Callable[[Any], str] = fmt.scalar
    bar: str | None = None  # bar colour; renders an extra bar column
    frac: bool = False  # values are already 0..1, so don't normalise by max
    right: bool = False


@dataclass
class Block:
    kind: str  # "kv" | "table" | "note"
    title: str = ""
    cols: Sequence[Col] = ()
    rows: list[list[Any]] = field(default_factory=list)
    pairs: list[tuple[str, Any]] = field(default_factory=list)
    text: str = ""
    key_col: int | None = None  # column whose raw value identifies the row
    action: str | None = None  # "parquet" | "snapshot": what enter/s does
    values: list[float] = field(default_factory=list)  # for kind == "spark"
    note: str = ""


@dataclass
class Section:
    key: str
    label: str


def kv(title: str, pairs: list[tuple[str, Any]]) -> Block:
    return Block("kv", title, pairs=[(k, v) for k, v in pairs])


def note(text: str) -> Block:
    return Block("note", text=text)


def spark(title: str, values: list, note_: str = "") -> Block:
    clean = [float(v or 0) for v in values]
    return Block("spark", title, values=clean, note=note_)


def _arrow_rows(table) -> list[dict[str, Any]]:
    return table.to_pylist() if table is not None else []


# --------------------------------------------------------------------------- #
# Parquet
# --------------------------------------------------------------------------- #

_PARQUET_SUFFIXES = (".parquet", ".pq", ".parq")


class ParquetSource:
    """Metadata of a single Parquet file."""

    kind = "parquet"

    def __init__(self, source: Any, label: str, path: str | None = None):
        self.label = label
        self.path = path or (source if isinstance(source, str) else label)
        self._pf = pq.ParquetFile(source)
        self.meta = self._pf.metadata
        self.pschema = self.meta.schema

    @property
    def title(self) -> str:
        return f"parquet · {os.path.basename(self.path)}"

    def sections(self) -> list[Section]:
        secs = [
            Section("overview", "Overview"),
            Section("schema", "Schema"),
            Section("columns", "Columns"),
            Section("rowgroups", "Row Groups"),
            Section("chunks", "Column Chunks"),
            Section("stats", "Statistics"),
        ]
        if self.meta.metadata:
            secs.append(Section("kvmeta", "Key/Value Meta"))
        return secs

    # -- helpers ----------------------------------------------------------- #

    def _chunks(self) -> Iterator[tuple[int, int, Any]]:
        for r in range(self.meta.num_row_groups):
            rg = self.meta.row_group(r)
            for c in range(rg.num_columns):
                yield r, c, rg.column(c)

    def _column_totals(self) -> dict[str, dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for _, _, ch in self._chunks():
            t = totals.setdefault(
                ch.path_in_schema,
                {
                    "compressed": 0,
                    "uncompressed": 0,
                    "values": 0,
                    "nulls": 0,
                    "nulls_known": True,
                    "encodings": set(),
                    "compression": set(),
                    "dict_pages": 0,
                    "physical": ch.physical_type,
                },
            )
            t["compressed"] += ch.total_compressed_size
            t["uncompressed"] += ch.total_uncompressed_size
            t["values"] += ch.num_values
            t["encodings"].update(ch.encodings or ())
            t["compression"].add(ch.compression)
            t["dict_pages"] += 1 if ch.has_dictionary_page else 0
            st = ch.statistics
            if st is not None and st.null_count is not None:
                t["nulls"] += st.null_count
            else:
                t["nulls_known"] = False
        return totals

    # -- sections ---------------------------------------------------------- #

    def section(self, key: str) -> list[Block]:
        return getattr(self, f"_sec_{key}")()

    def _sec_overview(self) -> list[Block]:
        m = self.meta
        # pyarrow reports row_group().total_byte_size uncompressed, so both
        # figures here are summed from the column chunks instead.
        compressed = sum(ch.total_compressed_size for _, _, ch in self._chunks())
        uncompressed = sum(ch.total_uncompressed_size for _, _, ch in self._chunks())
        try:
            file_size = os.path.getsize(self.path)
        except OSError:
            file_size = None
        avg_rg = m.num_rows / m.num_row_groups if m.num_row_groups else 0
        blocks = [
            kv(
                "File",
                [
                    ("Path", self.path),
                    ("File size", fmt.human_bytes(file_size)),
                    ("Format version", m.format_version),
                    ("Created by", m.created_by),
                    ("Serialized size", fmt.human_bytes(m.serialized_size)),
                ],
            ),
            kv(
                "Contents",
                [
                    ("Rows", fmt.human_count(m.num_rows)),
                    ("Columns", m.num_columns),
                    ("Row groups", m.num_row_groups),
                    ("Avg rows / row group", fmt.human_count(round(avg_rg))),
                    ("Leaf columns", self.pschema.__len__()),
                ],
            ),
            kv(
                "Size",
                [
                    ("Compressed (column chunks)", fmt.human_bytes(compressed)),
                    ("Uncompressed", fmt.human_bytes(uncompressed)),
                    (
                        "Compression ratio",
                        f"{uncompressed / compressed:.2f}×" if compressed else "–",
                    ),
                    (
                        "Bytes / row",
                        f"{compressed / m.num_rows:.1f}" if m.num_rows else "–",
                    ),
                ],
            ),
        ]
        codecs = sorted({ch.compression for _, _, ch in self._chunks()})
        encs = sorted({e for _, _, ch in self._chunks() for e in (ch.encodings or ())})
        blocks.append(
            kv("Encoding", [("Codecs", ", ".join(codecs)), ("Encodings", ", ".join(encs))])
        )
        return blocks

    def _sec_schema(self) -> list[Block]:
        cols = [
            Col("#", fmt.scalar, right=True),
            Col("Column"),
            Col("Physical"),
            Col("Logical"),
            Col("Converted"),
            Col("Arrow type"),
            Col("Def", fmt.scalar, right=True),
            Col("Rep", fmt.scalar, right=True),
        ]
        arrow_types = {}
        try:
            for f in self._pf.schema_arrow:
                arrow_types[f.name] = str(f.type)
        except Exception:  # pragma: no cover - schema_arrow is best effort
            pass
        rows = []
        for i in range(len(self.pschema)):
            c = self.pschema.column(i)
            logical = str(c.logical_type) if c.logical_type is not None else "–"
            rows.append(
                [
                    i,
                    c.path,
                    c.physical_type,
                    "–" if logical in ("None", "none") else logical,
                    c.converted_type or "–",
                    arrow_types.get(c.path.split(".")[0], "–"),
                    c.max_definition_level,
                    c.max_repetition_level,
                ]
            )
        blocks = [Block("table", f"Leaf columns ({len(rows)})", cols, rows)]
        blocks.append(note(str(self._pf.schema_arrow)))
        return blocks

    def _sec_columns(self) -> list[Block]:
        totals = self._column_totals()
        total_compressed = sum(t["compressed"] for t in totals.values()) or 1
        cols = [
            Col("Column"),
            Col("Physical"),
            Col("Compressed", fmt.human_bytes, bar="cyan", right=True),
            Col("Share", lambda v: fmt.pct(v, 1.0), frac=True, bar="cyan", right=True),
            Col("Uncompressed", fmt.human_bytes, right=True),
            Col("Ratio", lambda v: f"{v:.2f}×" if v else "–", bar="green", right=True),
            Col("Nulls", fmt.human_count, right=True),
            Col("Null %", lambda v: fmt.pct(v, 1.0), frac=True, bar="yellow", right=True),
            Col("Codec"),
            Col("Encodings"),
            Col("Dict pages", fmt.scalar, right=True),
        ]
        rows = []
        for name, t in sorted(totals.items(), key=lambda kv_: kv_[1]["compressed"], reverse=True):
            ratio = t["uncompressed"] / t["compressed"] if t["compressed"] else 0
            null_frac = t["nulls"] / t["values"] if t["nulls_known"] and t["values"] else 0.0
            rows.append(
                [
                    name,
                    t["physical"],
                    t["compressed"],
                    t["compressed"] / total_compressed,
                    t["uncompressed"],
                    ratio,
                    t["nulls"] if t["nulls_known"] else None,
                    null_frac,
                    ",".join(sorted(t["compression"])),
                    ",".join(sorted(t["encodings"])),
                    f"{t['dict_pages']}/{self.meta.num_row_groups}",
                ]
            )
        return [
            Block(
                "table",
                "Per-column footprint (summed over row groups)",
                cols,
                rows,
                note="Ratio = uncompressed / compressed. Null % needs per-chunk statistics.",
            ),
            spark(
                "Compressed bytes per column (largest first)",
                [r[2] for r in rows],
            ),
        ]

    def _sec_rowgroups(self) -> list[Block]:
        cols = [
            Col("RG", fmt.scalar, right=True),
            Col("Rows", fmt.human_count, bar="cyan", right=True),
            Col("Compressed", fmt.human_bytes, bar="green", right=True),
            Col("Uncompressed", fmt.human_bytes, right=True),
            Col("Ratio", lambda v: f"{v:.2f}×" if v else "–", right=True),
            Col("Bytes/row", lambda v: f"{v:,.1f}" if v else "–", right=True),
            Col("Cols", fmt.scalar, right=True),
            Col("Sorted by"),
        ]
        rows = []
        for r in range(self.meta.num_row_groups):
            rg = self.meta.row_group(r)
            chunks = [rg.column(c) for c in range(rg.num_columns)]
            compressed = sum(ch.total_compressed_size for ch in chunks)
            uncompressed = sum(ch.total_uncompressed_size for ch in chunks)
            sorting = ", ".join(
                f"{self.pschema.column(sc.column_index).path}{' desc' if sc.descending else ''}"
                for sc in (rg.sorting_columns or ())
            )
            rows.append(
                [
                    r,
                    rg.num_rows,
                    compressed,
                    uncompressed,
                    uncompressed / compressed if compressed else 0,
                    compressed / rg.num_rows if rg.num_rows else 0,
                    rg.num_columns,
                    sorting or "–",
                ]
            )
        return [
            Block("table", f"Row groups ({len(rows)})", cols, rows),
            spark("Rows per row group", [r[1] for r in rows]),
            spark("Compressed bytes per row group", [r[2] for r in rows]),
        ]

    def _sec_chunks(self) -> list[Block]:
        cols = [
            Col("RG", fmt.scalar, right=True),
            Col("Column"),
            Col("Values", fmt.human_count, right=True),
            Col("Compressed", fmt.human_bytes, bar="cyan", right=True),
            Col("Uncompressed", fmt.human_bytes, right=True),
            Col("Codec"),
            Col("Encodings"),
            Col("Dict", lambda v: "yes" if v else "–"),
            Col("Data offset", fmt.human_count, right=True),
        ]
        rows = [
            [
                r,
                ch.path_in_schema,
                ch.num_values,
                ch.total_compressed_size,
                ch.total_uncompressed_size,
                ch.compression,
                ",".join(ch.encodings or ()),
                ch.has_dictionary_page,
                ch.data_page_offset,
            ]
            for r, _, ch in self._chunks()
        ]
        return [Block("table", f"Column chunks ({len(rows)})", cols, rows)]

    def _sec_stats(self) -> list[Block]:
        cols = [
            Col("RG", fmt.scalar, right=True),
            Col("Column"),
            Col("Min"),
            Col("Max"),
            Col("Nulls", fmt.human_count, right=True),
            Col("Null %", lambda v: fmt.pct(v, 1.0), frac=True, bar="yellow", right=True),
            Col("Distinct", fmt.human_count, right=True),
            Col("Values", fmt.human_count, right=True),
        ]
        rows = []
        missing = 0
        for r, _, ch in self._chunks():
            st = ch.statistics
            if st is None:
                missing += 1
                continue
            nulls = st.null_count
            rows.append(
                [
                    r,
                    ch.path_in_schema,
                    fmt.short(fmt.scalar(st.min), 28),
                    fmt.short(fmt.scalar(st.max), 28),
                    nulls,
                    (nulls / ch.num_values) if (nulls is not None and ch.num_values) else 0.0,
                    st.distinct_count,
                    ch.num_values,
                ]
            )
        blocks = [Block("table", f"Chunk statistics ({len(rows)})", cols, rows)]
        if missing:
            blocks.append(note(f"{missing} column chunk(s) carry no statistics."))
        return blocks

    def _sec_kvmeta(self) -> list[Block]:
        pairs = []
        for k, v in (self.meta.metadata or {}).items():
            key = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
            val = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            pairs.append((key, val))
        return [kv("Footer key/value metadata", sorted(pairs))]


# --------------------------------------------------------------------------- #
# Iceberg
# --------------------------------------------------------------------------- #


def _walk_schema(schema) -> Iterator[tuple[int, Any]]:
    from pyiceberg.types import ListType, MapType, StructType

    def rec(fields, depth):
        for f in fields:
            yield depth, f
            t = f.field_type
            if isinstance(t, StructType):
                yield from rec(t.fields, depth + 1)
            elif isinstance(t, ListType):
                yield from rec([t.element_field], depth + 1)
            elif isinstance(t, MapType):
                yield from rec([t.key_field, t.value_field], depth + 1)

    yield from rec(schema.fields, 0)


def _type_label(t) -> str:
    from pyiceberg.types import ListType, MapType, StructType

    if isinstance(t, StructType):
        return f"struct<{len(t.fields)} fields>"
    if isinstance(t, ListType):
        return "list<…>"
    if isinstance(t, MapType):
        return "map<…>"
    return str(t)


class IcebergSource:
    """Metadata of an Iceberg table loaded from its metadata.json."""

    kind = "iceberg"

    def __init__(self, table, label: str):
        self.table = table
        self.label = label
        self.snapshot_id: int | None = None  # None = current

    @property
    def title(self) -> str:
        return f"iceberg · {self.label}"

    @property
    def active_snapshot(self):
        if self.snapshot_id is not None:
            return self.table.snapshot_by_id(self.snapshot_id)
        return self.table.current_snapshot()

    def sections(self) -> list[Section]:
        return [
            Section("overview", "Overview"),
            Section("schema", "Schema"),
            Section("partitioning", "Partitioning"),
            Section("partitions", "Partition Stats"),
            Section("sort", "Sort Orders"),
            Section("snapshots", "Snapshots"),
            Section("refs", "Refs"),
            Section("manifests", "Manifests"),
            Section("datafiles", "Data Files"),
            Section("deletefiles", "Delete Files"),
            Section("history", "History"),
            Section("metalog", "Metadata Log"),
            Section("properties", "Properties"),
        ]

    def section(self, key: str) -> list[Block]:
        return getattr(self, f"_sec_{key}")()

    def open_data_file(self, path: str) -> ParquetSource:
        """Open a data file referenced by the table as a Parquet source."""
        inp = self.table.io.new_input(path)
        return ParquetSource(inp.open(), os.path.basename(path), path=path)

    # -- sections ---------------------------------------------------------- #

    def _sec_overview(self) -> list[Block]:
        t = self.table
        md = t.metadata
        snap = self.active_snapshot
        summary = _summary_dict(snap)
        blocks = [
            kv(
                "Table",
                [
                    ("Identifier", ".".join(t.name()) if callable(t.name) else t.name),
                    ("Location", md.location),
                    ("Metadata", t.metadata_location),
                    ("Format version", md.format_version),
                    ("Table UUID", md.table_uuid),
                    ("Last updated", fmt.ts_ms(md.last_updated_ms)),
                    ("Last sequence number", md.last_sequence_number),
                    ("Last column id", md.last_column_id),
                ],
            ),
            kv(
                "Current state",
                [
                    ("Schema id", md.current_schema_id),
                    ("Default spec id", md.default_spec_id),
                    ("Default sort order id", md.default_sort_order_id),
                    ("Snapshots", len(md.snapshots)),
                    ("Schemas", len(md.schemas)),
                    ("Partition specs", len(md.partition_specs)),
                    ("Sort orders", len(md.sort_orders)),
                    ("Refs", len(md.refs)),
                ],
            ),
        ]
        if snap:
            blocks.append(
                kv(
                    f"Snapshot {snap.snapshot_id}"
                    + (" (current)" if self.snapshot_id is None else " (selected)"),
                    [
                        ("Committed at", fmt.ts_ms(snap.timestamp_ms)),
                        ("Operation", summary.get("operation", "–")),
                        ("Parent", fmt.ident(snap.parent_snapshot_id)),
                        ("Sequence number", snap.sequence_number),
                        ("Manifest list", snap.manifest_list),
                    ],
                )
            )
            size_keys = {"total-files-size", "added-files-size", "removed-files-size"}
            blocks.append(
                kv(
                    "Snapshot summary",
                    [
                        (
                            k,
                            fmt.human_bytes(v) if k in size_keys else v,
                        )
                        for k, v in sorted(summary.items())
                        if k != "operation"
                    ],
                )
            )
        else:
            blocks.append(note("Table has no snapshots yet."))
        return blocks

    def _sec_schema(self) -> list[Block]:
        sch = self.table.schema()
        cols = [
            Col("ID", fmt.scalar, right=True),
            Col("Field"),
            Col("Type"),
            Col("Required", lambda v: "required" if v else "optional"),
            Col("Doc"),
        ]
        rows = [
            [
                f.field_id,
                ("  " * depth) + ("└ " if depth else "") + f.name,
                _type_label(f.field_type),
                f.required,
                f.doc or "–",
            ]
            for depth, f in _walk_schema(sch)
        ]
        blocks = [Block("table", f"Schema {sch.schema_id} ({len(rows)} fields)", cols, rows)]
        if sch.identifier_field_ids:
            blocks.append(
                kv(
                    "Identifiers",
                    [("Identifier field ids", sorted(sch.identifier_field_ids))],
                )
            )
        others = [s for s in self.table.metadata.schemas if s.schema_id != sch.schema_id]
        if others:
            blocks.append(
                Block(
                    "table",
                    "Other schemas",
                    [
                        Col("Schema id", fmt.scalar, right=True),
                        Col("Fields", fmt.scalar, right=True),
                    ],
                    [[s.schema_id, len(s.fields)] for s in others],
                )
            )
        return blocks

    def _sec_partitioning(self) -> list[Block]:
        md = self.table.metadata
        schema = self.table.schema()
        cols = [
            Col("Spec", fmt.scalar, right=True),
            Col("Field id", fmt.scalar, right=True),
            Col("Partition field"),
            Col("Transform"),
            Col("Source id", fmt.scalar, right=True),
            Col("Source field"),
        ]
        rows = []
        for spec in md.partition_specs:
            marker = "*" if spec.spec_id == md.default_spec_id else ""
            if not spec.fields:
                rows.append([f"{spec.spec_id}{marker}", "–", "(unpartitioned)", "–", "–", "–"])
            for f in spec.fields:
                src = schema.find_field(f.source_id) if f.source_id else None
                rows.append(
                    [
                        f"{spec.spec_id}{marker}",
                        f.field_id,
                        f.name,
                        str(f.transform),
                        f.source_id,
                        src.name if src else "–",
                    ]
                )
        return [
            Block(
                "table",
                "Partition specs",
                cols,
                rows,
                note="* marks the default spec.",
            )
        ]

    def _sec_partitions(self) -> list[Block]:
        rows_in = _arrow_rows(self.table.inspect.partitions(self.snapshot_id))
        if not rows_in:
            return [note("No partition statistics for this snapshot.")]
        cols = [
            Col("Partition"),
            Col("Spec", fmt.scalar, right=True),
            Col("Records", fmt.human_count, bar="cyan", right=True),
            Col("Files", fmt.human_count, bar="green", right=True),
            Col("Size", fmt.human_bytes, bar="magenta", right=True),
            Col("Pos deletes", fmt.human_count, right=True),
            Col("Eq deletes", fmt.human_count, right=True),
            Col("Last updated"),
        ]
        rows = []
        for r in rows_in:
            part = r.get("partition") or {}
            label = (
                ", ".join(f"{k}={fmt.scalar(v)}" for k, v in part.items())
                if isinstance(part, dict) and part
                else "(unpartitioned)"
            )
            rows.append(
                [
                    label,
                    r.get("spec_id"),
                    r.get("record_count"),
                    r.get("file_count"),
                    r.get("total_data_file_size_in_bytes"),
                    r.get("position_delete_record_count"),
                    r.get("equality_delete_record_count"),
                    fmt.ts_ms(r.get("last_updated_at")),
                ]
            )
        rows.sort(key=lambda row: row[2] or 0, reverse=True)
        return [Block("table", f"Partitions ({len(rows)})", cols, rows)]

    def _sec_sort(self) -> list[Block]:
        md = self.table.metadata
        schema = self.table.schema()
        cols = [
            Col("Order", fmt.scalar, right=True),
            Col("Source id", fmt.scalar, right=True),
            Col("Field"),
            Col("Transform"),
            Col("Direction"),
            Col("Nulls"),
        ]
        rows = []
        for order in md.sort_orders:
            marker = "*" if order.order_id == md.default_sort_order_id else ""
            if not order.fields:
                rows.append([f"{order.order_id}{marker}", "–", "(unsorted)", "–", "–", "–"])
            for f in order.fields:
                src = schema.find_field(f.source_id) if f.source_id else None
                rows.append(
                    [
                        f"{order.order_id}{marker}",
                        f.source_id,
                        src.name if src else "–",
                        str(f.transform),
                        str(f.direction.name).lower(),
                        str(f.null_order.name).lower().replace("nulls_", "nulls "),
                    ]
                )
        return [Block("table", "Sort orders", cols, rows, note="* marks the default order.")]

    def _sec_snapshots(self) -> list[Block]:
        snaps = _arrow_rows(self.table.inspect.snapshots())
        if not snaps:
            return [note("Table has no snapshots.")]
        cols = [
            Col(""),
            Col("Committed at"),
            Col("Snapshot id", fmt.ident),
            Col("Parent id"),
            Col("Operation"),
            Col("Added recs", fmt.human_count, bar="green", right=True),
            Col("Deleted recs", fmt.human_count, bar="red", right=True),
            Col("Total recs", fmt.human_count, bar="cyan", right=True),
            Col("Total size", fmt.human_bytes, bar="magenta", right=True),
            Col("Files", fmt.human_count, right=True),
        ]
        current = self.table.current_snapshot()
        current_id = current.snapshot_id if current else None
        rows = []
        for s in snaps:
            summary = s.get("summary") or {}
            if isinstance(summary, list):  # arrow map decodes to pairs
                summary = dict(summary)
            sid = s.get("snapshot_id")
            marks = ("*" if sid == current_id else " ") + ("›" if sid == self.snapshot_id else "")
            rows.append(
                [
                    marks.strip() or "",
                    fmt.ts_ms(s.get("committed_at")),
                    sid,
                    fmt.ident(s.get("parent_id")),
                    s.get("operation") or summary.get("operation", "–"),
                    _num(summary.get("added-records")),
                    _num(summary.get("deleted-records")),
                    _num(summary.get("total-records")),
                    _num(summary.get("total-files-size")),
                    _num(summary.get("total-data-files")),
                ]
            )
        blocks = [
            Block(
                "table",
                f"Snapshots ({len(rows)}, oldest first)",
                cols,
                rows,
                key_col=2,
                action="snapshot",
                note=(
                    "* current · › selected.  Press [b]s[/b] to scope the file views to "
                    "the highlighted snapshot, [b]S[/b] to go back to current."
                ),
            ),
            spark(
                "Total records over time",
                [r[7] for r in rows],
                "One cell per snapshot, oldest on the left.",
            ),
            spark("Total size on disk over time", [r[8] for r in rows]),
        ]
        return blocks

    def _sec_refs(self) -> list[Block]:
        refs = _arrow_rows(self.table.inspect.refs())
        cols = [
            Col("Name"),
            Col("Type"),
            Col("Snapshot id", fmt.ident),
            Col("Max ref age (ms)", fmt.human_count, right=True),
            Col("Min snapshots to keep", fmt.human_count, right=True),
            Col("Max snapshot age (ms)", fmt.human_count, right=True),
        ]
        rows = [
            [
                r.get("name"),
                r.get("type"),
                fmt.ident(r.get("snapshot_id")),
                r.get("max_reference_age_in_ms"),
                r.get("min_snapshots_to_keep"),
                r.get("max_snapshot_age_in_ms"),
            ]
            for r in refs
        ]
        return [Block("table", f"Refs ({len(rows)})", cols, rows)] if rows else [note("No refs.")]

    def _sec_manifests(self) -> list[Block]:
        mans = _arrow_rows(self.table.inspect.manifests())
        if not mans:
            return [note("No manifests in the current snapshot.")]
        cols = [
            Col("Manifest", lambda v: fmt.short(v, 48)),
            Col("Size", fmt.human_bytes, bar="cyan", right=True),
            Col("Spec", fmt.scalar, right=True),
            Col("Content", lambda v: {0: "data", 1: "deletes"}.get(v, fmt.scalar(v))),
            Col("Added snapshot", fmt.ident),
            Col("Added", fmt.human_count, bar="green", right=True),
            Col("Existing", fmt.human_count, right=True),
            Col("Deleted", fmt.human_count, bar="red", right=True),
            Col("Added rows", fmt.human_count, right=True),
            Col("Partition summaries", fmt.human_count, right=True),
        ]
        rows = [
            [
                m.get("path"),
                m.get("length"),
                m.get("partition_spec_id"),
                m.get("content"),
                fmt.ident(m.get("added_snapshot_id")),
                m.get("added_data_files_count") or m.get("added_delete_files_count"),
                m.get("existing_data_files_count") or m.get("existing_delete_files_count"),
                m.get("deleted_data_files_count") or m.get("deleted_delete_files_count"),
                m.get("added_rows_count"),
                len(m.get("partition_summaries") or ()),
            ]
            for m in mans
        ]
        return [Block("table", f"Manifests ({len(rows)})", cols, rows)]

    def _files_block(self, arrow_rows, title: str) -> list[Block]:
        if not arrow_rows:
            return [note(f"No {title.lower()} for this snapshot.")]
        cols = [
            Col("File", lambda v: fmt.short(v, 46)),
            Col("Fmt"),
            Col("Partition"),
            Col("Records", fmt.human_count, bar="cyan", right=True),
            Col("Size", fmt.human_bytes, bar="magenta", right=True),
            Col("Bytes/row", lambda v: f"{v:,.1f}" if v else "–", right=True),
            Col("Cols", fmt.scalar, right=True),
            Col("Nulls", fmt.human_count, right=True),
            Col("Split offsets", fmt.scalar, right=True),
            Col("Seq", fmt.scalar, right=True),
        ]
        rows = []
        for f in arrow_rows:
            part = f.get("partition") or {}
            label = (
                ", ".join(f"{k}={fmt.scalar(v)}" for k, v in part.items())
                if isinstance(part, dict) and part
                else "–"
            )
            recs = f.get("record_count") or 0
            size = f.get("file_size_in_bytes") or 0
            nulls = f.get("null_value_counts") or {}
            if isinstance(nulls, list):
                nulls = dict(nulls)
            rows.append(
                [
                    f.get("file_path"),
                    f.get("file_format"),
                    label,
                    recs,
                    size,
                    (size / recs) if recs else 0,
                    len(f.get("column_sizes") or ()),
                    sum(nulls.values()) if nulls else 0,
                    len(f.get("split_offsets") or ()),
                    f.get("sequence_number"),
                ]
            )
        rows.sort(key=lambda r: r[4] or 0, reverse=True)
        return [
            Block(
                "table",
                f"{title} ({len(rows)})",
                cols,
                rows,
                key_col=0,
                action="parquet",
                note="Press [b]enter[/b] to open the file's Parquet metadata.",
            )
        ]

    def _sec_datafiles(self) -> list[Block]:
        return self._files_block(
            _arrow_rows(self.table.inspect.data_files(self.snapshot_id)), "Data files"
        )

    def _sec_deletefiles(self) -> list[Block]:
        return self._files_block(
            _arrow_rows(self.table.inspect.delete_files(self.snapshot_id)),
            "Delete files",
        )

    def _sec_history(self) -> list[Block]:
        rows_in = _arrow_rows(self.table.inspect.history())
        cols = [
            Col("Made current at"),
            Col("Snapshot id", fmt.ident),
            Col("Parent id", fmt.ident),
            Col("Is current ancestor", lambda v: "yes" if v else "no"),
        ]
        rows = [
            [
                fmt.ts_ms(r.get("made_current_at")),
                fmt.ident(r.get("snapshot_id")),
                fmt.ident(r.get("parent_id")),
                r.get("is_current_ancestor"),
            ]
            for r in rows_in
        ]
        return (
            [Block("table", f"History ({len(rows)})", cols, rows)]
            if rows
            else [note("No history entries.")]
        )

    def _sec_metalog(self) -> list[Block]:
        rows_in = _arrow_rows(self.table.inspect.metadata_log_entries())
        cols = [
            Col("Timestamp"),
            Col("Metadata file", lambda v: fmt.short(v, 56)),
            Col("Snapshot id", fmt.ident),
            Col("Schema id", fmt.scalar, right=True),
        ]
        rows = [
            [
                fmt.ts_ms(r.get("timestamp")),
                r.get("file"),
                fmt.ident(r.get("latest_snapshot_id")),
                r.get("latest_schema_id"),
            ]
            for r in rows_in
        ]
        return (
            [Block("table", f"Metadata log ({len(rows)})", cols, rows)]
            if rows
            else [note("No metadata log entries.")]
        )

    def _sec_properties(self) -> list[Block]:
        props = dict(self.table.properties or {})
        if not props:
            return [note("No table properties set.")]
        return [kv("Table properties", sorted(props.items()))]


def _summary_dict(snapshot) -> dict[str, Any]:
    """Flatten a Snapshot.summary (operation + free-form properties) to a dict."""
    if snapshot is None or snapshot.summary is None:
        return {}
    summary = snapshot.summary
    out = dict(getattr(summary, "additional_properties", None) or {})
    operation = getattr(summary, "operation", None)
    if operation is not None:
        out["operation"] = getattr(operation, "value", str(operation))
    return out


def _num(v: Any) -> Any:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


# --------------------------------------------------------------------------- #
# Directory of Parquet files
# --------------------------------------------------------------------------- #


class ParquetDirSource:
    """A directory of Parquet files; drill into any one of them."""

    kind = "parquet-dir"

    def __init__(self, root: str, files: list[str]):
        self.root = root
        self.files = files
        self.label = os.path.basename(root.rstrip("/")) or root

    @property
    def title(self) -> str:
        return f"parquet dir · {self.label}"

    def sections(self) -> list[Section]:
        return [Section("files", "Files"), Section("summary", "Summary")]

    def section(self, key: str) -> list[Block]:
        return getattr(self, f"_sec_{key}")()

    def open_data_file(self, path: str) -> ParquetSource:
        return ParquetSource(path, os.path.basename(path), path=path)

    def _scan(self) -> list[dict[str, Any]]:
        out = []
        for p in self.files:
            try:
                m = pq.ParquetFile(p).metadata
                out.append(
                    {
                        "path": p,
                        "rows": m.num_rows,
                        "cols": m.num_columns,
                        "rgs": m.num_row_groups,
                        "size": os.path.getsize(p),
                        "created_by": m.created_by,
                        "error": None,
                    }
                )
            except Exception as exc:  # unreadable file should not kill the view
                out.append({"path": p, "error": str(exc), "size": 0, "rows": 0})
        return out

    def _sec_files(self) -> list[Block]:
        scanned = self._scan()
        cols = [
            # The raw cell keeps the absolute path (that is what enter opens);
            # only the display is shortened to the part below the root.
            Col("File", lambda v: fmt.short(os.path.relpath(v, self.root), 50)),
            Col("Rows", fmt.human_count, bar="cyan", right=True),
            Col("Size", fmt.human_bytes, bar="magenta", right=True),
            Col("Row groups", fmt.scalar, right=True),
            Col("Cols", fmt.scalar, right=True),
            Col("Created by", lambda v: fmt.head(v, 34)),
        ]
        rows = [
            [
                s["path"],
                s.get("rows"),
                s.get("size"),
                s.get("rgs"),
                s.get("cols"),
                s.get("error") or s.get("created_by"),
            ]
            for s in scanned
        ]
        rows.sort(key=lambda r: r[2] or 0, reverse=True)
        return [
            Block(
                "table",
                f"Parquet files ({len(rows)})",
                cols,
                rows,
                key_col=0,
                action="parquet",
                note="Press [b]enter[/b] to open a file's Parquet metadata.",
            )
        ]

    def _sec_summary(self) -> list[Block]:
        scanned = self._scan()
        ok = [s for s in scanned if not s.get("error")]
        return [
            kv(
                "Directory",
                [
                    ("Root", self.root),
                    ("Files", len(scanned)),
                    ("Unreadable", len(scanned) - len(ok)),
                    ("Total rows", fmt.human_count(sum(s["rows"] for s in ok))),
                    ("Total size", fmt.human_bytes(sum(s["size"] for s in ok))),
                    ("Row groups", sum(s.get("rgs") or 0 for s in ok)),
                ],
            )
        ]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


class LoadError(Exception):
    pass


_AZURE_SCHEMES = ("abfs://", "abfss://", "wasb://", "wasbs://")


def _iceberg_metadata_json(path: str) -> str | None:
    """Return the newest metadata.json for a table directory, if any."""
    meta_dir = path if os.path.basename(path) == "metadata" else os.path.join(path, "metadata")
    if not os.path.isdir(meta_dir):
        return None
    candidates = [
        os.path.join(meta_dir, n) for n in os.listdir(meta_dir) if n.endswith(".metadata.json")
    ]
    if not candidates:
        return None
    # Names are vNNN-uuid.metadata.json or NNNNN-uuid.metadata.json; mtime is
    # the only ordering that holds for both, and version-hint.text may be stale.
    return max(candidates, key=os.path.getmtime)


def _load_from_catalog(target: str, catalog: str):
    from pyiceberg.catalog import load_catalog
    from pyiceberg.exceptions import (
        ForbiddenError,
        NoSuchNamespaceError,
        NoSuchTableError,
        OAuthError,
        UnauthorizedError,
    )

    # A REST catalog authenticates inside load_catalog, so both calls share one handler.
    try:
        table = load_catalog(catalog).load_table(target)
    except ValueError as exc:
        raise LoadError(f"Catalog {catalog!r} is not usable: {exc}") from exc
    except (NoSuchTableError, NoSuchNamespaceError) as exc:
        raise LoadError(
            f"{target!r} not found in catalog {catalog!r}: {exc}. "
            "With Unity Catalog, `warehouse` names the UC catalog; pass schema.table."
        ) from exc
    except (UnauthorizedError, OAuthError) as exc:
        raise LoadError(
            f"Catalog {catalog!r} rejected the credentials: {exc}. "
            "Check `token`, or `credential` + `oauth2-server-uri` + `scope`."
        ) from exc
    except ForbiddenError as exc:
        raise LoadError(
            f"Catalog {catalog!r} denied access to {target!r}: {exc}. "
            "Unity Catalog needs USE CATALOG, USE SCHEMA, SELECT and EXTERNAL USE SCHEMA."
        ) from exc

    # Without adlfs PyIceberg falls back to PyArrow's Azure filesystem, which ignores the
    # per-account SAS tokens a REST catalog vends; every read then fails with an opaque
    # auth error, so stop here with the fix instead.
    if table.metadata.location.startswith(_AZURE_SCHEMES) and not importlib.util.find_spec("adlfs"):
        raise LoadError(
            "Table lives on Azure storage but adlfs is not installed. "
            "Install the extra: uv tool install 'icefloor[azure]'"
        )
    return table


def load(target: str, catalog: str | None = None):
    """Load `target` as an Iceberg table, a Parquet file, or a Parquet dir."""
    from pyiceberg.table import StaticTable

    if catalog:
        return IcebergSource(_load_from_catalog(target, catalog), target)

    if target.startswith(
        ("s3://", "s3a://", "gs://", "abfs://", "abfss://", "http://", "https://")
    ):
        if target.endswith(".metadata.json"):
            return IcebergSource(StaticTable.from_metadata(target), target)
        raise LoadError("Remote targets must be a .metadata.json path, or use --catalog NAME.")

    path = os.path.abspath(os.path.expanduser(target))
    if not os.path.exists(path):
        raise LoadError(f"No such path: {target}")

    if os.path.isfile(path):
        if path.endswith(".metadata.json"):
            return IcebergSource(StaticTable.from_metadata(path), path)
        if path.lower().endswith(_PARQUET_SUFFIXES):
            return ParquetSource(path, os.path.basename(path), path=path)
        raise LoadError(f"Unsupported file: {target} (expected *.parquet or *.metadata.json)")

    meta_json = _iceberg_metadata_json(path)
    if meta_json:
        return IcebergSource(StaticTable.from_metadata(meta_json), path)

    files = []
    for root, _, names in os.walk(path):
        for n in sorted(names):
            if n.lower().endswith(_PARQUET_SUFFIXES):
                files.append(os.path.join(root, n))
    if files:
        return ParquetDirSource(path, files)

    raise LoadError(f"{target} holds no Parquet files and no Iceberg metadata/ directory.")
