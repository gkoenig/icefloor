"""Build a small local Iceberg table plus loose Parquet files for testing."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import random

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("region", pa.string()),
        pa.field("amount", pa.float64()),
        pa.field("note", pa.string()),
        pa.field("event_ts", pa.timestamp("us")),
    ]
)

REGIONS = ["emea", "apac", "amer"]


def batch(n: int, seed: int, start_id: int = 0) -> pa.Table:
    rng = random.Random(seed)
    base = dt.datetime(2026, 1, 1)
    return pa.table(
        {
            "id": pa.array(range(start_id, start_id + n), pa.int64()),
            "region": pa.array([rng.choice(REGIONS) for _ in range(n)], pa.string()),
            "amount": pa.array([round(rng.random() * 1000, 2) for _ in range(n)]),
            "note": pa.array(
                [None if i % 7 == 0 else f"note-{i % 50}" for i in range(n)],
                pa.string(),
            ),
            "event_ts": pa.array(
                [base + dt.timedelta(minutes=i) for i in range(n)], pa.timestamp("us")
            ),
        },
        schema=SCHEMA,
    )


def write_parquet(path: str, rows: int = 5000, row_group_size: int = 1200) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pq.write_table(
        batch(rows, seed=1),
        path,
        row_group_size=row_group_size,
        compression="zstd",
        write_statistics=True,
    )
    return path


def write_parquet_dir(root: str, files: int = 3) -> str:
    os.makedirs(root, exist_ok=True)
    for i in range(files):
        pq.write_table(
            batch(800 * (i + 1), seed=i, start_id=i * 1000),
            os.path.join(root, f"part-{i:03d}.parquet"),
            row_group_size=300,
            compression="snappy" if i % 2 else "gzip",
        )
    return root


def write_iceberg(warehouse: str) -> str:
    """Create a partitioned Iceberg table with several snapshots.

    Returns the table directory, which is what icefloor is pointed at.
    """
    from pyiceberg.catalog.sql import SqlCatalog
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import IdentityTransform

    os.makedirs(warehouse, exist_ok=True)
    catalog = SqlCatalog(
        "test",
        **{
            "uri": f"sqlite:///{warehouse}/catalog.db",
            "warehouse": f"file://{warehouse}",
        },
    )
    catalog.create_namespace_if_not_exists("sales")
    with contextlib.suppress(Exception):
        catalog.drop_table("sales.events")

    from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids
    from pyiceberg.schema import assign_fresh_schema_ids

    iceberg_schema = assign_fresh_schema_ids(_pyarrow_to_schema_without_ids(SCHEMA))
    region_id = iceberg_schema.find_field("region").field_id
    spec = PartitionSpec(
        PartitionField(
            source_id=region_id,
            field_id=1000,
            transform=IdentityTransform(),
            name="region",
        )
    )
    table = catalog.create_table(
        "sales.events",
        schema=iceberg_schema,
        partition_spec=spec,
        properties={
            "write.format.default": "parquet",
            "write.parquet.compression-codec": "zstd",
            "write.target-file-size-bytes": "134217728",
        },
    )

    table.append(batch(4000, seed=10))
    table.append(batch(2500, seed=11, start_id=4000))
    with table.update_schema() as update:
        update.add_column(
            "channel",
            field_type=__import__("pyiceberg.types", fromlist=["StringType"]).StringType(),
            doc="acquisition channel",
        )
    table.overwrite(
        batch(1500, seed=12, start_id=9000).append_column(
            "channel", pa.array(["web"] * 1500, pa.string())
        )
    )
    table.append(
        batch(900, seed=13, start_id=11000).append_column(
            "channel", pa.array(["app"] * 900, pa.string())
        )
    )
    return os.path.join(warehouse, "sales", "events")


def build_all(root: str) -> dict[str, str]:
    return {
        "parquet": write_parquet(os.path.join(root, "events.parquet")),
        "parquet_dir": write_parquet_dir(os.path.join(root, "parts")),
        "iceberg": write_iceberg(os.path.join(root, "warehouse")),
    }


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "fixtures"
    for name, path in build_all(target).items():
        print(f"{name}\t{path}")
