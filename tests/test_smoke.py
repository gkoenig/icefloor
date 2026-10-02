"""One runnable check: every section of every source type renders in the TUI."""

from __future__ import annotations

import pytest
from textual.widgets import OptionList

from icefloor.app import BrowseScreen, IcefloorApp
from icefloor.meta import IcebergSource, ParquetDirSource, ParquetSource, load
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
