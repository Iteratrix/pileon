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
   armed at the alert could still stop).
5. Collateral: for early alerts, the supportive replies after +15 min that
   would be stopped by blocking the quoter's audience (`nuke`'s `audience`
   yield), reported separately for true and false alerts.
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

## Deviations

1. The primary gold is the pooled in-context class, not the cascade class the
   reliability rule picked (see "Result on interim"). Decided 2026-10-17,
   after the interim thread round and before any held-out label.
