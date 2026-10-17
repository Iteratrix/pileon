# pileon

Research and tools for Bluesky pile-ons: detect them, attribute them, and give
targets tunable defenses. Monorepo; started 2026-10-16 from a private
quote-post pile-on study and the `nuclear-block` idea
(myrrlyn, bsky post `3mwogfnvves2a`: "block this account and accounts tightly
interlinked with it ... the thermonuclear block").

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
   trait, like `../bluesky-crawler`); research stays in Python where it reuses
   the study code.

Every dial (detection sensitivity, label confidence, block yield, abuse
threshold) ships with its measured tradeoff curve, never a bare magic number.

## Layout

- The private study lives outside the repository.

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
  `getSegment` / `getBlock` need an API key (bsky.network/account) and are
  metered in downloaded bytes (quota not published). Compaction **physically
  removes deleted records**, so the archive has survivorship bias. Replies whose
  parent no longer resolves still reveal deleted target posts.
- **Constellation** (constellation.microcosm.blue): backlink index (who
  replied/quoted/liked/reposted a target, counts, distinct DIDs); API marked
  unstable; drops deleted records. Client in `studies/qp-pileon/src/qp_pileon/constellation.py`.
- **Public AppView** (public.api.bsky.app): threads, likes with timestamps,
  quotes, profiles, relationships; ~3000 req / 5 min / IP.
- Static datasets (Zenodo Bluesky Social Dataset through early 2024; HF
  `tiagozip/bluesky`) are less complete than the archive and carry consent
  controversy. Prefer the archive.

Measured live volume (100 s sample, Fri 16:53 PDT), per day: 3.1M posts
(1.5M replies, 0.25M quotes), 20M likes, 3.5M reposts, 2.6M follows, 0.34M
blocks, 0.1M postgates; ~20 GB/day raw JSON across those collections.

## Constraints

- Honor deletions (Bluesky developer guidelines). Never keep text of deleted
  records; survivorship analysis uses aggregates.
- Anything published reports aggregates. Never publish lists or handles of
  non-public accounts (no "swarm lists" leave this repo).
- No automated interactions that notify users (guidelines' spam rule). Blocks
  and labels are fine; auto-replies are not.
- Valence labeling needs the parent/quoted post as context (41% of labels
  flipped without it, per the study).
