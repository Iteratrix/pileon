# Pre-registration: grading pile-on detectors on held-out network surges

Fixed on 2026-10-17, before any Stage C surge was labeled or graded. Stage C
labeling starts on 2026-10-18 at the earliest. The commit that adds this file
is the timestamp. Any later change is listed under Deviations, with its reason
and the result both ways.

## Question

Do structural signals (counts, ratios and follow relationships, no text)
detect hostile pile-ons on network surges the detector has never seen? And how
early?

## Data

- **Dev**, used to choose the signals and their directions, freeze the
  reference distributions and alert cutoffs, and choose the gold:
  - the interim network sample: 200 surges with onsets from 2026-08-19 to
    08-26 (UTC), from `pileon-detect` over the replay up to 08-26, sample seed
    20261017;
  - the private study's 607 incidents.
- **Held-out**: the Stage C sample, i.e. `deploy/stage-c.sh`: `pileon-detect`
  over the full replay plus the live archive, 500 surges stratified by size,
  seed 20261017, warm-up excluded. Removed from it: every surge with onset
  before 2026-08-27T00:00Z (the interim window). A held-out surge
  **overlaps** the dev data when its target or its amplifier is a target or
  amplifier in either dev set (the study centers on one amplifier). The
  overlap is counted (a number, never a list), and the primary result
  excludes those surges; with them is reported as secondary.

  Only surges with at least 10 labeled replies are graded. Attrition is
  reported at each stage, by size stratum:
  - sampled;
  - target post gone;
  - fewer than 10 labeled replies;
  - unscored (fewer than two signal families);
  - selected for thread judging but unjudgeable.

## Gold

The cascade is the secondary gold. It is also the screen that picks surges
for judging. Reply labels come from a screen-and-confirm cascade
(`pileon_lab.confirm`):
1. `gemma4:31b` (contrast prompt, then a direction pass) labels up to 30
   sampled replies and quotes per surge.
2. Every hostile or abusive call aimed at the target is re-judged by two blind
   LLM judges (Opus 5.5 and Fable 5.1, fresh context, packet copies outside
   the repository), and each takes the median of the three labels.
3. 120 random unflagged decoys, plus toxicity-ranked ones, estimate the misses.

A surge's reply class uses the shares of its labeled replies:
- **pile-on**: contempt (hostile or abusive, aimed at the target) at least
  10%, and supportive replies under 25%;
- **crossfire**: both thresholds met;
- **rally**: supportive replies at least 25%, contempt under 10%;
- **spotlight**: neither.

The primary gold is chosen by the thread round's decision rule (written
down before any verdict existed, in the private notes at 22:58 on
2026-10-17). Interim thread packets were judged by the same two judges, each
surge shown as one conversation (the target's and the quoter's threads, no
counts, handles or timestamps). Three measures per surge:
- **H**: the holistic verdict (pile-on or not; `unclear` is not a pile-on);
- **C**: the reply class of the in-context labels of the replies the cascade
  also labeled;
- **A**: the reply class of every in-context label shown.

For C and A, contempt means hostile or abusive with `aimed` = target. When
pooling the judges, the class is `surge_class` over the union of both judges'
labels, and needs at least 10 labels; otherwise the surge takes its cascade
class. Reliability is the Opus vs Fable Cohen's kappa on pile-on vs not, per
measure. It is undefined, and so fails every threshold, when either judge
makes fewer than two pile-on calls. The rule:
1. **Hybrid** if kappa(H) >= 0.6 and kappa(H) >= max(kappa(C), kappa(A)) -
   0.1. A surge is a pile-on when both holistic verdicts say so; on a split,
   the pooled in-context class C decides.
2. Else the **in-context class** (C or A, whichever is more reliable) if
   its kappa >= 0.6.
3. Else the **cascade reply class**.

Severity, `piled_on` and verdict confidence are exploratory and never gold.

### Result on interim, and the gold this pre-registration uses

