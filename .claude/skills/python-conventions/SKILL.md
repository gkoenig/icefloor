---
name: python-conventions
description: Python settings, limits and house rules for the icefloor repo — the interpreter and how it is pinned, uv as the only package entry point, the lint/format/test commands that must pass, dependency policy, typing and error-handling style, and Textual/PyArrow/PyIceberg specifics. Load before writing, editing, running or reviewing any Python in this repo, before adding or upgrading a dependency, and before changing pyproject.toml, uv.lock or .python-version.
---

# Python conventions — icefloor

Edit this file to change how Python work happens here. Sections marked **(fill in)**
are placeholders waiting on your decision; treat an unfilled one as "no constraint yet,
ask before assuming".

## Interpreter

- **Python 3.12**, pinned in `.python-version` and as `requires-python = ">=3.12"`.
- 3.12 is the floor, so these are fair game: PEP 604 `X | None`, PEP 695 type params,
  `typing.override`, `itertools.batched`, f-string nesting, `Enum.__contains__`.
- Still **not** available: PEP 750 t-strings, free-threading guarantees, `except*`
  improvements landed after 3.12. Don't reach for 3.13+ features.
- `from __future__ import annotations` at the top of every module. Keeps annotations
  cheap and lets PyIceberg/PyArrow types stay out of import-time cost.

## Package management — uv only

```bash
uv sync                  # create/refresh .venv from uv.lock
uv add <pkg>             # runtime dependency
uv add --dev <pkg>       # dev-only dependency
uv run <cmd>             # anything that needs the venv
uv python install 3.12   # if the pinned interpreter is missing
```

- Never call `pip`, `python -m venv`, `poetry`, `conda`, or a bare `python`/`python3`.
  A bare `python3` on this machine is the system 3.10 and will not have the deps.
- `uv.lock` is committed. Commit it with every dependency change.
- Don't hand-edit `[project.dependencies]`; let `uv add` do it so the lock stays honest.

## Checks that must pass before you call work done

```bash
uv run ruff check .      # lint; must be clean, no new per-line noqa
uv run ruff format .     # formatter is authoritative, line-length 100
uv run pytest -q         # headless Textual suite, ~5s
```

Ruff config lives in `pyproject.toml`: `select = ["E","F","I","UP","B","SIM","RUF"]`.
`RUF012` is ignored on purpose — Textual's `BINDINGS`/`CSS_PATH` are class-level lists
by API design. If you need another blanket ignore, add it there with a one-line reason
rather than scattering `# noqa`.

## Dependency policy

Current runtime set, deliberately small: `textual`, `pyiceberg`, `pyarrow`.
Dev-only: `pytest`, `pytest-asyncio`, `sqlalchemy` (fixture catalog), `ruff`.

Before adding anything, walk the ladder and stop at the first rung that holds:

1. Is the need real today, or speculative?
2. Does something in this repo already do it?
3. Does the stdlib do it? (`dataclasses`, `functools.lru_cache`, `argparse`,
   `datetime`, `pathlib`, `itertools`, `contextlib` — all preferred.)
4. Does `pyarrow` or `pyiceberg` already expose it? They carry a lot: Arrow compute,
   filesystems, the whole `table.inspect` surface.
5. Only then add a dependency — and say why in the commit.

Specifically: **no pandas, polars, numpy or rich-as-a-direct-dependency.** Rich arrives
through Textual and is used via Textual only. PyIceberg's optional extras (`s3fs`,
`adlfs`, `gcsfs`, `hive`, `glue`) go in as extras when a real target needs them, never
pre-emptively.

## Code style

- Pure extraction stays in `meta.py` and returns plain Python values. Widgets are built
  only in `views.py` and `app.py`. Don't let `rich`/`textual` imports leak into `meta.py`.
- Type-annotate public functions and dataclass fields. Internal locals: only when the
  type isn't obvious. No `Any` where a real type exists.
- `dataclass` over hand-written `__init__` for data holders.
- Comments explain *why*, never *what*. If a line needs a "what" comment, rewrite it.
- No one-implementation abstract base classes, no factories, no plugin registries.
  The three source classes are duck-typed on `sections()`/`section()` on purpose.

