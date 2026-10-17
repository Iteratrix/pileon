"""AT Protocol TIDs: record keys that encode their creation time.

A TID is 13 base32-sortable characters holding a 64-bit integer: a zero top
bit, 53 bits of microseconds since the Unix epoch, and a 10-bit clock id.
Clients mint them at creation time, so a follow's or post's rkey dates it (to
client-clock accuracy) without fetching the record. Because TIDs sort in time
order, a TID minted for an instant is also a `listRecords` cursor that starts
just before that instant.
"""

from __future__ import annotations

from datetime import UTC, datetime

ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
_LENGTH = 13
_CLOCK_BITS = 10
_MAX_US = (1 << 53) - 1


class TidError(ValueError):
    """A string that is not a well-formed TID."""


def decode_us(tid: str) -> int:
    """Return the microsecond timestamp encoded in `tid`."""
    if len(tid) != _LENGTH:
        msg = f"TID must be {_LENGTH} characters, got {len(tid)}: {tid!r}"
        raise TidError(msg)
    value = 0
    for char in tid:
        digit = _INDEX.get(char)
        if digit is None:
            msg = f"invalid TID character {char!r} in {tid!r}"
            raise TidError(msg)
        value = value * 32 + digit
    if value >> 63:
        msg = f"TID has its top bit set: {tid!r}"
        raise TidError(msg)
    return value >> _CLOCK_BITS


def encode_us(us: int, clock_id: int = 0) -> str:
    """Return the TID for microsecond timestamp `us`."""
    if not 0 <= us <= _MAX_US:
        msg = f"timestamp out of TID range: {us}"
        raise TidError(msg)
    value = (us << _CLOCK_BITS) | (clock_id & ((1 << _CLOCK_BITS) - 1))
    chars = []
    for _ in range(_LENGTH):
        chars.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def time_of(tid: str) -> datetime:
    """Return the creation instant encoded in `tid`, in UTC."""
    return datetime.fromtimestamp(decode_us(tid) / 1e6, tz=UTC)


def rkey_of(uri: str) -> str:
    """Return the record key (last path segment) of an AT URI."""
    return uri.rsplit("/", 1)[-1]


def for_time(moment: datetime) -> str:
    """Return the TID for `moment` (a cursor that lists records before it)."""
    return encode_us(round(moment.timestamp() * 1e6))
