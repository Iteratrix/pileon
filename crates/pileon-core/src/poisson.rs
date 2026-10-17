//! Poisson tail probabilities in log space.
//!
//! A surge is scored by how unlikely its arrival count is under the target's
//! baseline: `-log10 P(X >= n)` for `X ~ Poisson(mean)`. Tails get far below
//! what an `f64` can hold directly (a quiet account drawing 400 strangers in an
//! hour is around 1e-357), so everything stays in log space.

use az::Az;

/// Returns `-log10 P(X >= n)` for `X ~ Poisson(mean)`: 0 when `n` is
/// unremarkable, 6 for a one-in-a-million count.
///
/// # Examples
///
/// ```
/// use pileon_core::poisson::surprise;
/// assert!((surprise(3, 1.0) - 1.0953).abs() < 1e-3);
/// assert!(surprise(400, 20.0) > 350.0);
/// assert_eq!(surprise(0, 3.0), 0.0);
/// ```
pub fn surprise(n: u32, mean: f64) -> f64 {
    if n == 0 {
        return 0.0;
    }
    let mean = mean.max(1e-12);
    let n_f = n.az::<f64>();
    let log_tail = if n_f <= mean {
        log_upper_tail_by_complement(n, mean)
    } else {
        log_upper_tail_by_series(n, mean)
    };
    (-log_tail / std::f64::consts::LN_10).max(0.0)
}

fn log_pmf(k: u32, mean: f64) -> f64 {
    let k_f = k.az::<f64>();
    -mean + k_f * mean.ln() - ln_gamma(k_f + 1.0)
}

/// `ln P(X >= n)` as `ln pmf(n) + ln sum_j prod_{i=1..j} mean / (n + i)`; the
/// ratio is below one when `n > mean`, so the series converges quickly.
fn log_upper_tail_by_series(n: u32, mean: f64) -> f64 {
    let n_f = n.az::<f64>();
    let mut sum = 1.0;
    let mut term = 1.0;
    let mut i = 1.0;
    while i < 1_000_000.0 {
        term *= mean / (n_f + i);
        sum += term;
        if term < 1e-17 * sum {
            break;
        }
        i += 1.0;
    }
    log_pmf(n, mean) + sum.ln()
}

/// `ln (1 - P(X < n))`, summing the lower terms in log space; used when the
/// tail is large, so the subtraction loses no precision that matters.
fn log_upper_tail_by_complement(n: u32, mean: f64) -> f64 {
    let logs: Vec<f64> = (0..n).map(|k| log_pmf(k, mean)).collect();
    let peak = logs.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let lower = peak + logs.iter().map(|l| (l - peak).exp()).sum::<f64>().ln();
    let upper = 1.0 - lower.exp();
    if upper <= 0.0 {
        return log_upper_tail_by_series(n, mean);
    }
    upper.ln()
}

/// Lanczos approximation of `ln Γ(x)` for `x > 0` (g = 7, nine terms).
fn ln_gamma(x: f64) -> f64 {
    const COEFFICIENTS: [f64; 9] = [
        0.999_999_999_999_809_9,
        676.520_368_121_885_1,
        -1_259.139_216_722_402_8,
        771.323_428_777_653_1,
        -176.615_029_162_140_6,
        12.507_343_278_686_905,
        -0.138_571_095_265_720_12,
        9.984_369_578_019_572e-6,
        1.505_632_735_149_311_6e-7,
    ];
    if x < 0.5 {
        let pi = std::f64::consts::PI;
        return (pi / (pi * x).sin()).ln() - ln_gamma(1.0 - x);
    }
    let x = x - 1.0;
    let mut sum = COEFFICIENTS[0];
    let mut offset = 1.0;
    for coefficient in &COEFFICIENTS[1..] {
        sum += coefficient / (x + offset);
        offset += 1.0;
    }
    let t = x + 7.5;
    0.5 * (2.0 * std::f64::consts::PI).ln() + (x + 0.5) * t.ln() - t + sum.ln()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_reference_values() {
        let cases = [
            (3, 1.0, 1.095_276_898_866_782_5),
            (10, 0.1, 16.599_229_331_381_927),
            (5, 5.0, 0.252_194_696_907_526_1),
            (12, 0.5, 12.492_859_914_085_265),
            (400, 20.0, 357.058_089_128_580_83),
            (1, 0.0001, 4.000_021_714_543_133),
            (30, 30.0, 0.280_434_212_650_085_1),
        ];
        for (n, mean, expected) in cases {
            let got = surprise(n, mean);
            assert!(
                (got - expected).abs() < 1e-6 * expected.max(1.0),
                "surprise({n}, {mean}) = {got}, expected {expected}"
            );
        }
    }

    #[test]
    fn grows_with_count_and_shrinks_with_mean() {
        assert!(surprise(20, 1.0) > surprise(10, 1.0));
        assert!(surprise(20, 5.0) < surprise(20, 1.0));
        assert!(surprise(5, 0.0) > 50.0);
    }

    #[test]
    fn ln_gamma_matches_factorials() {
        let mut factorial: f64 = 1.0;
        for k in 1..=20_u32 {
            factorial *= k.az::<f64>();
            let x = k.az::<f64>() + 1.0;
            assert!((ln_gamma(x) - factorial.ln()).abs() < 1e-9);
        }
    }
}
