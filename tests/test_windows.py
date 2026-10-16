"""The local envelope-window heuristic: what it flags, and what it never does.

Every page here is built by :mod:`fixtures.pdf_builder` at known positions, so
each test states where the text is and which zone that puts it in. The clean
page is checked alongside the failing ones, so an implementation that always
reports problems — or never does — fails one side or the other.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")

from conftest import make_config  # noqa: E402
from fixtures.pdf_builder import Page, build_pdf, calibration_grid  # noqa: E402
from letterstream_mcp import windows  # noqa: E402
from letterstream_mcp.errors import ValidationError  # noqa: E402
from letterstream_mcp.store import Store  # noqa: E402
from letterstream_mcp.toolset import ToolSet  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _sender(page: Page) -> Page:
    return page.text(1.0, 0.45, "Sender Name").text(1.0, 0.62, "1 Sender St").text(
        1.0, 0.79, "Senderville, NY 10001"
    )


def _recipient(page: Page) -> Page:
    return page.text(1.0, 2.30, "Recipient Name").text(1.0, 2.47, "2 Recipient Ave").text(
        1.0, 2.64, "Recipientton, CA 90001"
    )


def _write(tmp_path: Path, page: Page, name: str = "page.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(build_pdf(page))
    return path


def test_a_page_laid_out_inside_the_safe_zones_reports_no_problems(tmp_path):
    report = windows.check_windows(_write(tmp_path, _recipient(_sender(Page()))))

    assert report["problems"] == []
    assert [ln["text"] for ln in report["zones"]["to"]["text_lines"]] == [
        "Recipient Name",
        "2 Recipient Ave",
        "Recipientton, CA 90001",
    ]
    assert [ln["text"] for ln in report["zones"]["from"]["text_lines"]] == [
        "Sender Name",
        "1 Sender St",
        "Senderville, NY 10001",
    ]


def test_a_date_at_the_top_of_the_to_window_is_flagged(tmp_path):
    """The failure seen on a real letter template: the date shows in the TO window."""
    page = _recipient(_sender(Page())).text(1.0, 2.0, "October 16, 2026")
    report = windows.check_windows(_write(tmp_path, page))

    assert report["zones"]["to"]["lines_outside_safe_zone"] == ["October 16, 2026"]
    assert any("October 16, 2026" in p and "TO window" in p for p in report["problems"])
    assert report["problem_count"] == 1


def test_an_address_line_falling_below_the_to_window_is_flagged(tmp_path):
    page = _sender(Page()).text(1.0, 2.75, "Recipient Name").text(1.0, 3.10, "2 Recipient Ave")
    report = windows.check_windows(_write(tmp_path, page))

    assert report["zones"]["to"]["lines_outside_safe_zone"] == ["2 Recipient Ave"]


def test_text_in_the_cert_area_is_flagged(tmp_path):
    page = _recipient(_sender(Page())).text(5.0, 1.0, "LETTERHEAD")
    report = windows.check_windows(_write(tmp_path, page))

    assert [ln["text"] for ln in report["zones"]["cert"]["text_lines"]] == ["LETTERHEAD"]
    assert report["problems"] == ["'LETTERHEAD' is in the CERT barcode area, which must be blank."]


def test_a_drawing_in_the_cert_area_is_flagged(tmp_path):
    page = _recipient(_sender(Page())).rect(5.0, 1.0, 6.0, 1.5)
    report = windows.check_windows(_write(tmp_path, page))

    assert report["zones"]["cert"]["drawings"] == 1
    assert any("CERT barcode area" in p for p in report["problems"])


def test_a_drawing_just_outside_the_cert_area_is_not_flagged(tmp_path):
    """Pins the boundary, so a check that flagged any drawing anywhere would fail."""
    page = _recipient(_sender(Page())).rect(5.0, 2.30, 6.0, 2.60)
    report = windows.check_windows(_write(tmp_path, page))

    assert report["zones"]["cert"]["drawings"] == 0
    assert report["problems"] == []


def test_an_empty_to_window_is_flagged(tmp_path):
    report = windows.check_windows(_write(tmp_path, _sender(Page())))
    assert any("No text shows through the TO window" in p for p in report["problems"])


def test_text_outside_the_minimum_margins_is_flagged(tmp_path):
    page = _recipient(_sender(Page())).text(0.1, 6.0, "edge")
    report = windows.check_windows(_write(tmp_path, page))
    assert report["problems"] == ["'edge' is outside LetterStream's minimum margins."]


def test_a_page_that_is_not_letter_size_is_refused(tmp_path):
    page = _recipient(_sender(Page(width_in=8.27, height_in=11.69)))
    with pytest.raises(ValidationError, match=r"letter size"):
        windows.check_windows(_write(tmp_path, page))


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path):
    path = tmp_path / "x.pdf"
    path.write_bytes(b"not a pdf at all")
    with pytest.raises(ValidationError, match=r"Could not read"):
        windows.check_windows(path)


def test_missing_pdfplumber_is_a_sentence_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    with pytest.raises(windows.GeometryUnavailable, match=r"pip install -e '\.\[geometry\]'"):
        windows.check_windows(_write(tmp_path, Page()))


def test_the_report_is_labelled_a_heuristic_and_never_changes_coversheet(tmp_path):
    report = windows.check_windows(_write(tmp_path, _recipient(_sender(Page()))))
    assert report["heuristic"] is True
    assert report["coversheet_changed"] is False
    assert "Unofficial heuristic" in report["disclaimer"]


def test_a_clean_window_check_does_not_change_the_coversheet_sent(
    state_dir, transport, tmp_path
):
    """A clean report is not permission for coversheet='N'. Submit still sends 'Y'."""
    from fixtures.synthetic_recipients import RECIPIENT_DICTS, SENDER_DICT

    document = _write(tmp_path, _recipient(_sender(Page())))
    toolset = ToolSet(
        config=make_config(state_dir, live=True), transport=transport, store=Store(state_dir)
    )
    checked = toolset.letterstream_check_windows(document_path=str(document))
    assert checked["ok"] is True and checked["windows"]["problems"] == []
    assert transport.total_calls == 0

    toolset.letterstream_submit(
        job_name="TESTJOB0001",
        document_path=str(document),
        pages=1,
        sender=SENDER_DICT,
        recipients=RECIPIENT_DICTS,
        mail_type="certified",
    )
    assert transport.submit_calls[0]["fields"]["coversheet"] == "Y"


def test_the_window_check_tool_works_in_dry_run_without_the_transport(
    state_dir, transport, tmp_path
):
    document = _write(tmp_path, _sender(Page()))
    toolset = ToolSet(
        config=make_config(state_dir, live=False), transport=transport, store=Store(state_dir)
    )
    result = toolset.letterstream_check_windows(document_path=str(document))
    assert result["ok"] is True
    assert result["windows"]["problem_count"] >= 1
    assert transport.total_calls == 0


def test_cli_check_windows_needs_no_credentials(tmp_path, monkeypatch, capsys):
    from letterstream_mcp import cli

    def no_config(**_):
        raise AssertionError("check-windows must not load configuration")

    monkeypatch.setattr(cli, "load_config", no_config)
    document = _write(tmp_path, _recipient(_sender(Page())))
    assert cli.main(["check-windows", str(document)]) == cli.EXIT_OK
    assert '"problem_count": 0' in capsys.readouterr().out


def test_cli_check_windows_reports_an_unreadable_file_as_an_error(tmp_path, capsys):
    from letterstream_mcp import cli

    path = tmp_path / "x.pdf"
    path.write_bytes(b"nope")
    assert cli.main(["check-windows", str(path)]) == cli.EXIT_ERROR
    assert '"error_type": "ValidationError"' in capsys.readouterr().out


def test_the_committed_calibration_grid_is_reproducible():
    """The fixture is generated, not hand-edited: regenerating it gives the same bytes."""
    assert (FIXTURES / "calibration_grid.pdf").read_bytes() == calibration_grid()


def test_the_calibration_grid_rules_every_quarter_inch():
    import pdfplumber

    with pdfplumber.open(str(FIXTURES / "calibration_grid.pdf")) as pdf:
        page = pdf.pages[0]
        assert (page.width, page.height) == (612, 792)
        verticals = sorted({round(float(ln["x0"]), 2) for ln in page.lines if ln["x0"] == ln["x1"]})
    assert verticals == [round(i * 18.0, 2) for i in range(35)]


# ---- submit(window_check=...): opt-in only -------------------------------------


def _submit(toolset: ToolSet, document: Path, **overrides):
    from fixtures.synthetic_recipients import RECIPIENT_DICTS, SENDER_DICT

    args = {
        "job_name": "TESTJOB0001",
        "document_path": str(document),
        "pages": 1,
        "sender": SENDER_DICT,
        "recipients": RECIPIENT_DICTS,
        "mail_type": "certified",
    }
    args.update(overrides)
    return toolset.letterstream_submit(**args)


def _live_toolset(state_dir, transport) -> ToolSet:
    return ToolSet(
        config=make_config(state_dir, live=True), transport=transport, store=Store(state_dir)
    )


def _bad_page() -> Page:
    """The real template failure: a date in the TO window, letterhead in CERT."""
    return _recipient(_sender(Page())).text(1.0, 2.0, "October 16, 2026").text(5.0, 1.0, "LETTERHEAD")


def test_window_check_is_off_by_default_and_never_runs(state_dir, transport, tmp_path, monkeypatch):
    """Without the flag the heuristic is not even called, whatever the layout."""
    from letterstream_mcp import toolset as toolset_module

    def must_not_run(_):
        raise AssertionError("window check ran without being asked for")

    monkeypatch.setattr(toolset_module, "check_windows", must_not_run)
    result = _submit(_live_toolset(state_dir, transport), _write(tmp_path, _bad_page()), coversheet="N")

    assert result["ok"] is True
    assert "window_check" not in result
    assert transport.submit_calls[0]["fields"]["coversheet"] == "N"


def test_window_check_with_coversheet_n_refuses_a_bad_layout_before_the_transport(
    state_dir, transport, tmp_path
):
    result = _submit(
        _live_toolset(state_dir, transport),
        _write(tmp_path, _bad_page()),
        coversheet="N",
        window_check=True,
    )
    assert result["ok"] is False
    assert result["error_type"] == "WindowCheckFailed"
    assert "October 16, 2026" in result["error"] and "LETTERHEAD" in result["error"]
    assert transport.total_calls == 0


def test_window_check_with_coversheet_y_reports_but_still_submits(state_dir, transport, tmp_path):
    """With a coversheet the page does not show through, so the report is advisory."""
    result = _submit(
        _live_toolset(state_dir, transport),
        _write(tmp_path, _bad_page()),
        coversheet="Y",
        window_check=True,
    )
    assert result["ok"] is True
    assert result["window_check"]["problem_count"] == 2
    assert transport.submit_calls[0]["fields"]["coversheet"] == "Y"


def test_window_check_with_coversheet_n_passes_a_clean_layout_without_changing_it(
    state_dir, transport, tmp_path
):
    result = _submit(
        _live_toolset(state_dir, transport),
        _write(tmp_path, _recipient(_sender(Page()))),
        coversheet="N",
        window_check=True,
    )
    assert result["ok"] is True
    assert result["window_check"]["problems"] == []
    assert transport.submit_calls[0]["fields"]["coversheet"] == "N"


def test_window_check_refuses_in_dry_run_too(state_dir, transport, tmp_path):
    """The check is local, so a dry run exercises it rather than skipping it."""
    toolset = ToolSet(
        config=make_config(state_dir, live=False), transport=transport, store=Store(state_dir)
    )
    result = _submit(toolset, _write(tmp_path, _bad_page()), coversheet="N", window_check=True)
    assert result["error_type"] == "WindowCheckFailed"
    assert transport.total_calls == 0


def test_cli_window_check_refusal_exits_as_a_refusal(tmp_path, monkeypatch, capsys, state_dir):
    import json as _json

    from fixtures.synthetic_recipients import RECIPIENT_DICTS, SENDER_DICT
    from letterstream_mcp import cli

    monkeypatch.setattr(cli, "load_config", lambda **_: make_config(state_dir, live=False))
    document = _write(tmp_path, _bad_page())
    code = cli.main(
        [
            "submit",
            "--job-name", "TESTJOB0001",
            "--document", str(document),
            "--pages", "1",
            "--sender", _json.dumps(SENDER_DICT),
            "--recipient", _json.dumps(RECIPIENT_DICTS[0]),
            "--coversheet", "N",
            "--window-check",
        ]
    )
    assert code == cli.EXIT_REFUSED
    assert '"error_type": "WindowCheckFailed"' in capsys.readouterr().out
