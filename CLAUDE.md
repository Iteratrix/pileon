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
     abuse threshold. Owner's leaning for production (2026-10-17): detect
     hostile quote posts early and detach them (postgate
     `detachedEmbeddingUris`), pending what replay validates. Recorded
     postgates carry `detached` URIs and `quotes_disabled` with times, so
     targets' real detaches are the natural experiment. A reply filter by
     text label was proposed and parked. Owner's decision (2026-10-18): the
     response to an alert is a user setting, and the default is a
     notification with a one-tap detach. Auto-detach is opt-in.

   Owner's direction (2026-10-17): the production system relies entirely on
   non-sentiment proxies (structure, approval, graph, timing). Text labels
   (local labelers, judges) exist only to validate those signals across
   many pile-ons in the jetstream. So a labeler is chosen for low
   systematic bias on the data it validates (network-wide surges), at the
   surge level, not for speed or deployability; its errors become the
   validation's errors.
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
  (amplifier candidates). At trigger it snapshots what a live alert can see:
  `trigger_target_post` and `trigger_quote` (engagement counted from posting,
  via a ledger of every quote within the lookback).
- `crates/pileon-detect/` - library (`stream` reads partitions back, `feed`
  hands one row to the detector, `output` writes surge lines) and the
  `pileon-detect` binary: merges the recorded
  `posts`/`likes`/`reposts` partitions from any roots in seq order (dropping
  overlap), runs the detector, writes one surge per JSON line (`.zst` output
  is compressed). Arrivals are compact rows `[seconds from onset, author,
  kind r/q/m, post rkey, target-post rkey, stranger]`. Every dial is a flag
  (`--min-strangers`, `--min-surprise`, `--window-minutes`, ...). Example:
  `target/release/pileon-detect --root data/jetstream/replay --root
  data/jetstream/live --out data/surges/all.jsonl.zst`. ~1M rows/s, ~1 GB RAM.
