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

## Your notes

<!-- Add requirements, constraints, target environments, catalogs to support,
     metadata you care about, or anything else here. -->
