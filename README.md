# icefloor

A terminal UI for reading **Iceberg table metadata** and **Parquet file metadata** —
both in one place, with the link between them walkable: pick a data file out of an
Iceberg snapshot and drop straight into that file's row groups, encodings and column
statistics.

## How to use it

If you are an end user, the easiest path is to run it from a checked-out copy of the
repository:

```bash
# 1) install dependencies for this project
uv sync

# 2) inspect a table or file
uv run icefloor path/to/table
uv run icefloor path/to/file.parquet
uv run icefloor path/to/parts/

# 3) load a table from a configured PyIceberg catalog
uv run icefloor db.events -c prod
```

`icefloor` auto-detects what you passed in:

- a `*.parquet` file
- a directory containing Parquet files
- an Iceberg table directory that contains a `metadata/` folder
- a `*.metadata.json` file
- a catalog table name when used with `-c/--catalog`

Useful options:

```bash
uv run icefloor --list-sections path/to/table
uv run icefloor -s snapshots path/to/table
uv run icefloor path/to/table -c prod
```

`--list-sections` prints the available section names and exits. `-s` jumps directly to a
particular section. Once the UI opens, use the keyboard controls shown below to browse,
filter and drill down into the data.

## What it shows

**Iceberg** — table identity and format version, the current and historical schemas,
partition specs and sort orders, every snapshot with its summary, refs, the manifest
list, data and delete files, partition-level record/file/size statistics, the metadata
log, and table properties.

**Parquet** — file version and writer, the Parquet and Arrow schemas with definition and
repetition levels, per-row-group sizes and sort order, every column chunk with its codec
and encodings, min/max/null/distinct statistics, footer key-value metadata, and a
per-column footprint showing where the bytes actually went.

Numbers that benefit from it get an inline bar, scaled to the largest value on screen,
so filtering re-scales the comparison. Series over snapshots or row groups also get a
sparkline.

## Keys

| Key | Action |
| --- | --- |
| `↑` `↓` | move within the focused pane |
| `tab` | next pane |
| `enter` | open the highlighted Parquet file |
| `/` | filter the visible rows |
| `esc` | clear the filter, or go back a level |
| `s` / `S` | scope the file views to the highlighted snapshot / back to current |
| `r` | re-read the current section from storage |
| `y` | copy the highlighted row's key |
| `?` | help |
| `t` | light / dark |
| `q` | quit |

Sections load on a background thread, so a table with thousands of manifests stays
responsive and a section that fails to read reports the error in place.

## Sample data

```bash
uv run python tests/fixture.py fixtures
uv run icefloor fixtures/warehouse/sales/events
```

Builds a partitioned Iceberg table (five snapshots, a schema evolution, an overwrite)
plus loose Parquet files to poke at.

## Development

See `CLAUDE.md` for the architecture and `uv run pytest -q` for the checks.