The interim round covered 28 surges:
- the 5 cascade pile-ons;
- 11 surges the screen alone called pile-ons;
- 12 others: near-threshold, contested and random ones.

Each judge labeled 1,352 posts in context.
- **H**: neither judge called any surge a pile-on; both called the same one a
  crossfire. kappa(H) is undefined.
- **In context the judges agree**: weighted kappa 0.71 on labels, 0.96 on
  contempt. But almost nothing reaches the pile-on class: kappa(C) and
  kappa(A) are undefined (pile-on calls 1/1 and 0/1).
- **The rule therefore picks the cascade class, and that class is not
  valid.** In context, 4 of its 5 interim pile-ons carry little or no
  contempt toward the target. In two, the target's post was a screenshot
  mocking someone else, which the reply-by-reply labelers never see. Of the
  replies a judge had itself called contempt when shown one at a time, Opus
  withdrew 9 of 22 in context and Fable 19 of 31.
- **gemma4 as a holistic judge** called 4 surges pile-ons (kappa 0 against
  either judge) and repeated the screenshot error, so it cannot replace the
  judges.

**Deviation from the rule.** It was made after these interim results and
before any held-out label existed. The rule tested reliability only; it
could not tell a reliable but rare measure from a measure that is wrong.
- **Primary gold**: the pooled in-context class A, over both judges' labels
  of every post shown.
- **Secondary golds**: the cascade class, and "hostile surge" (pile-on or
  crossfire, i.e. contempt of at least 10% regardless of support).
- **Exploratory**: the holistic verdict.

In-context gold needs judges on the held-out surges (thread packets, both
judges). They see:
- every held-out surge with a cascade contempt share of at least 4%, or that
  the screen alone called pile-on or crossfire;
- every surge that either frozen detector alerts on at its 10% cutoff;
- a random 25% of the rest (seed 20261018).

Primary AUC, precision and recall use only judged surges, with
inverse-probability weights: 4 for the random stratum, 1 otherwise. A
selected surge that cannot be judged (its target post is gone) takes its
cascade class. Picking surges by detector alerts is safe only because every
stratum's inclusion probability is known and weighted: the alerts' precision
is then measured directly, and AUC and recall stay unbiased. Unweighted
results on the judged set would favor the detector, and they are not
reported. As a secondary, every graded surge counts unweighted against the
cascade class.

In-context pile-ons were 0 or 1 of 28 enriched interim surges, so the
held-out set may hold very few. With fewer than 5 primary positives, AUC is
not read as a test: the counts and the alerts' precision are reported, and
grading moves to a pile-on-rich sampling frame, pre-registered separately.

Gold tooling (`label`, `confirm`, `threads`, `evalset`) is pinned at the
commit that adds this file. Held-out judges run on copies
outside the repository with fresh context, one directory per judge, helper
scripts included (runbook step 4). `merge` never relabels misses that decoys
find, so confirmation only corrects contempt downward. The miss estimate
next to each result is the size of that bias.

## Detectors (frozen at commit 98b3a76)

`pileon_lab.detector`. What is frozen is `Reference.score` and the two
reference files below. The grading code may change only in ways this document
specifies. A surge's score is the mean of four equally weighted
families, each the mean of its signals' signed percentiles in a frozen
reference distribution. Nothing is fitted.

- **retro** (signals as measured 48 h on): `lab/detector/retro-reference.json`,
  sha256 `57a874b6477e0adbc7516379a211f4d7210f7a5b5d41dfb603c53d5f9d5675ef`.
  - volume: reply lift vs the median and the 90th-percentile post, replies vs
    the previous high, 48 h arrivals vs the median, engagement mix shift;
  - approval: likes per reply (low), arrivals who like the target post (low),
    top response vs target likes, dunk vs target likes;
  - reach: quoter/target followers, target followers (low);
  - audience: arrivals following the quoter, arrivals following the target
    (low), arrivals the target follows (low).
