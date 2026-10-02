"""Textual application shell: navigation, drill-down and key handling."""

from __future__ import annotations

from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
)

from . import meta, views
from .meta import Block
from .views import BlockTable

HELP = """\
[b]Navigation[/b]
  up / down       move inside the focused pane
  tab             next pane
  enter           open the highlighted Parquet file
  escape          clear the filter, else go back a level
  ctrl+left       go back a level

[b]Views[/b]
  /               filter the visible rows
  s               scope file views to the highlighted snapshot
  S               reset the snapshot scope to current
  r               re-read this section from storage
  y               copy the highlighted row's key

[b]App[/b]
  ?               this help
  t               toggle light / dark
  q               quit
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,q,question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(HELP, id="help-body")


class BrowseScreen(Screen[None]):
    """One source (Iceberg table, Parquet file, Parquet dir) in a nav + body."""

    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "back", "Back/clear"),
        Binding("ctrl+left", "pop", "Back", show=False),
        Binding("s", "scope_snapshot", "Scope snapshot"),
        Binding("S", "reset_snapshot", "Current snapshot", show=False),
        Binding("r", "reload", "Reload"),
        Binding("y", "copy_key", "Copy key", show=False),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, source: Any, section: str | None = None):
        super().__init__()
        self.source = source
        self.sections = source.sections()
        keys = [s.key for s in self.sections]
        self.section_key = section if section in keys else (keys[0] if keys else "")
        self.needle = ""
        self._cache: dict[str, list[Block]] = {}
        self._errors: dict[str, str] = {}

    # -- layout ------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield OptionList(*[s.label for s in self.sections], id="nav")
            with Vertical(id="main"):
                yield Static("", id="crumb")
                yield Input(placeholder="filter rows…", id="filter")
                yield VerticalScroll(id="body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#filter", Input).display = False
        nav = self.query_one("#nav", OptionList)
        keys = [s.key for s in self.sections]
        nav.highlighted = keys.index(self.section_key) if self.section_key in keys else 0
        nav.focus()
        self.load_section()

    # -- rendering --------------------------------------------------------- #

    def _crumb(self) -> str:
        label = next(
            (s.label for s in self.sections if s.key == self.section_key),
            self.section_key,
        )
        parts = [f"[b]{self.source.title}[/b]", label]
        snap = getattr(self.source, "snapshot_id", None)
        if snap is not None:
            parts.append(f"[yellow]snapshot {snap}[/yellow]")
        if self.needle:
            parts.append(f"[cyan]filter “{self.needle}”[/cyan]")
        return "  ›  ".join(parts)

    @work(exclusive=True, thread=True, group="section")
    def load_section(self, force: bool = False) -> None:
        key = self.section_key
        if force or key not in self._cache:
            try:
                blocks, error = self.source.section(key), None
            except Exception as exc:  # a bad manifest must not kill the TUI
                blocks, error = [], f"{type(exc).__name__}: {exc}"
            self.app.call_from_thread(self._store, key, blocks, error)
        self.app.call_from_thread(self.paint)

    def _store(self, key: str, blocks: list[Block], error: str | None) -> None:
        self._cache[key] = blocks
        if error:
            self._errors[key] = error
        else:
            self._errors.pop(key, None)

    def paint(self) -> None:
        self.query_one("#crumb", Static).update(self._crumb())
        body = self.query_one("#body", VerticalScroll)
        body.remove_children()
        error = self._errors.get(self.section_key)
        if error:
            body.mount(Static(f"Could not read this section.\n\n{error}", classes="note error"))
            return
        blocks = self._cache.get(self.section_key)
        if blocks is None:
            body.mount(Static("reading…", classes="note"))
            return
        widgets = views.render_blocks(blocks, self.needle)
        if widgets:
            body.mount_all(widgets)
        else:
            body.mount(Static("nothing to show", classes="note"))
        body.scroll_home(animate=False)

    # -- events ------------------------------------------------------------ #

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index is None:
            return
        key = self.sections[event.option_index].key
        if key != self.section_key:
            self.section_key = key
            self.needle = ""
            filt = self.query_one("#filter", Input)
            filt.value = ""
            filt.display = False
            self.load_section()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.query_one("#body", VerticalScroll).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.needle = event.value
            self.paint()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        tables = self.query(BlockTable)
        if tables:
            tables.first().focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        block = getattr(event.data_table, "block", None)
        if block is None or block.action is None:
            return
        value = self._row_value(event.data_table, event.row_key)
        if value is None:
            return
        if block.action == "parquet":
            self.open_parquet(value)
        elif block.action == "snapshot":
            self._set_snapshot(value)

    # -- actions ----------------------------------------------------------- #

    def action_focus_filter(self) -> None:
        filt = self.query_one("#filter", Input)
        filt.display = True
        filt.focus()

    def action_back(self) -> None:
        filt = self.query_one("#filter", Input)
        if self.needle or filt.display:
            filt.value = ""
            filt.display = False
            self.needle = ""
            self.paint()
            self.query_one("#nav", OptionList).focus()
        else:
            self.action_pop()

    def action_pop(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.notify("Already at the top level.", timeout=2)

    def action_reload(self) -> None:
        self.load_section(force=True)
        self.notify(f"Re-read “{self.section_key}”.", timeout=2)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_scope_snapshot(self) -> None:
        if not hasattr(self.source, "snapshot_id"):
            self.notify("Snapshot scoping only applies to Iceberg tables.", timeout=3)
            return
        table = self._focused_table("snapshot")
        if table is None:
            self.notify("Open the Snapshots view and highlight a row first.", timeout=3)
            return
        value = self._cursor_value(table)
        if value is not None:
            self._set_snapshot(value)

    def action_reset_snapshot(self) -> None:
        if getattr(self.source, "snapshot_id", None) is None:
            return
        self.source.snapshot_id = None
        self._cache.clear()
        self.load_section(force=True)
        self.notify("Back to the current snapshot.", timeout=2)

    def action_copy_key(self) -> None:
        table = self._focused_table()
        if table is None:
            return
        value = self._cursor_value(table)
        if value is None:
            return
        self.app.copy_to_clipboard(str(value))
        self.notify(f"Copied {meta.fmt.short(value, 48)}", timeout=2)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Snapshot scoping is meaningless for a plain Parquet file.
        if action in ("scope_snapshot", "reset_snapshot"):
            return hasattr(self.source, "snapshot_id")
        return True

    # -- helpers ----------------------------------------------------------- #

    def _focused_table(self, action: str | None = None) -> BlockTable | None:
        widget = self.focused
        if isinstance(widget, BlockTable) and (action is None or widget.block.action == action):
            return widget
        for table in self.query(BlockTable):
            if action is None or table.block.action == action:
                return table
        return None

    @staticmethod
    def _row_value(table: DataTable, row_key: Any) -> str | None:
        raw = getattr(row_key, "value", row_key)
        if raw is None:
            return None
        _, _, value = str(raw).partition("|")
        return value or None

    def _cursor_value(self, table: BlockTable) -> str | None:
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return self._row_value(table, row_key)

    def _set_snapshot(self, value: str) -> None:
        try:
            snapshot_id = int(value)
        except ValueError:
            self.notify(f"Not a snapshot id: {value}", severity="warning", timeout=3)
            return
        self.source.snapshot_id = snapshot_id
        self._cache.clear()
        self.load_section(force=True)
        self.notify(f"Scoped to snapshot {snapshot_id}.", timeout=2)

    @work(exclusive=True, thread=True, group="drill")
    def open_parquet(self, path: str) -> None:
        try:
            source = self.source.open_data_file(path)
        except Exception as exc:
            self.app.call_from_thread(
                self.notify,
                f"Could not open {meta.fmt.short(path, 40)}: {exc}",
                severity="error",
                timeout=6,
            )
            return
        self.app.call_from_thread(self.app.push_screen, BrowseScreen(source))


class IcefloorApp(App[None]):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "toggle_theme", "Theme"),
    ]

    def __init__(self, source: Any, section: str | None = None):
        super().__init__()
        self.source = source
        self.start_section = section

    def on_mount(self) -> None:
        self.title = "icefloor"
        self.sub_title = f"{self.source.kind} · {self.source.label}"
        self.push_screen(BrowseScreen(self.source, self.start_section))

    def action_toggle_theme(self) -> None:
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
