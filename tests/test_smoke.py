"""One runnable check: every section of every source type renders in the TUI."""

from __future__ import annotations

import os

import pytest
from pyiceberg.utils.config import Config
from textual.widgets import OptionList

from icefloor import meta
from icefloor.app import BrowseScreen, IcefloorApp
from icefloor.meta import IcebergSource, LoadError, ParquetDirSource, ParquetSource, load
from icefloor.views import BlockTable

from .fixture import build_all


@pytest.fixture(scope="module")
def paths(tmp_path_factory) -> dict[str, str]:
    return build_all(str(tmp_path_factory.mktemp("fx")))


def test_load_dispatch(paths):
    assert isinstance(load(paths["parquet"]), ParquetSource)
    assert isinstance(load(paths["parquet_dir"]), ParquetDirSource)
    assert isinstance(load(paths["iceberg"]), IcebergSource)


@pytest.mark.parametrize("which", ["parquet", "parquet_dir", "iceberg"])
async def test_every_section_renders(paths, which):
    source = load(paths[which])
    app = IcefloorApp(source)
    async with app.run_test(size=(180, 50)) as pilot:
        screen = app.screen
        assert isinstance(screen, BrowseScreen)
        nav = screen.query_one("#nav", OptionList)
        for index, section in enumerate(screen.sections):
            nav.highlighted = index
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.section_key == section.key
            body = screen.query_one("#body")
            assert list(body.children), f"{which}/{section.key} rendered nothing"
            # A section that blew up renders a .error note; none should.
            assert not list(body.query(".error")), (
                f"{which}/{section.key}: {screen._errors.get(section.key)}"
            )


async def test_filter_narrows_rows(paths):
    source = load(paths["parquet"])
    app = IcefloorApp(source, section="columns")
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        before = screen.query(BlockTable).first().row_count
        screen.needle = "region"
        screen.paint()
        await pilot.pause()
        after = screen.query(BlockTable).first().row_count
        assert 0 < after < before


@pytest.mark.parametrize("which,section", [("iceberg", "datafiles"), ("parquet_dir", "files")])
async def test_drill_into_parquet_file(paths, which, section):
    source = load(paths[which])
    app = IcefloorApp(source, section=section)
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.screen.query(BlockTable).first()
        assert table.row_count > 0
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, BrowseScreen)
        assert app.screen.source.kind == "parquet"


async def test_snapshot_scoping(paths):
    source = load(paths["iceberg"])
    app = IcefloorApp(source, section="snapshots")
    async with app.run_test(size=(180, 50)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.screen.query(BlockTable).first()
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        first_snapshot = source.table.history()[0].snapshot_id
        assert source.snapshot_id == first_snapshot
        # The oldest snapshot must hold fewer data files than the newest.
        scoped = len(source.section("datafiles")[0].rows)
        source.snapshot_id = None
        assert scoped <= len(source.section("datafiles")[0].rows)


@pytest.fixture
def fixture_catalog(paths, monkeypatch) -> str:
    """Expose the fixture's SQLite catalog `test`, configured purely through env vars."""
    warehouse = os.path.dirname(os.path.dirname(paths["iceberg"]))
    monkeypatch.setenv("PYICEBERG_CATALOG__TEST__TYPE", "sql")
    monkeypatch.setenv("PYICEBERG_CATALOG__TEST__URI", f"sqlite:///{warehouse}/catalog.db")
    # PyIceberg snapshots its config at import time; rebuild it so the env vars count.
    monkeypatch.setattr("pyiceberg.catalog._ENV_CONFIG", Config())
    return "test"


def test_load_from_catalog(fixture_catalog):
    assert isinstance(load("sales.events", fixture_catalog), IcebergSource)


@pytest.mark.parametrize(
    ("target", "catalog"), [("sales.nope", "test"), ("main.sales.events", "test"), ("x.y", "nope")]
)
def test_catalog_errors_are_load_errors(fixture_catalog, target, catalog):
    with pytest.raises(LoadError):
        load(target, catalog)


def test_azure_table_without_adlfs_is_load_error(fixture_catalog, monkeypatch):
    from pyiceberg.catalog.sql import SqlCatalog

    real_load = SqlCatalog.load_table

    def load_on_azure(self, identifier):
        table = real_load(self, identifier)
        table.metadata = table.metadata.model_copy(
            update={"location": "abfss://c@acct.dfs.core.windows.net/t"}
        )
        return table

    monkeypatch.setattr(SqlCatalog, "load_table", load_on_azure)
    monkeypatch.setattr(meta.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(LoadError, match="icefloor\\[azure\\]"):
        load("sales.events", fixture_catalog)


@pytest.mark.parametrize("error", ["UnauthorizedError", "OAuthError", "ForbiddenError"])
def test_catalog_auth_errors_are_load_errors(fixture_catalog, monkeypatch, error):
    from pyiceberg import exceptions
    from pyiceberg.catalog.sql import SqlCatalog

    def refuse(self, identifier):
        raise getattr(exceptions, error)("nope")

    monkeypatch.setattr(SqlCatalog, "load_table", refuse)
    with pytest.raises(LoadError):
        load("sales.events", fixture_catalog)