- **early** (only what is observable 15 minutes after the surge's onset,
  the detector's start of the surge, which is not the quote time; the median
  delay from quote to onset on held-out is reported):
  `lab/detector/early-reference.json`, sha256
  `d83328bc1f74ddc10f4f191dcc63fe8ca491e45d0e5ae6ef853f1cf701e089c5`.
  - volume: arrivals by +15 min vs the target's median replies;
  - approval: likes per reply and share of arrivals liking the target post,
    both by +15 min and both low;
  - reach: as retro;
  - audience: arrivals following the quoter (high) and the target (low).

The alert cutoffs are fixed scores: each reference's 95th, 90th and 80th
percentile scores on the interim surges. On a size-stratified held-out sample
the realized alert rates will differ, and they are reported. At a base
rate of a few percent or less, a 10% alert rate caps precision near 0.3 or
lower.

## Metrics

Primary: the retro detector's AUC against the primary gold on held-out
surges that do not overlap the dev data, with a 95% stratified bootstrap
interval (2,000 draws, positives and negatives resampled separately,
weights carried along).

Secondary:
1. Precision, recall and realized alert rate at the frozen 10% cutoff, and at
   5% and 20%; and the alert rate needed for 80% recall (threshold-free).
2. The same against the cascade reply class, and against the screen alone (to
   show how much confirmation matters).
3. The early detector's AUC against the primary gold, and its precision and
   recall at the 10% cutoff.
4. Lead time: for early alerts that are true pile-ons, the share of their
   eventual contempt replies that arrive after onset + 15 min (what a defense
   armed at the alert could still stop). Contempt is counted as in the
   pooled class: each judge's in-context contempt call counts one half. A
   post's time is its `createdAt`.
5. Collateral: for early alerts, the supportive replies after +15 min that
   would be stopped by blocking the quoter's audience (`nuke`'s `audience`
   yield on `gemma4:31b+contrast@confirmed` labels), reported separately for
   true and false alerts.
6. Labeler error: the judges' agreement, and the decoys' miss estimate and its
   interval, reported next to every number above.

How to read it:
- **Replicated**: the retro AUC interval's lower end is above 0.60.
- **Strongly replicated**: the lower end is above 0.70.
- **Early holds**: its point estimate is 0.70 or more against the primary
  gold.

Expected precision: at an AUC of 0.80, the interval is about +/-0.17 with 10
positives and about +/-0.11 with 25 (Hanley-McNeil). So "strongly
replicated" needs a point estimate near 0.87 at 10 positives. Fewer than 10
held-out pile-ons makes every result provisional, and it is reported as
such.

## Amendment 1: the trawl, a pile-on-rich frame

Added on 2026-10-18, after Stage C finished labeling and before any held-out
surge was judged in context or graded. Nobody has read the Stage C screen
labels, its features or its classes. The trawl's phase-1 counts were being
fetched while this was written; they are label-free. A first version of the
tiers and draws was fixed in code (`pileon_lab.trawl`) before any count
arrived. A Fable review then enlarged the draws and replaced the rally and
notification rules. Counts were still arriving, but no tier had been computed
and no count had been looked at.

**Why.** On interim, in-context pile-ons were at most 1 of 28 surges enriched
for them, so the base rate is probably under 1%. Stage C grades about 300
held-out surges, so it likely holds fewer than the 5 primary positives this
document requires. This document's answer to that is a pile-on-rich frame. It
is adopted now, before the Stage C result is known, and it runs alongside
Stage C. Stage C's grade stays exactly as registered, and the two are reported
separately and never pooled.

**Frame.** Every surge in Stage C's full detection run (replay plus live
archive) that passes these checks, applied in this order and each exclusion
counted under the first check it fails:
1. not warm-up;
2. has a target post;
3. onset on or after 2026-08-27T00:00Z;
4. at least 10 recorded replies plus quotes;
5. neither its target nor its amplifier is in either dev set. Overlapping
   surges are excluded outright, not reported as a secondary.

