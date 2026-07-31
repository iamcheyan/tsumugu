"""Reusable modal dialogs for the TUI."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class InputDialog(ModalScreen[str | None]):
    """A modal that prompts for a text value. Returns the string or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, prompt: str, initial_value: str = ""):
        super().__init__()
        self._prompt = prompt
        self._initial = initial_value

    def compose(self) -> ComposeResult:
        with Vertical(id="input-dialog"):
            yield Label(self._prompt)
            yield Input(value=self._initial, id="dialog-input")
            with Horizontal():
                yield Button("OK", id="dialog-ok", variant="primary")
                yield Button("Cancel", id="dialog-cancel")

    def on_mount(self) -> None:
        self.query_one("#dialog-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        val = self.query_one("#dialog-input", Input).value.strip()
        self.dismiss(val if val else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    """A yes/no confirmation modal. Returns True or False."""

    BINDINGS = [Binding("escape", "cancel", "No", priority=True), Binding("enter", "yes", "Yes", priority=True)]

    def __init__(self, prompt: str):
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._prompt)
            with Horizontal():
                yield Button("Yes", id="confirm-yes", variant="error")
                yield Button("No", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)
