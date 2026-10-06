# icefloor

Terminal UI that inspects and visualises **Iceberg table metadata** and **Parquet file
metadata** side by side. Point it at a path; it works out what the path is.

> Project-specific Python rules live in `.claude/skills/python-conventions/SKILL.md`.
> Add your own details to the **Your notes** section at the bottom of this file.

## Running it

```bash
uv run icefloor <target>                 # open the TUI
uv run icefloor <target> --list-sections # print section keys, exit
uv run icefloor <target> -s snapshots    # open straight on a section
uv run icefloor db.events -c prod        # load from a configured PyIceberg catalog
```

`<target>` is auto-detected: a `*.parquet` file, a directory of Parquet files, an
Iceberg table directory (one holding `metadata/`), or a `*.metadata.json` file.

## Development

```bash
uv sync                      # Python 3.12 venv, pinned by .python-version + uv.lock
uv run pytest -q             # the full check (headless Textual, ~5s)
uv run ruff check . && uv run ruff format .
uv run python tests/fixture.py fixtures   # write sample Parquet + Iceberg data
```

`tests/fixture.py` builds a real local Iceberg table (SQLite catalog, partitioned by
`region`, five snapshots, a schema evolution and an overwrite) plus loose Parquet files.
Run it when you want something to look at by hand.

## Layout

| File | Role |
| --- | --- |
| `src/icefloor/meta.py` | All metadata extraction. **UI-free.** Sources return `Block`s of plain Python values. |
| `src/icefloor/views.py` | `Block` → Textual widget. Owns bar/table/sparkline rendering. |
| `src/icefloor/app.py` | App shell: nav pane, body, key bindings, drill-down, snapshot scoping. |
| `src/icefloor/fmt.py` | Formatters (`human_bytes`, `ts_ms`, `bar`, `ident`, …). |
| `src/icefloor/app.tcss` | Styling. |
| `src/icefloor/cli.py` | Argument parsing, path dispatch, exit codes. |

### The Block model

A source exposes `sections()` and `section(key) -> list[Block]`. A `Block` is one of:

- `kind="kv"` — `pairs` of label/value
- `kind="table"` — `cols: list[Col]` + `rows: list[list]` of **raw** values
- `kind="spark"` — `values: list[float]`
- `kind="note"` — a prose `text`

`Col` carries the formatter and whether the column also gets a bar
(`bar="cyan"`, plus `frac=True` when the value is already 0..1). Bars are normalised
against the largest value *currently on screen*, so filtering rescales them.

Set `Block.key_col` + `Block.action` to make rows actionable: `"parquet"` opens the
row's path as a new Parquet screen, `"snapshot"` scopes the Iceberg file views.

**Adding a section** means adding one `Section` to `sections()` and one
`_sec_<key>()` method returning `Block`s. No UI change needed.

## Catalog support (`-c NAME`)

`meta._load_from_catalog` calls `pyiceberg.catalog.load_catalog(NAME).load_table(target)`
and wraps the result in the same `IcebergSource` used for local tables. From there on,
every read goes through `table.io`. icefloor itself holds **no** catalog or credential
logic. Endpoint, auth, TLS and storage credentials all come from PyIceberg config
(`~/.pyiceberg.yaml`, `PYICEBERG_HOME`, `PYICEBERG_CATALOG__<NAME>__<KEY>` env vars).

**Two access paths.** The catalog returns only a pointer to `metadata.json`. Metadata,
manifests and Parquet footers are then read straight from object storage. That needs either
credentials the catalog vends with the table (REST + `X-Iceberg-Access-Delegation`,
which PyIceberg sends by default) or storage credentials in config (`s3.*`, `adls.*`,
`gcs.*`).

**Status per backend:**

| Backend | Status | Extra |
| --- | --- | --- |
| SQL (SQLite) | covered by the test suite | `sql` |
| REST, Databricks Unity Catalog on Azure | documented in README, not CI-tested (needs a live workspace) | `azure` |
| Glue + S3 | extras shipped, untested | `glue`, `s3` |
| Hive, GCS, other REST + ADLS setups | untested; would need `pyiceberg[hive]` / `[gcsfs]` | none yet |

**Error contract.** `_load_from_catalog` turns user-fixable failures into `LoadError`
(exit 2, one line with a hint). These are unknown or misconfigured catalog (`ValueError`),
missing table or namespace, 401 and OAuth failures, 403, and an Azure table location without
`adlfs` installed. Anything else, such as network errors, falls through to `cli.main`'s
generic handler (exit 1).

**Boundaries and limitations:**
- **Read-only.** icefloor never calls a write or commit API. Keep it that way.
- **Offline by default.** Catalog code is imported lazily, only when `-c` is passed.
  Local paths must never touch the network or need credentials, and no catalog extra
  may become a core dependency.
- **No credential flags.** Config is PyIceberg's, unchanged. Don't add `--token` style
  options; secrets would land in shell history.
- **Vended credentials are fetched once**, at table load. PyIceberg 0.12 does not refresh
  them, so a session that outlives the SAS/STS token (often about 1h) starts failing per
  section. `r` does not help. Restart icefloor.
- **Unity Catalog specifics.** The URI is
  `https://<workspace>/api/2.1/unity-catalog/iceberg-rest` and `warehouse` is the UC
  catalog, so the target is `schema.table`. One config entry covers one UC catalog. The
  service needs `EXTERNAL USE SCHEMA` plus external data access turned on for the
  metastore. Only managed Iceberg tables or Delta with UniForm are visible. UniForm's
  Iceberg metadata is generated asynchronously and can lag the newest Delta commit.
- **ADLS needs `adlfs`.** PyArrow's Azure filesystem ignores the per-account
  `adls.sas-token.<account>` keys that vending produces, hence the guard in
  `_load_from_catalog`.
- **Network.** The machine needs HTTPS to both the catalog host and the storage
  endpoint. If only the first is reachable, the table loads and every section shows a
  `.error` note.

## Things that bit us (don't re-break these)

- `pyarrow`'s `row_group(i).total_byte_size` is the **uncompressed** total. Real on-disk
  size must be summed from `column(j).total_compressed_size`.
- `Snapshot.summary` is a pydantic `Summary`, not a dict — `dict(summary)` raises.
  Use `meta._summary_dict()`.
- Snapshot and parent ids are 64-bit; never run them through a thousands-separator
  formatter. Use `fmt.ident`.
- Sections load on a worker thread (`@work(thread=True)`) so a slow manifest scan can't
  freeze the UI. Mount widgets only via `app.call_from_thread`.
- A section that raises is surfaced as a `.error` note, never as an empty pane — the
  test suite asserts this.
- PyIceberg reads its config **once, at import** (`pyiceberg.catalog._ENV_CONFIG`).
  Tests that set `PYICEBERG_CATALOG__*` env vars must also rebuild that object (see the
  `fixture_catalog` fixture).
- `SqlCatalog` scopes tables by catalog *name*. The fixture creates them under `test`, so
  a config entry for that DB under any other name finds nothing.

## Your notes

<!-- Add requirements, constraints, target environments, catalogs to support,
     metadata you care about, or anything else here. -->
