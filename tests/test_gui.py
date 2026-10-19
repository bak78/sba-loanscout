"""The window's form: inputs become run options, and the column headings tick
or clear their whole column."""

import tkinter as tk

import pytest

from sba_loanscout.gui import ALL_STATES_LABEL, IndustryColumn, options_from_inputs


def test_inputs_become_run_options():
    options = options_from_inputs("MD", "212, 207", "500", ["Home care", "Dental"], dry_run=True)
    assert (options.state, options.zip_prefixes, options.limit) == ("MD", ("212", "207"), 500)
    assert options.fields == ("Home care", "Dental") and options.dry_run


def test_all_states():
    assert options_from_inputs(ALL_STATES_LABEL, "", "50", ["Dental"], False).state == "ALL"


@pytest.mark.parametrize("state, zips, limit, fields, message", [
    ("MD", "", "abc", ["Dental"], "whole number"),
    ("MD", "", "0", ["Dental"], "at least 1"),
    ("MD", "", "50", [], "Tick at least one"),
    ("MD", "2", "50", ["Dental"], "2 to 5 digits"),
    (ALL_STATES_LABEL, "212", "50", ["Dental"], "need a state"),
])
def test_bad_inputs_get_a_plain_message(state, zips, limit, fields, message):
    with pytest.raises(ValueError, match=message):
        options_from_inputs(state, zips, limit, fields, False)


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    window.withdraw()
    yield window
    window.destroy()


def test_the_heading_ticks_and_clears_its_whole_column(root):
    column = IndustryColumn(root, "Construction", ["Roofing", "Electrical", "Paving"], ticked=set())
    assert column.ticked() == [] and not column.all.get()
    column.all.set(True)
    column.toggle_all()
    assert column.ticked() == ["Roofing", "Electrical", "Paving"]
    column.all.set(False)
    column.toggle_all()
    assert column.ticked() == []


def test_the_heading_shows_ticked_only_while_every_line_is(root):
    column = IndustryColumn(root, "Healthcare", ["Home care", "Dental"], ticked={"Home care", "Dental"})
    assert column.all.get()
    column.items[1][1].set(False)
    column.sync()
    assert not column.all.get() and column.ticked() == ["Home care"]
