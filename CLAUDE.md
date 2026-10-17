# pileon

Research and tools for Bluesky pile-ons: detect them, attribute them, and give
targets tunable defenses. Started 2026-10-16 from a private quote-post pile-on
study and the `nuclear-block` idea (myrrlyn, bsky post `3mwogfnvves2a`: "block
this account and accounts tightly interlinked with it ... the thermonuclear
block").

**Private material lives in `private/` (gitignored).** If `private/NOTES.md`
exists, read it: it covers the private study, its data, results that derive
from it, and machine details. Nothing from `private/` may be committed, and
nothing that names the study's amplifier or targets belongs in tracked files.

## Vocabulary (agreed 2026-10-16)

- **Surge**: a burst of stranger arrivals at a target, relative to the
  target's own baseline. Valence-agnostic: what a burst detector finds, and
  what virality looks like before valence.
- Surges split by the valence of the arrivals (two dials: hostile share and
  supportive share of labeled replies; defaults 10% and 25%):

  |                     | supportive low | supportive high |
  |---------------------|----------------|-----------------|
  | **hostile high**    | **pile-on**    | **crossfire**   |
  | **hostile low**     | **spotlight**  | **rally**       |

  Pile-on: disproportionately hostile. Rally: disproportionately supportive
  (people rallying around the target). Crossfire: both in force (attackers and
  defenders fighting in the target's replies). Spotlight: attention without
  much stance.
- **Amplifier**: the account whose post caused a surge (any valence). The
  amplifier of a pile-on is an **instigator**. **Target**: the account surged
  at.
- "Hostile" here means codebook v2 hostile-or-abusive and aimed at the target.

## Roadmap

1. **Pile-on detector**, validated against historical Bluesky data. Built as a
   replayable stream processor: consumes events (posts, replies, quotes, likes,
   reposts, blocks, postgate detaches) in time order and emits per-target
   pile-on state. Historical validation = replay through it; live use = the
   same code on the Jetstream live tail. One implementation, so what is
   validated is what ships.
   - 1a. **Nuclear blocker**: per-incident blocklists with a dialable yield.
     Score by counterfactual replay: "target nuked at time t with yield Y" ->
     false positives (blocked non-assailants; defenders and the target's own
     follows reported separately) and false negatives (assailants who got
     through). Blocking the instigator before the QP prevents the quote itself
     (the railgun ceiling).
   - 1b. **Instigator labeler**: "has triggered a pile-on" (evidence-cited)
     and "could trigger one" (risk). Needs a corpus with many instigators.
   - 1c. **Live defense**: detect, notify, act when targeted. Open question:
     does early action (detach quote, threadgate, block instigator) break the
     cycle? Measurable via natural experiments (detaches and blocks are public
     records). Also: allow replies but instantly block posts that cross an
     abuse threshold.
2. Productionize some or all of the above. Rust core (pure, no I/O, `Fetch`
   trait, like `../bluesky-crawler`); research stays in Python (`lab/`).

Every dial (detection sensitivity, label confidence, block yield, abuse
threshold) ships with its measured tradeoff curve, never a bare magic number.

## Layout

- `crates/pileon-core/` - pure Rust: Jetstream event parsing (live frame and
  replay SDK line into one `Event`) and the event-to-row mapping. Detector
  code goes here too, so ingest and detection share one model.
- `crates/pileon-core/src/surge.rs` - the surge detector (Stage A): a pure
  state machine over post edges in time order. Arrivals = replies to, quotes
  of, mentions of a target by others; strangers = no interaction either way in
  30 days (day-bucketed Bloom filters, `bloom.rs`). Baseline = decayed count of
  stranger arrivals normalized by observed time (unbiased from day one).
  Opens when the trailing window holds >= `min_strangers` distinct strangers
  with Poisson surprise >= `min_surprise` (`poisson.rs`); closes after
  `quiet_hours` without strangers or `max_hours`. Records arrivals (including
  a lookback before onset) and engagement on quotes of the target's posts
  (amplifier candidates).
- `crates/pileon-detect/` - `pileon-detect` binary: merges the recorded
  `posts`/`likes`/`reposts` partitions from any roots in seq order (dropping
  overlap), runs the detector, writes one surge per JSON line (`.zst` output
  is compressed). Arrivals are compact rows `[seconds from onset, author,
  kind r/q/m, post rkey, target-post rkey, stranger]`. Every dial is a flag
  (`--min-strangers`, `--min-surprise`, `--window-minutes`, ...). Example:
  `target/release/pileon-detect --root data/jetstream/replay --root
  data/jetstream/live --out data/surges/all.jsonl.zst`. ~1M rows/s, ~1 GB RAM.
- `crates/pileon-recorder/` - `pileon-recorder` binary: `live` (WebSocket
  tail) and `ingest` (SDK JSON lines on stdin), hourly zstd JSONL partitions,
  `state.json` cursor, disk floor (exit 75).
- `replay/` - `replay.mjs`, the one-shot archive replay driving the official
  `@bsky/jetstream` SDK (pinned 1.0.0); pipes into `pileon-recorder ingest`.
- `deploy/` - systemd user units (`pileon-recorder.service`,
  `pileon-replay.service`); installed copies live in `~/.config/systemd/user/`.
- `lab/` - `pileon-lab` (Python, uv): pile-on detector research. `fetch.py`
  (polite cached XRPC; `offline=True` for analysis), `sources.py` (typed
  AppView/PDS reads), `study.py` (qp-pileon clusters as incidents),
  `enrich.py` (warms the cache), `features.py` (relative features),
  `sweep.py` (de-identified sweep data, AUC ranking, page build),
  `web/sweeps.template.html` (the interactive sweep page), `codebook.py`
  (valence codebook v2), `agreement.py` (kappa), `judges.py` (LLM gold-judge
  calibration). Cache and outputs in `data/lab/`. Pipeline: `enrich` (warm the
  cache from a study database) -> `features` -> `sweep` (with `--template`
  and `--page` to build the page, `--corpus-note` to describe the data).
  Checks: `cd lab && uv run pytest && uv run ruff check . && uv run mypy src`.
- `private/` - gitignored; see above.

## Data sources (verified 2026-10-16)

- **Jetstream v2 live tail** (`wss://jetstream.us-{east,west}.bsky.network`,
  `network.bsky.jetstream.subscribeEvents`, subprotocol `xrpc.v1.json`): no
  auth, server-side `collections`/`dids`/`kinds` filters (<=100 collections,
  <=10k DIDs), 36h cursor lookback.
- **Jetstream v2 Network Replay** (docs: bsky.network/docs/jetstream-replay):
  Bluesky-hosted archive of the whole network, built by backfilling every repo
  and then capturing live (segment 0 starts ~2026-07-28). Backfilled records are
  stamped with witness time, so use `createdAt`/rkey TIDs for real times; only
  the post-launch segments are in time order. HTTP `planSnapshot` /
  `getSegment` / `getBlock` need an API key (bsky.network/account; ours is
  `~/.config/pileon/jetstream.key`, mode 600) and are metered in downloaded
  bytes: 1 GB burst, 2 MB/s refill (~173 GB/day). Collection filtering in plans
  saves almost nothing (posts-only ~= the whole archive). Archive on
  2026-10-16: 2.08 TB, 26.4B events; live era 237 GB / 2.29B events. Compaction
  **physically removes deleted records**, so the archive has survivorship bias.
  Replies whose parent no longer resolves still reveal deleted target posts.
- **Constellation** (constellation.microcosm.blue): backlink index (who
  replied/quoted/liked/reposted a target, counts, distinct DIDs); API marked
  unstable; drops deleted records.
- **Public AppView** (public.api.bsky.app): threads, likes with timestamps,
  quotes, profiles, relationships; ~3000 req / 5 min / IP.
- Static datasets (Zenodo Bluesky Social Dataset through early 2024; HF
  `tiagozip/bluesky`) are less complete than the archive and carry consent
  controversy. Prefer the archive.

Measured live volume (100 s sample, Fri 16:53 PDT), per day: 3.1M posts
(1.5M replies, 0.25M quotes), 20M likes, 3.5M reposts, 2.6M follows, 0.34M
blocks, 0.1M postgates; ~20 GB/day raw JSON across those collections.

## Recording (started 2026-10-16)

Decided after two independent reviews (Opus 5.5 and Fable, both
"agree-with-changes"): a corpus is needed for unseeded discovery (1) and the
labeler (1b); 1a hydrates per incident on demand; 1c can prototype on microcosm
Spacedust (`wantedSubjectDids`) but should run on our own tail in production.

- **Live:** `pileon-recorder.service` tails `jetstream.us-west.bsky.network`
  into `data/jetstream/live/`. True first recorded seq 27068492305
  (2026-10-17 00:48 UTC); follows were added at seq 27069071413 (01:10 UTC).
  Keeps every like (~1 GB/day); a 14-day prune to likes-on-quote-posts is still
  to be built (first partitions turn 14 days old on 2026-10-30). Total
  ~1.5 GB/day compressed with follows (~0.13 GB/day of it).
- **Replay:** `pileon-replay.service` backfills the archive's live era, seq
  (24385886263, 27069071412]: from the end of the bootstrap (segment 6887,
  2026-08-05 15:41 UTC) to just before live follow capture. Likes kept only on
  quote posts (`--likes on-quotes`). Quota-bound (~33 h at 2 MB/s, restarted
  with follows 2026-10-17 01:11 UTC); exits 3 at the sealed tip and systemd
  reruns it every 10 min until the window is done. Expected ~50 GB on disk.
- **Overlap:** for every table except follows, seqs 27068492305..27069071412
  are in both roots. Same host, same seq, same mapping: always dedup on `seq`
  when unioning `live` and `replay`.
- **Follow graph before August:** not recorded; pull per incident with AppView
  `getRelationships` (the follow record's rkey is a TID, so it dates the
  follow). A full graph snapshot would mean scanning the 1.84 TB bootstrap.
- Both use us-west only. Hosts have separate seq spaces (us-east was rebuilt
  2026-10-02..06); never mix them. `state.json` records the host and refuses a
  mismatch.
- Not covered: live events from 2026-08-04 13:13 to 2026-08-05 15:41 UTC (merged
  into the bootstrap as current state, so their deletes and timing are gone),
  and anything before August except via per-account history (PDS `listRecords`,
  free; account-filtered archive plans cost ~0.3 MB per event and are not
  cheap).

Row format: one JSON object per line, `seq` (dedup key: delivery is
at-least-once), `ts` (witness time, microseconds), `did`, then table fields.
Posts carry `parent`, `root`, `quote`, `mentions`, `langs`, never text.
Follows and blocks carry `subject` (a DID). A delete carries only `collection`
and `rkey`; join it to the create row to recover the subject (unfollows,
unblocks).
`created_at` is client-controlled (values from 2002 and odd offsets occur);
2026-08-24 was a relay catch-up day, so `ts` there is late. Prefer
`created_at` when it falls within a sane window of `ts`, else `ts`. Query the
sealed partitions with DuckDB:

```sql
SELECT count(*) FROM read_json('data/jetstream/*/posts/*/*.jsonl.zst',
  format='newline_delimited', union_by_name=true);
```

Operations: `journalctl --user -u pileon-recorder -f` (a stats line per
minute), `systemctl --user status pileon-replay`, progress in each root's
`state.json`. Machine-level changes are logged in `~/.claude/SYSTEM.md`.

## Constraints

- Honor deletions (Bluesky developer guidelines). Never keep text of deleted
  records; survivorship analysis uses aggregates.
- Anything published reports aggregates. Never publish lists or handles of
  non-public accounts (no "swarm lists" leave this repo).
- No automated interactions that notify users (guidelines' spam rule). Blocks
  and labels are fine; auto-replies are not.
- Valence labeling needs the parent/quoted post as context (41% of labels
  flipped without it, per the study).
- Gold checks for valence are done by two LLM judges, an Opus-model agent and
  a Fable agent, not by the owner (decided 2026-10-16). Each judge must first
  match the 184 existing human gold labels from the private study at least as
  well as the local labeler did (weighted kappa ~0.40); agreement between the judges is
  extra evidence on top, never a substitute (model-model agreement is
  consistency, not validity). Calibrated 2026-10-16 (`pileon_lab.judges`,
  blind packet in `data/lab/judges/`, 123 persons-only items, mapping B):
  bulk qwen v2 weighted kappa 0.427; Opus 5.5 judge 0.705; Fable judge 0.700;
  Opus vs Fable 0.920 (5-point, 88% exact). Both pass. Network-wide gold =
  both judges on every item; their disagreements are flagged, not resolved by
  either judge.

## Surge detector status (2026-10-16)

First run over replay Aug 5-11 (all warm-up): ~2,000 surges/day at the loose
defaults (8 strangers, surprise 6), median 32 distinct strangers, 89%
reply-driven, 84% with at least one quote of the target's post, median
duration 13 h. Validated against one high-follower amplifier's quote posts
(details in `private/NOTES.md`): quote posts that coincide with a detected
surge drew a median of 16 distinct responders to the quoted post in 48 h;
those not detected drew a median of 2. Next: rerun on the full replay, then
classify surges (pile-on / rally / crossfire / spotlight) in the lab.

## Stage C runbook: classifying network surges

Unattended path: `deploy/stage-c.sh` (started 2026-10-16 as transient unit
`pileon-stage-c`) waits for the replay, then runs detection, step 1, step 2
and the packet of step 3 below; follow with `journalctl --user -u
pileon-stage-c -f`. After it finishes: run the judges, score, build and
publish the network sweep page.

All in `lab/`, against `data/surges/<run>.jsonl.zst` from `pileon-detect`.
The local labeler runs on muse-mini (`ssh muse-mini`, Ollama, idle otherwise
except for the Muse project); open a tunnel first:
`ssh -N -L 11435:localhost:11434 muse-mini &`.

1. Label a sample: `uv run python -m pileon_lab.label --surges S --cache
   ../data/lab/cache.db --labels ../data/lab/labels.db --sample-size 500
   --per-surge 30` (stratified by size, warm-up excluded; ~2 s/item with
   `qwen3.5:27b-mlx`, so 15k items is ~9 h; resumable).
2. Features: `uv run python -m pileon_lab.network --surges S --cache ...
   --labels ... --enrich --out ../data/lab/network-features.jsonl` (same
   sample/seed), then `pileon_lab.sweep --features ... --template
   web/sweeps.template.html --page ... --corpus-note "..."`.
3. Gold and hill-climbing: `pileon_lab.evalset packet --surges S --cache ...
   --labels ... --size 300 --dir ../data/lab/eval-net`; run the two judge
   agents on `packet.jsonl` (outputs `opus.jsonl`, `fable.jsonl`, blind, same
   rules as the study calibration); `evalset run --dir ... --model M
   [--variant V]` for each candidate; `evalset score --dir ...` (dev split)
   ranks them against judge consensus. Candidates must also not regress on
   the human gold: `evalset run --dir ../data/lab/judges --model M` then
   `pileon_lab.judges score` on the study packet. Read `--split test` only
   for the final choice. Prompt variants live in `evalset.VARIANTS`.

Hydrated post text sits only in the response cache (`data/lab/cache.db`);
purge it after labeling runs if retention matters.

## Findings

Results so far come from the private study and are kept in
`private/NOTES.md`. In general terms: relative volume (arrivals against the
target's own baseline) says a surge is happening; approval signals (likes on
the target post vs on the amplifier's post and the replies, repliers not
liking what they reply to) separate pile-ons from rallies without any text.
Most of that signal is already present 15 minutes after the amplifying post
(`pileon_lab.early`), so a live defense can act early. Surges whose replies
are mostly critical but rarely hostile ("critical dogpiles", ~8% of judged
incidents) sit between spotlights and pile-ons on every approval signal:
structure measures disapproval, and only text separates disagreement from
attack, so defenses should keep those as two separate dials. Off-the-shelf
toxicity/sentiment models rank hostility decently but trail the
context-aware labeler at usable thresholds (`pileon_lab.baselines`).
