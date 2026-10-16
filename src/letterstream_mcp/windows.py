"""Local envelope-window check: an unofficial heuristic, not LetterStream's.

With ``coversheet = "N"`` the first page of your PDF shows through the
envelope windows, and USPS reads whatever address it sees there. This module
reads the first page locally and reports what falls inside the windows and the
area LetterStream reserves for the certified-mail barcode. It makes no network
call and works in dry run.

Where the rectangles come from, stated exactly: they were read on 2026-10-16
from the vector drawings in the PDF that LetterStream's visual PreFlight
returned for a letter-size calibration grid. That result is committed as
``tests/fixtures/preflight_calibration_result.pdf`` (it identifies itself as
"LetterStream Preflight v4 revised 06/18/2024"), and ``tests/test_windows.py``
matches every zone below to exactly one of its drawn boxes, and every box to a
zone. A redraw of the grid that was sent is
``tests/fixtures/calibration_grid.pdf``. LetterStream labels the red boxes TO
and FROM and the green box CERT. Reading the thinner green box *inside* each
red window as the safe zone for text is this project's interpretation and has
not been confirmed by LetterStream. PreFlight itself (:meth:`letterstream_mcp.gate.MailGate.preflight`)
is the authoritative check.

Nothing here changes ``coversheet``. A clean report is not permission to set it
to ``"N"``; see the README, "Envelope windows".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import LetterStreamError, ValidationError

POINTS_PER_INCH = 72.0

#: Letter size, in inches. The measurements below were taken on this size.
LETTER_WIDTH_IN = 8.5
LETTER_HEIGHT_IN = 11.0
#: How far a page may differ from letter size before the check refuses.
PAGE_SIZE_TOLERANCE_IN = 0.05


class GeometryUnavailable(LetterStreamError):
    """The optional PDF-reading dependency for the window check is not installed."""


@dataclass(frozen=True)
class Zone:
    """A rectangle in inches, measured from the top-left corner of the page."""

    name: str
    x0: float
    y0: float
    x1: float
    y1: float

    def intersects(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        return x0 < self.x1 and x1 > self.x0 and y0 < self.y1 and y1 > self.y0

    def contains(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        return x0 >= self.x0 and x1 <= self.x1 and y0 >= self.y0 and y1 <= self.y1

    def as_dict(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


# Read from that PreFlight result; rounded to 0.01 in. Openings are
# LetterStream's red boxes, safe zones the green boxes inside them, CERT the
# green box at the top right.
TO_OPENING = Zone("TO window", 0.72, 1.89, 4.15, 3.07)
TO_SAFE = Zone("TO safe zone", 0.875, 2.04, 4.00, 2.92)
FROM_OPENING = Zone("FROM window", 0.72, 0.17, 3.72, 1.19)
FROM_SAFE = Zone("FROM safe zone", 0.875, 0.17, 3.57, 1.04)
CERT_AREA = Zone("CERT barcode area", 4.75, 0.69, 8.31, 2.19)
#: The grey box LetterStream draws, labelled "Minimum Margins (.25" left/right
#: .20" top/bottom)". As drawn it is 0.17 in from the top and 0.19 in from the
#: bottom, not 0.20; the drawn box is used, like every other zone here.
MARGIN_BOX = Zone("minimum margins", 0.25, 0.17, 8.25, 10.81)

DISCLAIMER = (
    "Unofficial heuristic. Window positions were measured from one LetterStream "
    "PreFlight result on 2026-10-16; the safe zones are this project's reading of "
    "it, not a LetterStream specification. Run PreFlight for the authoritative "
    "check. This never changes coversheet, and coversheet='Y' remains the "
    "recommendation."
)


def _load_pdfplumber():
    try:
        import pdfplumber
    except ImportError as exc:
        raise GeometryUnavailable(
            "The envelope-window check needs pdfplumber. Install it with "
            "pip install -e '.[geometry]' from the checkout, or pip install pdfplumber."
        ) from exc
    return pdfplumber


def _inches(obj: dict[str, Any]) -> tuple[float, float, float, float]:
    """pdfplumber's top-left-origin bbox, in inches."""
    return (
        float(obj["x0"]) / POINTS_PER_INCH,
        float(obj["top"]) / POINTS_PER_INCH,
        float(obj["x1"]) / POINTS_PER_INCH,
        float(obj["bottom"]) / POINTS_PER_INCH,
    )


