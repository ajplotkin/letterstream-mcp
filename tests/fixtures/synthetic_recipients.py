"""Synthetic addresses. Every value here is invented for the test suite.

The organisation names, street addresses and city names are made up. Nothing
here was taken from, or derived from, any real mailing. These fixtures exist to
be formatted into address strings and hashed; no test posts them anywhere.
"""

from __future__ import annotations

from letterstream_mcp.models import Address, Recipient

SYNTHETIC_SENDER = Address(
    name_1="Testcorp Holdings",
    name_2="Attn Records Desk",
    address_1="1 Example Plaza",
    address_2="Suite 000",
    city="Faketown",
    state="AZ",
    zip_code="99999",
)

SYNTHETIC_RECIPIENT_A = Recipient(
    doc_id="SYNDOC0001",
    address=Address(
        name_1="Placeholder Bank NA",
        name_2="Disputes Department",
        address_1="2 Nowhere Road",
        address_2="",
        city="Faketown",
        state="AZ",
        zip_code="99999",
    ),
)

SYNTHETIC_RECIPIENT_B = Recipient(
    doc_id="SYNDOC0002",
    address=Address(
        name_1="Imaginary Registered Agent LLC",
        name_2="",
        address_1="3 Invented Street",
        address_2="Floor 0",
        city="Othertown",
        state="DE",
        zip_code="99998",
    ),
)

SYNTHETIC_RECIPIENTS = (SYNTHETIC_RECIPIENT_A, SYNTHETIC_RECIPIENT_B)

SENDER_DICT = {
    "name_1": SYNTHETIC_SENDER.name_1,
    "name_2": SYNTHETIC_SENDER.name_2,
    "address_1": SYNTHETIC_SENDER.address_1,
    "address_2": SYNTHETIC_SENDER.address_2,
    "city": SYNTHETIC_SENDER.city,
    "state": SYNTHETIC_SENDER.state,
    "zip_code": SYNTHETIC_SENDER.zip_code,
}

RECIPIENT_DICTS = [
    {
        "doc_id": r.doc_id,
        "name_1": r.address.name_1,
        "name_2": r.address.name_2,
        "address_1": r.address.address_1,
        "address_2": r.address.address_2,
        "city": r.address.city,
        "state": r.address.state,
        "zip_code": r.address.zip_code,
    }
    for r in SYNTHETIC_RECIPIENTS
]