- `crates/pileon-core/src/alert.rs`, `appview.rs`, `tid.rs` - the live
  detector (amendment 2), ported step for step from the lab's `detector
  live`: a `Snapshot` of a surge at trigger, `gather` (facts through the
  `Fetch` trait; requests match `sources.py` URL for URL), `signals`, and
  `Reference::score` (sums in the lab's order).
- `crates/pileon-alert/` - `pileon-alert` binary. `live` warms the detector
  on every recorded partition, then follows Jetstream from the last recorded
  seq and scores each surge the moment it opens. Shadow mode: it appends to
  `data/alerts/alerts.jsonl` (scores) and `surges.jsonl` (closed surges, for
  judging later) and notifies nobody. `score` replays recorded surges
  against the lab's cache (`CacheFetch` reads `responses` and `posts` by the
  lab's keys); `lab/scripts/parity.py` compares its rows with `detector
  live` rows. Unit: `deploy/pileon-alert.service`.
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
  calibration), `surges.py` / `label.py` / `network.py` / `evalset.py`
  (network surges: sample, label, features, judge packets), `baselines.py`
  (off-the-shelf toxicity models), `decision.py` (decision models through
  Ollama's `/v1/systemone`), `early.py` (early warning), `nuke.py`
  (blocklists and gates scored by counterfactual replay), `threads.py`
  (whole-thread judge packets, in-context gold), `detector.py` (the frozen
  structural detector), `heldout.py` (Stage C held-out grading), `trawl.py`
  (the pile-on-rich frame). Cache and outputs
  in `data/lab/`. Pipeline: `enrich` (warm the cache from a study database)
  -> `features` -> `sweep` (with `--template`
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
  Keeps every like (~1.05 GB/day): the owner decided on 2026-10-17 not to
  prune them, since like signals are the strongest text-free features.
  Revisit (prune to likes on quote posts, or add disk) when free disk falls
  below 60 GB, projected early December. Total ~1.55 GB/day compressed with
  follows (~0.07 GB/day of it).
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
- Decision rights (owner, 2026-10-17): technical and process calls inside the
  owner's direction (labeler choice, relabels, judge packets, what runs on
  muse-mini) are Claude's to make and report. When unsure, ask a Fable agent
  for a second opinion before asking the owner. The owner decides product
  direction and anything irreversible, external, or shared.
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
- Labeler tournament (2026-10-17, `lab/scripts/tournament.sh`, 11 local
  model/prompt configs on muse-mini, same packet). Weighted kappa vs human
  gold (judge-consensus dev in parentheses): llama3.3:70b+v2 0.535 (0.413),
  gemma4:31b+v2 0.445 (0.569), qwen3.5:27b-mlx+v2 0.427 (0.374), gemma4+contrast
  0.419 (0.579), qwen3.5+contrast 0.418 (0.416), mistral-small3.1:24b 0.392,
  qwen3.6:35b-a3b+v2 0.338, qwen3.5+reason 0.240, qwen3:32b 0.239. Rule: beat
  the incumbent by >= 0.03 on human gold and not lose on dev consensus;
  llama3.3:70b+v2 won (+0.108) and is now the default. The owner confirmed
  it for the full network run unless a newer model beats it by the same
  rule. Round 2 (same packet, Ollama 0.40.6; `tournament-round1.txt` keeps
  round 1): no challenger passes. qwen3.8:27b-mlx+v2 0.405 (0.629, best on
  dev consensus of any config), nemotron-3.5-lightning 0.258 (0.248);
  nemotron-3.5-bolt does not run (missing Metal MoE kernel in 0.40.6's
  llama.cpp). llama after the upgrade's model conversion: byte-identical
  labels. Structured output (`+json`) left every label unchanged for llama
  and qwen3.8 at the same speed (neither had parse errors; llama has none in
  5,308 bulk labels either), so it is insurance only. Decision models as
  labels: clef 0.373 (0.528), nimble 0.373 (0.457), tev1 0.212, clef-flash
  0.211; the `-gated` copies (aimed < 0.5 folds to neutral) are worse
  everywhere because P(aimed) runs low, so the gate is wrong at 0.5. As
  probabilities they rank well: P(hostile+) AUC vs human gold nimble 0.861,
  tev1 0.850, clef 0.824 (llama labels 0.863, qwen3.5+v2 0.873, toxic-bert
  insult 0.790); vs judge consensus clef 0.842 (0.876 times P(aimed)), best
  of all configs. tev1 takes ~1.3 s/item, nimble ~3, clef ~8. The "contrast" and
  "reason" prompt variants did not help any model on human gold. gemma4 agrees
  with the judges best but with the humans less: judge consensus alone would
  have picked a different winner.
- Network round (2026-10-17; interim network packet plus the study packet;
  every chat config gets the bulk pipeline's direction pass, so the fair
  incumbent is llama's two-pass pipeline). Weighted kappa, human gold / study
  consensus dev / network dev / network test: gemma4:31b+contrast with the
  direction pass 0.519 / 0.674 / 0.559 / 0.579; llama3.3:70b+v2 two-pass
  0.488 / 0.468 / 0.306 / 0.459 (one pass 0.535 on human gold);
  qwen3.8:27b-mlx+v2 two-pass 0.405 / 0.647 / 0.468 / 0.674; clef gated
  0.213 / 0.397 / 0.614 / 0.525 (it also folds real attacks on the target).
  gemma4+contrast is the only config near the top on all four and beats the
  two-pass incumbent by 0.031 on human gold, so it labels Stage C and the
  interim sample (Claude's call, per the owner's delegation of labeler
  choices). ~3 s/item; ~1% parse errors, all on replies both judges call
  benign. The contrast prompt hurts llama (human gold 0.482, two-pass 0.450;
  network dev 0.18). A median of clef gated, gemma4 and qwen3.8 is best on
  network (0.644 / 0.693) but 0.45 on human gold and ~44 h per Stage C;
  clef's P(hostile+) x P(aimed) ranks network hostility at AUC 0.954.
  Revisit both once a surge-level judge packet exists.
- Surge gold comes from whole threads, not single replies (2026-10-17 thread
  round): single replies miss screenshots, banter and who is aimed at.
- gemma4 is a screen, not gold (2026-10-17). On the network packet its
  author-directed hostile calls are loose: 11 of 26 are consensus-hostile
  (16 of 36 counting either judge), recall 11 of 12, and its confidence is
  flat (~0.9 on right and wrong calls alike). Best hostile F1 of any config
  (0.58; llama two-pass 0.39), so no swap helps; instead the two judges
  confirm every hostile call (`pileon_lab.confirm`, runbook step 4), which
  costs ~5% of items. Network pages and nuke runs use the confirmed labeler
  `gemma4:31b+contrast@confirmed`.

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
   --per-surge 30` (stratified by size, warm-up excluded; resumable). The
   default labeler is `gemma4:31b` + contrast (network round winner, see
   Constraints), ~3 s/item, so 500 surges (~13k items) take ~14 h with the
   direction pass; `qwen3.8:27b-mlx` + v2 is ~1.6 s/item if speed matters
   more than accuracy.