def _lines_of_text(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group words into visual lines by their top coordinate."""
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda w: (round(float(w["top"]), 0), float(w["x0"]))):
        x0, y0, x1, y1 = _inches(word)
        if lines and abs(lines[-1]["y0"] - y0) < 2.0 / POINTS_PER_INCH:
            line = lines[-1]
            line["text"] += " " + word["text"]
            line["x0"], line["x1"] = min(line["x0"], x0), max(line["x1"], x1)
            line["y0"], line["y1"] = min(line["y0"], y0), max(line["y1"], y1)
        else:
            lines.append({"text": word["text"], "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return lines


def _rounded(box: dict[str, Any]) -> dict[str, Any]:
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in box.items()}


def check_windows(pdf_path: Path | str) -> dict[str, Any]:
    """Report what on the first page falls in the envelope windows and CERT area.

    Returns ``problems`` (a list of sentences) plus, per zone, the text lines
    and the count of drawings and images that touch it. An empty ``problems``
    list means this heuristic found nothing, not that the page is safe to mail
    without a coversheet.
    """
    pdfplumber = _load_pdfplumber()
    path = Path(pdf_path).expanduser()
    if not path.is_file():
        raise ValidationError(f"No PDF at {path}.")

    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:  # noqa: BLE001 - pdfminer raises many types
        raise ValidationError(f"Could not read {path} as a PDF: {exc}") from exc
    with pdf:
        if not pdf.pages:
            raise ValidationError(f"{path} has no pages.")
        page = pdf.pages[0]
        width_in = float(page.width) / POINTS_PER_INCH
        height_in = float(page.height) / POINTS_PER_INCH
        if (
            abs(width_in - LETTER_WIDTH_IN) > PAGE_SIZE_TOLERANCE_IN
            or abs(height_in - LETTER_HEIGHT_IN) > PAGE_SIZE_TOLERANCE_IN
        ):
            raise ValidationError(
                f"The first page is {width_in:.2f} x {height_in:.2f} in. The window "
                "positions were measured on letter size (8.5 x 11 in) only."
            )
        lines = _lines_of_text(page.extract_words(keep_blank_chars=False))
        drawings = [
            _inches(o)
            for o in (list(page.rects) + list(page.lines) + list(page.curves))
        ]
        images = [_inches(o) for o in page.images]

    problems: list[str] = []
    zones: dict[str, Any] = {}

    def touching(zone: Zone, boxes: list[tuple[float, float, float, float]]) -> int:
        return sum(1 for box in boxes if zone.intersects(*box))

    for key, opening, safe in (("to", TO_OPENING, TO_SAFE), ("from", FROM_OPENING, FROM_SAFE)):
        visible = [ln for ln in lines if opening.intersects(ln["x0"], ln["y0"], ln["x1"], ln["y1"])]
        clipped = [ln for ln in visible if not safe.contains(ln["x0"], ln["y0"], ln["x1"], ln["y1"])]
        zones[key] = {
            "opening": opening.as_dict(),
            "safe_zone": safe.as_dict(),
            "text_lines": [_rounded(ln) for ln in visible],
            "lines_outside_safe_zone": [ln["text"] for ln in clipped],
            "drawings": touching(opening, drawings),
            "images": touching(opening, images),
        }
        if not visible:
            problems.append(
                f"No text shows through the {opening.name}. Without a coversheet "
                "the envelope would carry no readable address there."
            )
        for ln in clipped:
            problems.append(
                f"{ln['text']!r} shows through the {opening.name} but is not wholly "
                f"inside its safe zone; it may be clipped or misread."
            )

    cert_lines = [ln for ln in lines if CERT_AREA.intersects(ln["x0"], ln["y0"], ln["x1"], ln["y1"])]
    cert_drawings = touching(CERT_AREA, drawings)
    cert_images = touching(CERT_AREA, images)
    zones["cert"] = {
        "area": CERT_AREA.as_dict(),
        "text_lines": [_rounded(ln) for ln in cert_lines],
        "drawings": cert_drawings,
        "images": cert_images,
    }
    for ln in cert_lines:
        problems.append(f"{ln['text']!r} is in the CERT barcode area, which must be blank.")
    if cert_drawings or cert_images:
        problems.append(
            f"{cert_drawings} drawing(s) and {cert_images} image(s) touch the CERT "
            "barcode area, which must be blank."
        )

    outside = [
        ln["text"]
        for ln in lines
        if not MARGIN_BOX.contains(ln["x0"], ln["y0"], ln["x1"], ln["y1"])
    ]
    for text in outside:
        problems.append(f"{text!r} is outside LetterStream's minimum margins.")

    return {
        "heuristic": True,
        "disclaimer": DISCLAIMER,
        "document": str(path),
        "page_checked": 1,
        "page_size_in": {"width": round(width_in, 2), "height": round(height_in, 2)},
        "zones": zones,
        "problems": problems,
        "problem_count": len(problems),
        "coversheet_changed": False,
    }