## Error handling

- A broken or unreadable artefact must degrade to a visible message, never a traceback
  into the terminal and never a silently empty pane. `BrowseScreen.load_section`
  catches per-section and routes to a `.error` note; keep that contract.
- `except Exception` is acceptable **only** at those display boundaries and when
  probing optional metadata, and it must always surface or log something.
- `meta.load()` raises `LoadError` for anything the user can fix (bad path, wrong file
  type); `cli.main` turns that into exit code 2 with a one-line message.

## Textual specifics

- Import the decorator as `from textual import work` — there is no `textual.work` module.
- Anything that touches storage (manifest scans, opening a remote data file) runs in
  `@work(thread=True)`; mount or notify from a worker only through
  `app.call_from_thread`.
- Tests drive the app with `app.run_test()`, then
  `await app.workers.wait_for_complete()` followed by `await pilot.pause()`. Without
  both, assertions race the worker.
- Styling belongs in `app.tcss`, not in inline `styles.*` assignments.
- `Static` in Textual 8 has no `.renderable`; don't assert on widget internals in tests.
  Assert on CSS classes or widget queries instead.

## PyArrow / PyIceberg traps

- `row_group(i).total_byte_size` is **uncompressed**. Compressed size comes from summing
  `column(j).total_compressed_size`.
- `Snapshot.summary` is a pydantic model, not a mapping — `dict()` on it raises.
- `table.inspect.manifests()` has no `snapshot_id` parameter; `data_files`,
  `delete_files`, `entries`, `files` and `partitions` do.
- Arrow map columns decode to a dict *or* a list of pairs depending on version. Guard
  with `if isinstance(x, list): x = dict(x)`.
- Open Iceberg-referenced files through `table.io.new_input(path).open()` so remote
  filesystems keep working — never `open()` the path directly.

## Testing

- One suite: `tests/test_smoke.py`. It walks every section of every source type and
  asserts nothing errored. Add a section → it is covered automatically.
- `tests/fixture.py` builds real artefacts (a partitioned Iceberg table with five
  snapshots and a schema change, plus loose Parquet). Prefer extending it over mocking;
  mocks hide exactly the library quirks listed above.
- `asyncio_mode = "auto"`, so async tests need no decorator.

## Target environments — (fill in)

<!-- Which OSes/terminals must this work on? True-colour assumed, or do you need to
     survive a 16-colour / no-unicode terminal? Minimum terminal width? -->

## Data and catalogs

Full rationale lives in CLAUDE.md → "Catalog support". The rules for code:

- **Catalogs.** SQL (SQLite) is the tested one. REST is the target for real use, with
  Databricks Unity Catalog on Azure as the reference setup. Glue ships as an extra but is
  untested. Hive and Nessie are unsupported until a real target needs them. Add their
  extra then, not before.
- **Object stores.** Local disk always. ADLS through the `azure` extra (`adlfs`), S3
  through `s3`. GCS has no extra yet.
- **Auth.** Only through PyIceberg config (`~/.pyiceberg.yaml` or
  `PYICEBERG_CATALOG__<NAME>__<KEY>`). Prefer credentials vended by a REST catalog over
  static storage keys. Never add CLI flags or code paths that accept secrets, and never
  log or display a config value that could hold one (`token`, `credential`, `*.sas-token`,
  `*.account-key`, `*secret*`). The Properties section shows *table* properties only.
- **Writes.** None, to any table. Don't call `append`, `overwrite`, `delete`, any
  `commit`/transaction API, or a catalog create/drop/rename. The only exception is
  `tests/fixture.py`, which builds its own throwaway table.
- **Tests stay offline.** Catalog tests use the fixture's SQLite catalog configured
  through env vars, plus monkeypatched errors. Never require a live REST endpoint or cloud
  credentials in `pytest`.
- **Size.** No hard number set yet. Working assumption: thousands of manifests and
  tens of thousands of data files must stay responsive. That means section work stays on
  the worker thread, and nothing loads every manifest when the active section doesn't need it.
  **(fill in a real upper bound once measured against a production table)**

## Limits and hard rules

- the icefloor TUI shall be available as offline-tool as well, so it must not require network access to run, and must not require any credentials to run.