2. Features: `uv run python -m pileon_lab.network --surges S --cache ...
   --labels ... --enrich --out ../data/lab/network-features.jsonl` (same
   sample/seed), then `pileon_lab.sweep --features ... --template
   web/sweeps.template.html --page ... --corpus-note "..."`.
3. Gold and hill-climbing: `pileon_lab.evalset packet --surges S --cache ...
   --labels ... --size 300 --dir ../data/lab/eval-net`; run the two judge
   agents on `packet.jsonl` (outputs `opus.jsonl`, `fable.jsonl`, blind, same
   rules as the study calibration); `evalset run --dir ... --model M
   [--variant V]` for each candidate; `evalset score --dir ...` (dev split)
   ranks them against judge consensus (judges on a blind copy outside the
   repo, as in step 4). Candidates must also not regress on
   the human gold: `evalset run --dir ../data/lab/judges --model M` then
   `pileon_lab.judges score` on the study packet. Read `--split test` only
   for the final choice. Prompt variants live in `codebook.VARIANTS`;
   `evalset run --variant` also takes `<variant>+json` (structured output
   held to `codebook.LABEL_SCHEMA`) and `systemone` (a decision model: stance
   probabilities, P(aimed at the target), and an ordinal severity score; also
   writes a `-gated` copy that folds unaimed hostility to neutral).
   `evalset bulk --dir ... --labels ...` writes the bulk labeler's stored
   labels for the packet (`@bulk`, and `@bulk-directed` with the direction
   pass applied) so `score` ranks them too; `evalset direct --dir ... --model
   M --variant V` gives a chat run the same direction pass (`-directed`), so
   challengers compete with the bulk pipeline's two passes, not one.
   `CONFIGS="model variant,..." lab/scripts/tournament.sh DIR` runs a chosen
   subset on any packet, direction passes included.
