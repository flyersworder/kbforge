import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from kbforge_sql.errors import SqlSourceError
from kbforge_sql.values import canonical


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        (Decimal("1.50"), "1.5"),
        (Decimal("1.5"), "1.5"),
        (Decimal("100"), "100"),
        (Decimal("1E+2"), "100"),
        (Decimal("0.00"), "0"),
        (0.1, "0.1"),
        ("  café\r\nline two  \n", "  café\nline two"),
        ("café", "café"),
        (date(2026, 9, 18), "2026-09-18"),
        (datetime(2026, 9, 18, 12, 0), "2026-09-18T12:00:00"),
        (
            datetime(2026, 9, 18, 14, 0, tzinfo=timezone(timedelta(hours=2))),
            "2026-09-18T12:00:00+00:00",
        ),
        (datetime(2026, 9, 18, 12, 0, tzinfo=UTC), "2026-09-18T12:00:00+00:00"),
        (
            UUID("12345678-1234-5678-1234-567812345678"),
            "12345678-1234-5678-1234-567812345678",
        ),
    ],
)
def test_canonical_forms(value, expected):
    assert canonical(value, "c") == expected


def test_bool_is_not_collapsed_into_int():
    assert canonical(True, "c") is True


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf, Decimal("NaN"), Decimal("Infinity")],
)
def test_non_finite_numbers_are_rejected_naming_the_column(value):
    with pytest.raises(SqlSourceError, match="column 'price'"):
        canonical(value, "price")


def test_bytes_are_rejected_with_the_remedy():
    with pytest.raises(SqlSourceError) as exc:
        canonical(b"\x00", "blob")
    assert str(exc.value) == (
        "column 'blob' holds a bytes value, which has no canonical text form; "
        "add it to 'exclude' or cast it in the query"
    )