**Phase 1 (selection only).** Each eligible surge gets the frozen retro
`Reference.score` on a row holding only what batched post and profile counts
give:
- approval: likes per reply on the target post, and dunk likes against
  target likes;
- reach: quoter/target followers, and target followers.

Counts are the current ones on 2026-10-18. A surge scored on fewer than two
families stays out of the tiers, and is counted.

**Tiers and draws** (seed 20261019, one draw per tier). Percentile cutoffs
are order statistics: the cutoff for quantile q is the value at index
floor(q x n) of the sorted scores. A surge at a cutoff joins the higher tier
(for the rally tier, a surge at its cutoff joins it). The rally tier is
carved out first:
- **rally**: approval family score at or below its 5th percentile among
  scored surges, i.e. the target post is liked far more than it is replied to
  and out-likes the dunk. Draw 40.
- The rest, by phase-1 score percentile among all scored surges:
  - **top**: 99.5th and above. Draw 100.
  - **high**: 98th to 99.5th. Draw 50.
  - **mid**: 90th to 98th. Draw 50.
  - **low**: below the 90th. Draw 60.

`trawl plan` records the sha256 of the phase-1 file and of the reference in
`tiers.json`. It runs only after this amendment is committed and pushed.

**Phase 2.**
- Drawn surges get full features (`network --enrich`, no labels) and early
  rows (`detector early`).
- Trawl and Stage C plan surges go into one shuffled thread packet (`trawl
  union`, then `threads packet --plan`), so each surge is judged once and
  judges cannot tell frames or tiers apart.
- Judges, codebook and rules are the same as for Stage C (runbook step 5).

**Gold.** The pooled in-context class is primary and hostile surge is
secondary, both as defined above. The pooled class needs at least 10 pooled
labels, which is 5 posts labeled by both judges, the same rule as for Stage
C.
- A judge that leaves a surge without a verdict is rerun on that surge. A
  surge is never dropped for a missing verdict.
- A drawn surge is unjudgeable when its thread cannot be built or it has fewer
  than 10 pooled labels. Unjudgeable surges are counted by reason and dropped
  from their tier. There are no replacements and no cascade fallback (trawl
  surges have no screen labels).
- A judged surge's weight is its tier's size over the number of judged
  surges in that tier.

**Tests.**
1. Primary: does the phase-1 score order pile-ons? A one-sided
   Cochran-Armitage trend test of the pile-on share across top, high, mid and
   low (scores 3, 2, 1, 0), at alpha 0.05.
   - Each tier's pile-on share is reported with its Wilson 95% interval. The
     top tier's share is the precision of the structural screen at a 0.5%
     alert rate within the frame.
   - Beside the p value: the enrichment, top-tier share over the weighted
     frame rate, with a stratified bootstrap interval (2,000 draws, seed 7)
     and the share of draws where it is undefined.
2. Rally tag:
   - is the rally tier's rally share higher than the low tier's? One-sided
     Fisher exact.
   - is its hostile-surge share lower than the top tier's? One-sided Fisher
     exact.
3. Secondary:
   - test 1 with hostile surges;
   - the full frozen retro and early detectors on the judged trawl surges:
     weighted AUC with the stratified bootstrap of Metrics, and the precision
     of their 10% alerts, all against the primary gold;
   - the weighted pile-on rate in the frame.

**How to read it.**
- **Proxies rank pile-ons**: the primary trend p is below 0.05. The
  enrichment and the top tier's share are always reported beside it, so a
  significant trend at a few percent precision reads as exactly that.
- **Notification-grade**: read only if the primary passes. The top tier's
  pile-on share has a Wilson lower bound of at least 0.20.
- **Rally tag works**: the rally-share Fisher p is below 0.05.
- **Replicated (trawl)**: the retro detector's weighted AUC lower bound is
  above 0.60. Low-tier weights run near 1,000, so this interval will be wide,
  which is why the AUC is secondary here.

