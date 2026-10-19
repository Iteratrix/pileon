//! Surge detection over recorded or live Jetstream rows.
//!
//! [`stream`] reads recorded partitions back in sequence order, [`feed`]
//! hands one row to the detector, and [`output`] writes closed surges. The
//! live alert daemon feeds the same detector through [`feed`], converting
//! each live event with [`stream::Row::from_core`], so replay and live see
//! identical inputs.

pub mod output;
pub mod stream;

use pileon_core::surge::{EngagementKind, PostEvent, Surge, SurgeDetector, event_time_us};

use stream::{PostLine, Row, SubjectLine};

/// Feeds one row to `detector`; returns the surges that closed and the
/// row's event time.
pub fn feed(detector: &mut SurgeDetector, row: &Row) -> (Vec<Surge>, i64) {
    match row {
        Row::Post(PostLine {
            ts,
            did,
            rkey,
            created_at,
            parent,
            quote,
            mentions,
            ..
        }) => {
            let at_us = event_time_us(*ts, created_at.as_deref());
            let uri = format!("at://{did}/app.bsky.feed.post/{rkey}");
            let closed = detector.observe_post(&PostEvent {
                at_us,
                author: did,
                uri: &uri,
                parent: parent.as_deref(),
                quote: quote.as_deref(),
                mentions,
            });
            (closed, at_us)
        }
        Row::Like(SubjectLine {
            ts,
            created_at,
            subject,
            ..
        }) => {
            let at_us = event_time_us(*ts, created_at.as_deref());
            (
                detector.observe_engagement(subject, EngagementKind::Like, at_us),
                at_us,
            )
        }
        Row::Repost(SubjectLine {
            ts,
            created_at,
            subject,
            ..
        }) => {
            let at_us = event_time_us(*ts, created_at.as_deref());
            (
                detector.observe_engagement(subject, EngagementKind::Repost, at_us),
                at_us,
            )
        }
    }
}
