//! Day-bucketed Bloom filters: "have these two accounts interacted lately?"
//!
//! The surge detector counts strangers, so it must remember every pair of
//! accounts that replied to, quoted, or mentioned each other in the last few
//! weeks. Exact sets of that (tens of millions of pairs a month) cost
//! gigabytes; one Bloom filter per day costs a few megabytes each, and the 1%
//! false-positive rate only means about one stranger in a hundred is taken
//! for a regular.

use std::collections::VecDeque;

use az::{Az, SaturatingAs};

/// A fixed-size Bloom filter over 64-bit keys.
#[derive(Debug, Clone)]
pub struct Bloom {
    bits: Vec<u64>,
    bit_count: u64,
    hashes: u32,
}

impl Bloom {
    /// Creates a filter sized for `items` keys at about `false_positive` rate.
    ///
    /// # Examples
    ///
    /// ```
    /// use pileon_core::bloom::Bloom;
    /// let mut seen = Bloom::with_capacity(1_000, 0.01);
    /// seen.insert(42);
    /// assert!(seen.contains(42));
    /// ```
    pub fn with_capacity(items: u64, false_positive: f64) -> Self {
        let items = items.max(1).az::<f64>();
        let ln2 = std::f64::consts::LN_2;
        let bits = (-(items * false_positive.clamp(1e-9, 0.5).ln()) / (ln2 * ln2)).ceil();
        let bit_count = bits.saturating_as::<u64>().max(64);
        let hashes = ((bit_count.az::<f64>() / items) * ln2)
            .round()
            .saturating_as::<u32>()
            .clamp(1, 16);
        let words = bit_count.div_ceil(64).saturating_as::<usize>();
        Self {
            bits: vec![0; words],
            bit_count,
            hashes,
        }
    }

    /// Adds `key`.
    pub fn insert(&mut self, key: u64) {
        for index in indexes(key, self.bit_count, self.hashes) {
            let (word, bit) = split(index);
            self.bits[word] |= 1 << bit;
        }
    }

    /// Returns whether `key` may have been added (never false for added keys).
    pub fn contains(&self, key: u64) -> bool {
        indexes(key, self.bit_count, self.hashes).all(|index| {
            let (word, bit) = split(index);
            self.bits[word] & (1 << bit) != 0
        })
    }
}

fn indexes(key: u64, bit_count: u64, hashes: u32) -> impl Iterator<Item = u64> {
    let second = mix(key) | 1;
    (0..u64::from(hashes)).map(move |i| key.wrapping_add(i.wrapping_mul(second)) % bit_count)
}

fn split(index: u64) -> (usize, u64) {
    ((index / 64).saturating_as::<usize>(), index % 64)
}

/// `SplitMix64` finalizer: a second, independent hash for double hashing.
fn mix(mut x: u64) -> u64 {
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    x ^ (x >> 31)
}

/// One Bloom filter per day, keeping the last `days` days.
#[derive(Debug, Clone)]
pub struct DayBlooms {
    days: u32,
    per_day: u64,
    false_positive: f64,
    buckets: VecDeque<(i64, Bloom)>,
}

impl DayBlooms {
    /// Creates an empty ring remembering `days` days of about `per_day` keys.
    pub fn new(days: u32, per_day: u64, false_positive: f64) -> Self {
        Self {
            days,
            per_day,
            false_positive,
            buckets: VecDeque::new(),
        }
    }

    /// Records `key` on `day` (days since the Unix epoch), dropping days that
    /// fell out of the window.
    pub fn insert(&mut self, key: u64, day: i64) {
        let oldest = day - i64::from(self.days);
        while self.buckets.front().is_some_and(|(d, _)| *d <= oldest) {
            self.buckets.pop_front();
        }
        let index = match self.buckets.iter().position(|(d, _)| *d >= day) {
            Some(i) if self.buckets[i].0 == day => i,
            Some(i) => {
                self.buckets.insert(i, (day, self.fresh()));
                i
            }
            None => {
                self.buckets.push_back((day, self.fresh()));
                self.buckets.len() - 1
            }
        };
        self.buckets[index].1.insert(key);
    }

    fn fresh(&self) -> Bloom {
        Bloom::with_capacity(self.per_day, self.false_positive)
    }

    /// Returns whether `key` was recorded within `days` days before `day`.
    pub fn contains(&self, key: u64, day: i64) -> bool {
        let oldest = day - i64::from(self.days);
        self.buckets
            .iter()
            .filter(|(d, _)| *d > oldest)
            .any(|(_, bloom)| bloom.contains(key))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_false_negatives_and_few_false_positives() {
        let mut bloom = Bloom::with_capacity(10_000, 0.01);
        for key in 0..10_000_u64 {
            bloom.insert(mix(key));
        }
        assert!((0..10_000_u64).all(|key| bloom.contains(mix(key))));
        let false_hits = (10_000..110_000_u64)
            .filter(|key| bloom.contains(mix(*key)))
            .count();
        assert!(false_hits < 2_000, "{false_hits} false positives in 100k");
    }

    #[test]
    fn day_ring_forgets_old_days() {
        let mut ring = DayBlooms::new(30, 1_000, 0.01);
        ring.insert(7, 100);
        assert!(ring.contains(7, 100));
        assert!(ring.contains(7, 129));
        assert!(!ring.contains(7, 130));
        ring.insert(8, 131);
        assert!(!ring.contains(7, 131));
        assert!(ring.contains(8, 131));
    }
}