4. Confirm the screen's hostile calls: `uv run python -m pileon_lab.confirm
   packet --surges S --cache ... --labels ... [--sample-size N] --judged
   <judged packet dirs> --dir ../data/lab/confirm-<run>` writes every
   unjudged author-directed hostile call plus 120 random decoys (blind;
   `strata.json` says which is which). Copy only `packet.jsonl` and
   `codebook.md` to a fresh directory outside the repo (the scratchpad) and
   run the two judges there with fresh context (a general-purpose agent
   inheriting the session's Opus model, and a Fable agent), never a fork: a
   fork knows the packet is enriched, and so does any agent that reads files
   under the repo, because this CLAUDE.md then loads into its context
   (happened 2026-10-17; the exposed runs are kept as `*@exposed.jsonl`).
   Copy their outputs back as `opus.jsonl` and `fable.jsonl`. Then
   `confirm merge --labels ... --judged <every judged dir>` stores
   `<labeler>@confirmed` (each judged call takes the median of screen and
   judges) and prints the confirmation rate and the decoy miss estimate.
   Rebuild features (step 2) and rerun `nuke` with `--labeler
   gemma4:31b+contrast@confirmed`.

   The packet's toxic stratum (`--toxic 80 --toxic-pool 400`, toxic-bert
   insult, `uv sync --group baselines`) and `pools.json` give a stratified
   miss estimate. Tell judges to keep helper scripts inside their own
   directory: shared scratchpad scripts collided on 2026-10-17.
5. Thread round, the primary gold (`lab/PREREGISTRATION.md`): `uv run
   python -m pileon_lab.threads packet --surges S --cache ... --features
   <confirmed features> --screen-features <screen-only features>
   --sample-size N --dir ../data/lab/threads-<run>`. It writes chunked
   packets, each surge one conversation:
   - the target's posts with their parents;
   - the quoter's post and the other quote posts;
   - the replies under both.

   Packets carry no counts, handles, links or timestamps. `key.json` and
   `strata.json` stay out of the blind copies. Copy each chunk with
   `codebook.md` into its own blind directory outside the repo and run one
   judge per directory, under step 4's rules. Concatenate the outputs into
   `opus.jsonl` and `fable.jsonl`, then validate their coverage. `threads
   score --dir ... --features ... --labels ... --isolated <item packet
   dirs> [--models gemma4_31b]` compares the holistic, in-context and cascade
   measures. `threads run` adds a local model's verdicts and labels
   (`--num-ctx 16384`); never run it while Stage C labels on the same host.
6. Grade the frozen detectors exactly as `lab/PREREGISTRATION.md` says:
   1. `pileon_lab.detector early --surges S --cache ... --features
      <confirmed features> --out <early rows>` adds the 15-minute signals.
   2. `pileon_lab.heldout plan --surges S --features <early rows>
      --screen-features <screen-only features> --references
      detector/retro-reference.json detector/early-reference.json
      --interim-surges ../data/surges/interim.jsonl.zst --study-db
      ../private/qp-pileon/data/crawl.db --out plan.json` assigns each
      surge a stratum (screen, alert, a seeded random 25%) and a weight,
      flags overlap with the dev data, and prints attrition.
   3. `threads packet --plan plan.json --surges S --cache ... --dir ...`,
      then judge the packet as in step 5.
   4. `heldout gold --plan plan.json --threads <dir> --features <early rows>
      --measure primary|hostile|cascade [--include-overlap] --out gold.json`.
   5. `detector grade --reference lab/detector/{retro,early}-reference.json
      --features <early rows> --gold gold.json`.
   6. `heldout alerts --gold gold.json --features <early rows> --reference
      detector/early-reference.json --threads <dir> --surges S --cache ...
      --groups-out groups.json` prints the lead time. Then `nuke --surges S
      --labels ... --labeler gemma4:31b+contrast@confirmed --cache ...
      --features ... --groups groups.json --offline` gives collateral for
      true and false alerts.

   Never refreeze the references on held-out data.
7. The trawl, a pile-on-rich frame (`lab/PREREGISTRATION.md`, amendment 1;
   `pileon_lab.trawl`):
   1. `trawl score --surges <full run> --interim-surges ... --study-db ...
      --cache ../data/lab/cache-trawl.db --out trawl/phase1.jsonl` scores
      every eligible held-out surge on the frozen retro detector's cheap
      families, from batched counts (about 30 minutes).
   2. Commit and push any change to the design first. Then `trawl plan
      --phase1 ... --reference detector/retro-reference.json --surges <full
      run> --dir trawl` writes tiers, the seeded draw, and the drawn surges'
      lines.
   3. `network --surges trawl/surges.jsonl --sample-size 100000 --enrich`
      and `detector early` on the same file give full features.
   4. `trawl union --plan heldout/plan.json trawl/plan.json --surges <full
      run> --dir threads-src` merges both plans. Build one thread packet from
      it (`threads packet --plan threads-src/plan.json --surges
      threads-src/surges.jsonl --sample-size 100000`) and judge it once.
   5. `trawl report --dir trawl --threads <judged dir>` prints the tiers and
      tests and writes `gold-primary.json` and `gold-hostile.json` for
      `detector grade`.

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
attack, so defenses should keep those as two separate dials. About half of
all hostile replies in targets' threads are aimed at someone other than the
target (6% in pile-ons, about half in rallies, two thirds in spotlights), so
an abuse gate that ignores who a reply is aimed at would block defenders and
bystanders about as often as attackers outside pile-ons. Off-the-shelf
toxicity/sentiment models rank hostility decently but trail the
context-aware labeler at usable thresholds (`pileon_lab.baselines`).

Nuclear block (`pileon_lab.nuke`, study incidents): blocking who liked the
dunk barely matters (about 10% of attackers ever liked it; <1% of later harm
stopped). Blocking the amplifier's audience stops about a quarter of later
pile-on harm but silences about as large a share of supporters. A
followers-only reply gate stops ~94% of later pile-on harm and silences ~86%
of supporters (72% on a rally). Blocking an attacker at their first hostile
reply stops ~1%: harm is one-shot per author. Every structural rule sits near
the diagonal (harm stopped ~= supporters silenced), so prevention without
collateral has to filter exposure by text label and direction, with the gate
as the blunt emergency option.

Network-wide surges (interim, Aug 19-26, 300-item judge packet stratified
half bulk-hostile; Opus and Fable judges agree on 260): author-directed
hostility is rare (13 and 19 hostile, 0 abusive), and most negativity is at
third parties the target posted about. llama3.3:70b+v2, the study's winner,
does not transfer: of the 150 items it labeled hostile the judges agree 12
are hostile and 103 neutral; after its direction pass, 64 remain (12 hostile,
28 neutral by consensus). Weighted kappa vs consensus 0.31 dev / 0.46 test
with the direction pass, 0.19 / 0.29 without; hostile recall 1.00, precision
0.18 / 0.32. It also calls many neutral replies supportive. The study's
packet (one amplifier's targets, hostility mostly at them) hid this third-party
failure, so the labeler choice for network labels has to be made on the
network packet.

Interim network sweep on confirmed labels (2026-10-17; 200 surges, 5,308
replies; gemma4 screen, judges confirm, `pileon_lab.confirm`). Of gemma4's
258 author-directed hostile calls, 70 survive (27%, 95% CI 22-33%; both
judges on 47); the rest are mostly jokes, banter and puns it read as
mockery. No random decoy is hostile by both judges (0 of 120), so misses
look rare but the bound is loose (0-155 of 4,983). Confirmed author-directed
hostility is 1.3% of replies. Judge exposure check: a clean rerun of each
judge matched its run with this file in context at weighted kappa 0.91
(Opus) and 0.87 (Fable); exposed runs called ~10% fewer replies hostile.
Of 197 surges with 10+ labeled replies, 6 are pile-ons (3%), 146 rallies,
45 spotlights, no crossfire (on unconfirmed labels: 18 pile-ons). With 6
positives every AUC is wide, but the study's structural proxies point the
same way: quoter/target followers 0.86 (bootstrap 95% 0.73-0.98), dunk vs
post likes 0.84 (0.70-0.95), top reply vs post likes 0.82 (0.71-0.92),
replies vs the target's previous high 0.78 (0.63-0.93); pile-on targets are
smaller (target followers 0.21) and draw fewer likes per reply (0.27). On
unconfirmed labels the same proxies score 0.51-0.72, so screen noise hides
real signal and grading must use confirmed labels. In pile-ons, 49% of
labeled replies from the quoter's audience are contempt at the target (0.8%
in other surges). Nuke on the 5 pile-ons with a dunk (28 later harm
replies): audience block stops 57% of harm and silences 9% of supporters,
the gate 86% / 73%, strike 25% / 5%; far too few to generalize, Stage C
decides.

Thread round on interim (2026-10-17 night; `pileon_lab.threads`, 28
enriched surges, 1,352 posts per judge, details in `private/NOTES.md`). With
the whole conversation visible, neither judge calls any surge a pile-on, the
5 cascade pile-ons included; both call the same one surge a crossfire. In
context the judges agree closely (weighted kappa 0.71, contempt kappa 0.96).
Context removes contempt: of replies a judge had called contempt when shown
alone, Opus withdrew 9 of 22 and Fable 19 of 31. Four of the 5 cascade
pile-ons carry little or no contempt toward the target in context. In two of
them the target's post was a screenshot mocking someone else, which item
packets never show (they carry the target post's text only). So
reply-by-reply labels are not valid surge gold. The pre-registration's
primary gold is the pooled in-context class, and the cascade is now only the
screen. gemma4 as a holistic judge repeats the screenshot error (kappa 0
against the judges), and its in-context post labels agree with the judges at
weighted kappa 0.38 (Opus) and 0.33 (Fable), against 0.71 between the
judges, so in-context gold needs the judges. True pile-ons look rare network-wide: 0 or 1 of 28
surges enriched for them. The detector prototype (`pileon_lab.detector`,
four families of structural signals, frozen references in `lab/detector/`)
scores AUC 0.93 against interim cascade labels, but that gold is now
suspect; the held-out grade decides.

Held-out grade (2026-10-18; `lab/PREREGISTRATION.md` and its amendment 1;
details in `private/NOTES.md`). Both judges read 521 held-out surges as whole
threads (106 blind agents; holistic pile-on kappa 0.97, label weighted kappa
0.93).
- **Stage C sample** (159 judged surges with no dev overlap, 8 pile-ons in
  context): the frozen retro detector scores AUC 0.977 (95% 0.95-0.99) and
  the early one 0.888 (0.78-0.97). With only 8 positives, both are
  provisional. At the frozen 10% cutoff, precision is 0.16 and recall 1.00.
  The early grade is not a live estimate (amendment 2): a surge triggers a
  median 39 min after its backdated onset, and the early quoter is the
  whole-surge one, whose quote came after onset + 15 min in 5 of the 8
  positives. The live variant (everything cut at the trigger) replaces it.
- **The trawl** (all 62,792 eligible held-out surges, tiered by a cheap
  structural score of reach and approval alone): in-context pile-on share is
  85% (79 of 93) in the top 0.5%, 56% in the next 1.5%, 10% in the next 8%,
  and 2% in the rest. The rally tier is 0% pile-on and 88% rally. The trend
  test gives p < 1e-15, enrichment is 24x over the frame rate of 3.6%, and
  the tag meets the notification-grade bar.
- So structural proxies find pile-ons and rallies without text. Pile-ons
  cluster in one niche: a much bigger quoter than target, few likes per
  reply, and a dunk that out-likes the target post.
- The trawl measures precision per tier well, but it cannot rank detectors.
  One bottom-tier pile-on carries about 40% of the weight, so every weighted
  AUC there has a 95% interval about 0.5 wide: four families 0.65, the cheap
  two-family score 0.71. On Stage C, judged on the same surges, the frozen
  four-family detector (0.977) beats the two-family score (0.941; approval
  alone 0.995). So the frozen detector stays, and the cheap score serves as
  a pre-filter. (Exploratory comparison, not pre-registered.)
- Defenses replayed on the judged held-out surges, with in-context labels and
  armed 15 minutes after onset (before most surges trigger; armed at +60 min
  the audience block stops 53% and blocks 8% of supporters):
  - On 88 pile-ons, blocking the quoter's audience stops 58% of later harm
    and blocks 17% of supporters, but there are only 30 supporters to count.
  - In 146 rallies, the same block reaches only 5% of supporters.
  - A followers-only gate stops 99.7% of pile-on harm but blocks 77% of
    supporters (59% in rallies).
  - Network-wide, unlike in the study, cutting the quote's audience is a
    low-collateral defense. Detaching the quote is the softer form of that
    cut, so 58% is its upper bound.
- Detaches as targets do them today (natural experiment,
  `lab/scripts/natural_detach.py`; recorded postgates; surges matched on the
  previous two hours and cumulative arrivals; placebo 1.01):
  - After 828 mid-surge detaches, arrivals over the next 6 hours run at 1.03
    (95% 0.95-1.11) of matched surges.
  - Detaching the top quote gives 0.97 (0.79-1.23, n 98), and replies alone
    give 1.11 (1.02-1.21).
  - Disabling quotes mid-surge gives 1.08 (1.03-1.13), or 0.91 (0.83-1.00)
    when done more than 2 hours in.
  - The median detach comes 6 hours after onset.
  - So a detach does not slow the crowd already there. The quoter's audience
    keeps arriving, and only blocking or gating that audience removes it. The
    outcome here is volume, not harm, and a detach armed at +15 min is
    untested.