Power (simulated by the Fable review, alpha 0.05): the primary trend test
has power 0.68 when the tiers' pile-on shares are 5%, 2%, 1% and 0.2%, and
0.97 when the top tier's share is 10%. A 20% top share has a Wilson interval
of about 13-29%. The rally-share test has power about 0.73 at a 90% rally
share against 70%. Tooling (`pileon_lab.trawl`) is pinned at the commit that
adds this amendment.

## Amendment 2: the live variant, what an alert can see

Added on 2026-10-18, after the Stage C and trawl grades of the retro and
early detectors were computed and read. Before porting the early detector to
the live path, a check of what it uses found inputs a live system cannot have
at onset + 15 min:
- **Onset is backdated.** It is the first stranger arrival in the window that
  opened the surge. The detector only knows of a surge when it triggers, a
  median 39 min after onset on the 523 thread-round surges. 88% trigger later
  than onset + 15 min.
- **The target post and the quoter come from the whole surge.** The target
  post is the one drawing the most arrivals over the surge (the one leading at
  +15 min in 67% of surges), and the quoter is the author of the quote with
  the most engagement while the surge was open. In 73% of the surges with a
  quote, that quote was posted after onset + 15 min. In 5 of the 8 held-out
  primary positives, it was posted after onset + 15 min.

So the early grade does not estimate a live alert. It stays on record as
computed. This amendment adds the variant that does.

**The live variant.** The alert fires when the surge triggers, and every
input is what was visible then:
- **Snapshot.** `pileon-detect` records `trigger_target_post`, the target's
  post drawing the most arrivals from onset to trigger (ties: the smaller
  URI), and `trigger_quote`. That is the quote of the target's posts with the
  most engagement at trigger. Engagement (replies, quotes, reposts, likes) is
  counted from when the quote was posted, for quotes inside the lookback.
  Ties go to the earlier quote, then the smaller URI. No quote at trigger
  means no quoter.
- **Signals.** The early signals and directions, cut at the trigger:
  - volume: distinct arrivals from onset to trigger against the target's
    median replies before onset (the baseline's kind matched to the trigger
    target post);
  - approval: likes per reply on the trigger target post and the share of
    arrivals liking it, likes dated by the trigger;
  - reach: trigger quoter/target followers, and target followers (low);
  - audience: arrivals following the trigger quoter (high) and the target
    (low), follows dated before onset.

  Relationships are looked up only for arrivals by the trigger. Follower
  counts are current, as for the other variants.
- **Reference.** `lab/detector/live-reference.json`, frozen by `detector
  freeze --variant live` from live rows on the same 200 unlabeled interim
  surges as the early reference. Each surge is taken from the 2026-10-18
  detection run by its id, and an id missing from that run is dropped and
  counted. Cutoffs are the 95th, 90th and 80th percentile scores as before.
  The freeze runs after this amendment is committed and pushed. Its sha256
  is added here in a separate commit, before any live row on a graded surge
  is scored.

**Grading.** The same gold files as before, unchanged:
- primary: weighted AUC with the stratified bootstrap on the Stage C primary
  gold (`heldout/gold-primary.json`). Also precision, recall and the realized
  alert rate at the live 10% cutoff;
- secondary: the same on the Stage C hostile gold and on the trawl primary
  gold (`trawl/gold-primary.json`);
- lead time: the share of pooled contempt in true live alerts that arrives
  after the trigger (`heldout alerts --at trigger`);
- descriptive: how often the trigger target post and quoter differ from the
  whole-surge ones, and the early and live scores side by side on the same
  surges.

**How to read it.** The live variant's numbers are the ones reported for a
live alert. The early detector's numbers are not. No threshold here decides
whether the live variant runs. It runs in shadow mode (alerts logged,
nobody notified), and judging a sample of its alerts measures live
precision. Notifying anyone is the owner's decision and is not made here.

## Deviations

1. The primary gold is the pooled in-context class, not the cascade class the
   reliability rule picked (see "Result on interim"). Decided 2026-10-17,
   after the interim thread round and before any held-out label.
