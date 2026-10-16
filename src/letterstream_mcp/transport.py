"""HTTP transport, split by consequence rather than by verb.

The four methods below exist as four methods, and not as one generic
``post()``, so that "did this code path mail anything?" is answerable by
looking at which method was called:

* :meth:`Transport.submit_preauth` creates a *held* job. It forces
  ``preauth=1`` onto the outgoing form itself, after merging the caller's
  fields, so no caller can turn a submission into an immediate mailing by
  passing a different value.
* :meth:`Transport.release` sends the ``doauth`` argument. This is the only
  method in this package that causes mail to be produced and billed.
* :meth:`Transport.query` performs read-only lookups (status, tracking, proof
  PDF download). It refuses to carry a ``doauth`` field.
* :meth:`Transport.preflight` uploads a PDF to LetterStream's visual
  PreFlight check, which returns the PDF marked up with their envelope windows.
  It uploads under the ``preflight_file`` field, never ``single_file``, and
  refuses ``doauth``, ``preauth`` and every field that describes a mailing
  (``job``, ``to[]``, ``from`` ...), so it cannot be turned into a submission.

Tests substitute a fake implementing this protocol and assert on which of the
four was called. See ``tests/fakes.py``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .errors import TransportError

#: Field name that releases a pre-authorised job into production.
RELEASE_FIELD = "doauth"
#: Field name and value that hold a submission back from production.
PREAUTH_FIELD = "preauth"
PREAUTH_VALUE = "1"
#: Field that selects LetterStream's PreFlight check, and its one documented value.
PREFLIGHT_FIELD = "preflight"
PREFLIGHT_VISUAL = "visual"
#: Multipart field the PreFlight document is uploaded under. Submissions use
#: ``single_file``; a PreFlight upload must never use that name.
PREFLIGHT_FILE_FIELD = "preflight_file"
#: Fields that describe a mailing. A PreFlight request carrying any of them is
#: refused, so it cannot be mistaken by LetterStream (or by a reader) for a
#: submission.
SUBMISSION_FIELDS = frozenset(
    {
        RELEASE_FIELD,
        PREAUTH_FIELD,
        "job",
        "to[]",
        "to",
        "from",
        "pages",
        "mailtype",
        "coversheet",
        "duplex",
        "ink",
        "paper",
        "returnenv",
    }
)


@runtime_checkable
class Transport(Protocol):
    """The surface :class:`letterstream_mcp.client.LetterStreamClient` uses."""

    def submit_preauth(
        self,
        fields: dict[str, Any],
        *,
        document_name: str,
        document_bytes: bytes,
    ) -> str:
        """Upload a document as a held job. Must not release it."""

    def release(self, fields: dict[str, Any]) -> str:
        """Release a previously held job. This is the mailing call."""

    def query(self, fields: dict[str, Any]) -> bytes:
        """Read-only lookup. Must not release anything."""

    def preflight(
        self,
        fields: dict[str, Any],
        *,
        document_name: str,
        document_bytes: bytes,
    ) -> bytes:
        """Upload a PDF for a visual PreFlight check. Creates no job."""


class HttpTransport:
    """Real transport over ``requests``.

    Instantiating this object performs no I/O. Nothing is sent until one of the
    four methods is called.
    """

    def __init__(self, base_url: str, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def _session(self):  # pragma: no cover - thin import shim
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise TransportError(
                "The 'requests' package is required for live calls. "
                "Install it with: pip install requests"
            ) from exc
        return requests

    def submit_preauth(
        self,
        fields: dict[str, Any],
        *,
        document_name: str,
        document_bytes: bytes,
    ) -> str:
        outgoing = dict(fields)
        if RELEASE_FIELD in outgoing:
            raise TransportError(
                f"submit_preauth was handed a {RELEASE_FIELD!r} field. That "
                "field releases mail and does not belong on a submission."
            )
        # Set last, so a caller-supplied value cannot survive. A submission
        # made through this transport is always a held job.
        outgoing[PREAUTH_FIELD] = PREAUTH_VALUE

        # to[] is a repeated form field; requests needs it as a list of pairs.
        payload = _as_pairs(outgoing)
        requests = self._session()
        try:
            response = requests.post(
                self.base_url,
                data=payload,
                files=[("single_file", (document_name, document_bytes, "application/pdf"))],
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise TransportError(
                f"Submission request to {self.base_url} failed: {exc}"
            ) from exc
        return response.text

    def release(self, fields: dict[str, Any]) -> str:
        if RELEASE_FIELD not in fields:
            raise TransportError(
                f"release() requires a {RELEASE_FIELD!r} field; refusing to "
                "send an unrecognised request to the mailing endpoint."
            )
        requests = self._session()
        try:
            response = requests.post(
                self.base_url,
                data=_as_pairs(fields),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"Authorisation request to {self.base_url} failed: {exc}"
            ) from exc
        return response.text

    def query(self, fields: dict[str, Any]) -> bytes:
        if RELEASE_FIELD in fields:
            raise TransportError(
                f"query() was handed a {RELEASE_FIELD!r} field. Read-only "
                "lookups never release mail."
            )
        requests = self._session()
        try:
            response = requests.post(
                self.base_url,
                data=_as_pairs(fields),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"Query request to {self.base_url} failed: {exc}"
            ) from exc
        return response.content

    def preflight(
        self,
        fields: dict[str, Any],
        *,
        document_name: str,
        document_bytes: bytes,
    ) -> bytes:
        check_preflight_fields(fields)
        requests = self._session()
        try:
            response = requests.post(
                self.base_url,
                data=_as_pairs(fields),
                files=[
                    (
                        PREFLIGHT_FILE_FIELD,
                        (document_name, document_bytes, "application/pdf"),
                    )
                ],
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"PreFlight request to {self.base_url} failed: {exc}"
            ) from exc
        # Observed live: the body is raw PDF bytes even though the response is
        # labelled text/html, so read .content, not .text.
        return response.content


def check_preflight_fields(fields: dict[str, Any]) -> None:
    """Refuse a PreFlight request that is anything other than a PreFlight request.

    Shared by the real transport and the test fake, so the fake cannot accept a
    request the real transport would refuse.
    """
    if fields.get(PREFLIGHT_FIELD) != PREFLIGHT_VISUAL:
        raise TransportError(
            f"preflight() requires {PREFLIGHT_FIELD}={PREFLIGHT_VISUAL!r}; refusing "
            "to upload a document without it."
        )
    carried = sorted(SUBMISSION_FIELDS & set(fields))
    if carried:
        raise TransportError(
            f"preflight() was handed submission field(s) {carried}. A PreFlight "
            "check creates no job and must not look like one."
        )


def _as_pairs(fields: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten a field dict to form pairs, expanding lists into repeats."""
    pairs: list[tuple[str, str]] = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(item)) for item in value)
        else:
            pairs.append((key, str(value)))
    return pairs
