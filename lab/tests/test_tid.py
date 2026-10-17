from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pileon_lab.tid import TidError, decode_us, encode_us, for_time, rkey_of, time_of


def test_decodes_a_real_follow_rkey():
    # A real follow record's rkey from 2023, as returned by getRelationships.
    assert time_of("3jvdycipe3q2p").year == 2023


def test_rkey_of_takes_the_last_segment():
    uri = "at://did:plc:x/app.bsky.graph.follow/3jvdycipe3q2p"
    assert rkey_of(uri) == "3jvdycipe3q2p"


@given(st.integers(min_value=0, max_value=(1 << 53) - 1), st.integers(0, 1023))
def test_round_trip(us, clock):
    tid = encode_us(us, clock)
    assert len(tid) == 13
    assert decode_us(tid) == us


@given(st.integers(0, (1 << 52)), st.integers(1, 10**9))
def test_sorts_in_time_order(earlier, gap):
    assert encode_us(earlier) < encode_us(earlier + gap)


def test_for_time_is_a_cursor_before_the_moment():
    moment = datetime(2026, 3, 30, 18, 36, 7, tzinfo=UTC)
    before = for_time(moment)
    assert time_of(before) == moment


@pytest.mark.parametrize(
    "bad", ["", "3jvdycipe3q2", "3jvdycipe3q2pp", "3jvdycipe3q21", "zzzzzzzzzzzzz"]
)
def test_rejects_malformed(bad):
    with pytest.raises(TidError):
        decode_us(bad)
