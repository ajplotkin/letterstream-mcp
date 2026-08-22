"""HTTP transport, split by consequence rather than by verb.

The three methods below exist as three methods, and not as one generic
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

Tests substitute a fake implementing this protocol and assert on which of the
three was called. See ``tests/fakes.py``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .errors import TransportError

#: Field name that releases a pre-authorised job into production.
RELEASE_FIELD = "doauth"
#: Field name and value that hold a submission back from production.
PREAUTH_FIELD = "preauth"
PREAUTH_VALUE = "1"


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


class HttpTransport:
    """Real transport over ``requests``.

    Instantiating this object performs no I/O. Nothing is sent until one of the
    three methods is called.
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


def _as_pairs(fields: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten a field dict to form pairs, expanding lists into repeats."""
    pairs: list[tuple[str, str]] = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(item)) for item in value)
        else:
            pairs.append((key, str(value)))
    return pairs
