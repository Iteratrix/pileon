//! AT Protocol TIDs: record keys that encode their creation time.
//!
//! A TID is 13 base32-sortable characters holding a 64-bit integer: a zero
//! top bit, 53 bits of microseconds since the Unix epoch, and a 10-bit clock
//! id. A follow's rkey therefore dates the follow, and a TID minted for an
//! instant is a `listRecords` cursor that starts just before it.

use az::Az;

const ALPHABET: &[u8; 32] = b"234567abcdefghijklmnopqrstuvwxyz";
const LENGTH: usize = 13;
const CLOCK_BITS: u32 = 10;
const MAX_US: i64 = (1 << 53) - 1;

/// A string that is not a well-formed TID, or a time no TID can hold.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TidError {
    /// Not 13 bytes long.
    #[error("TID must be {LENGTH} characters, got {0}")]
    Length(usize),
    /// A character outside the base32-sortable alphabet.
    #[error("invalid TID character {0:?}")]
    Character(char),
    /// The reserved top bit is set.
    #[error("TID has its top bit set")]
    TopBit,
    /// A timestamp before the epoch or beyond 53 bits.
    #[error("timestamp out of TID range: {0}")]
    Range(i64),
}

/// Returns the microsecond timestamp encoded in `tid`.
///
/// # Errors
///
/// Returns [`TidError`] when `tid` is not a well-formed TID.
///
/// # Examples
///
/// ```
/// use pileon_core::tid::{decode_us, encode_us};
/// let tid = encode_us(1_786_000_000_000_000).unwrap();
/// assert_eq!(decode_us(&tid), Ok(1_786_000_000_000_000));
/// assert!(decode_us("not-a-tid").is_err());
/// ```
pub fn decode_us(tid: &str) -> Result<i64, TidError> {
    if tid.len() != LENGTH {
        return Err(TidError::Length(tid.len()));
    }
    let mut value: u128 = 0;
    for c in tid.chars() {
        let Some(digit) = ALPHABET.iter().position(|&a| char::from(a) == c) else {
            return Err(TidError::Character(c));
        };
        value = value * 32 + digit.az::<u128>();
    }
    if value >> 63 != 0 {
        return Err(TidError::TopBit);
    }
    Ok((value >> CLOCK_BITS).az::<i64>())
}

/// Returns the TID for microsecond timestamp `us` with clock id 0.
///
/// # Errors
///
/// Returns [`TidError::Range`] for a negative timestamp or one beyond 53 bits.
pub fn encode_us(us: i64) -> Result<String, TidError> {
    if !(0..=MAX_US).contains(&us) {
        return Err(TidError::Range(us));
    }
    let mut value = us.cast_unsigned() << CLOCK_BITS;
    let mut chars = [0_u8; LENGTH];
    for slot in chars.iter_mut().rev() {
        *slot = ALPHABET[(value & 31).az::<usize>()];
        value >>= 5;
    }
    Ok(chars.iter().map(|&b| char::from(b)).collect())
}

/// Returns the record key (last path segment) of an AT URI.
///
/// # Examples
///
/// ```
/// use pileon_core::tid::rkey_of;
/// assert_eq!(rkey_of("at://did:plc:a/app.bsky.graph.follow/3abc"), "3abc");
/// ```
pub fn rkey_of(uri: &str) -> &str {
    uri.rsplit('/').next().unwrap_or(uri)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_the_lab_encoding() {
        assert_eq!(encode_us(0), Ok("2222222222222".to_owned()));
        assert_eq!(decode_us("3jzfcijpj2z2a"), Ok(1_688_137_381_887_007));
    }

    #[test]
    fn rejects_malformed_tids() {
        assert_eq!(decode_us("3jzfcijpj2z2"), Err(TidError::Length(12)));
        assert_eq!(decode_us("3jzfcijpj2z21"), Err(TidError::Character('1')));
        assert_eq!(decode_us("zzzzzzzzzzzzz"), Err(TidError::TopBit));
        assert_eq!(encode_us(-1), Err(TidError::Range(-1)));
    }
}
