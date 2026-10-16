"""A minimal, dependency-free PDF writer for the envelope-window fixtures.

Positions are given in inches from the top-left of a letter page, the same
frame the window rectangles in :mod:`letterstream_mcp.windows` use, and
converted here to PDF's bottom-left points. Text is set in Helvetica, one of
the standard fonts every PDF reader supplies, so no font is embedded.

Run as a script to regenerate the calibration grid. The grid actually sent
through LetterStream's PreFlight on 2026-10-16 was drawn with PyMuPDF; this is
a dependency-free redraw of the same ruling (0.25 in steps, heavier every inch,
labelled in inches), not the same bytes::

    python tests/fixtures/pdf_builder.py tests/fixtures/calibration_grid.pdf

The output is byte-for-byte deterministic; a test checks the committed grid
still matches.
"""

from __future__ import annotations

import sys
from pathlib import Path

PT = 72.0
PAGE_W_IN = 8.5
PAGE_H_IN = 11.0


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Page:
    """Collects drawing operators for one page."""

    def __init__(self, width_in: float = PAGE_W_IN, height_in: float = PAGE_H_IN) -> None:
        self.width_in = width_in
        self.height_in = height_in
        self.ops: list[str] = []

    def _y(self, y_in: float) -> float:
        return (self.height_in - y_in) * PT

    def text(self, x_in: float, y_in: float, text: str, size: float = 10) -> "Page":
        """Place text with its baseline at ``y_in`` inches from the top."""
        self.ops.append(
            f"BT /F1 {size:g} Tf {x_in * PT:.2f} {self._y(y_in):.2f} Td "
            f"({_escape(text)}) Tj ET"
        )
        return self

    def line(
        self,
        x0_in: float,
        y0_in: float,
        x1_in: float,
        y1_in: float,
        *,
        width: float = 0.5,
        gray: float = 0.0,
    ) -> "Page":
        self.ops.append(
            f"{width:g} w {gray:g} G {x0_in * PT:.2f} {self._y(y0_in):.2f} m "
            f"{x1_in * PT:.2f} {self._y(y1_in):.2f} l S"
        )
        return self

    def rect(self, x0_in: float, y0_in: float, x1_in: float, y1_in: float) -> "Page":
        """A filled black rectangle."""
        self.ops.append(
            f"0 g {x0_in * PT:.2f} {self._y(y1_in):.2f} "
            f"{(x1_in - x0_in) * PT:.2f} {(y1_in - y0_in) * PT:.2f} re f"
        )
        return self


def build_pdf(page: Page) -> bytes:
    """Serialise one page to a complete PDF with a valid xref table."""
    content = "\n".join(page.ops).encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {page.width_in * PT:g} "
            f"{page.height_in * PT:g}]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>"
        ).encode("latin-1"),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def calibration_grid() -> bytes:
    """A letter page ruled every 0.25 in, heavier every inch, labelled in inches."""
    page = Page()
    steps_x = int(PAGE_W_IN * 4)
    steps_y = int(PAGE_H_IN * 4)
    for i in range(steps_x + 1):
        x = i / 4
        page.line(x, 0, x, PAGE_H_IN, width=0.8 if i % 4 == 0 else 0.3, gray=0 if i % 4 == 0 else 0.6)
    for i in range(steps_y + 1):
        y = i / 4
        page.line(0, y, PAGE_W_IN, y, width=0.8 if i % 4 == 0 else 0.3, gray=0 if i % 4 == 0 else 0.6)
    for k in range(int(PAGE_W_IN) + 1):
        page.text(k + 0.03, 0.14, f'{k}"', size=7)
    for k in range(1, int(PAGE_H_IN)):
        page.text(0.03, k - 0.03, f'{k}"', size=7)
    page.text(2.78, 6.94, "LetterStream PreFlight calibration grid - inches from top-left", size=9)
    return build_pdf(page)


if __name__ == "__main__":  # pragma: no cover - fixture regeneration
    if len(sys.argv) != 2:
        raise SystemExit("usage: pdf_builder.py <out.pdf>")
    Path(sys.argv[1]).write_bytes(calibration_grid())
