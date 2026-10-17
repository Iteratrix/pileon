# pileon

Research and tools for Bluesky pile-ons: finding them, understanding what
separates them from ordinary virality, and giving targets defenses they can
tune.

## Vocabulary

A **surge** is a burst of strangers arriving at someone's posts, measured
against that person's own normal. Surges split by the tone of the arrivals:

|                    | little support | lots of support |
|--------------------|----------------|-----------------|
| **lots of hostility** | **pile-on**    | **crossfire**   |
| **little hostility**  | **spotlight**  | **rally**       |

The account whose post set off a surge is its **amplifier**; the amplifier of a
pile-on is an **instigator**.

## What's here

- `crates/pileon-core` - the event model: Jetstream v2 events (live WebSocket
  frames and archive-replay lines) parsed into one shape and mapped to compact
  rows. Post rows carry reply, quote and mention edges, never post text.
- `crates/pileon-recorder` - `pileon-recorder live` tails the Jetstream live
  stream; `pileon-recorder ingest` records an archive replay from stdin. Both
  write hourly zstd JSONL partitions with a durable cursor and a disk floor.
- `replay/` - a one-shot replay of the Jetstream v2 Network Replay archive
  using the official `@bsky/jetstream` SDK, piped into the recorder.
- `deploy/` - systemd user units for the recorder and the replay.
- `lab/` - Python research code: polite cached AppView/PDS fetching, relative
  incident features (reply lifts against the target's baseline, like ratios,
  who arrives), an AUC ranking and an interactive sweep page, and a harness for
  calibrating LLM valence judges against human gold labels.

## Principles

- Deletions are honored: nothing keeps the text of deleted records.
- Published results are aggregates. No lists of accounts or handles of private
  people leave the research.
- No automated interactions that notify anyone. Blocks and labels are fine;
  automated replies are not.
- Every detection threshold ships with its measured tradeoff, not a bare
  number.

## Running

```sh
cargo test --workspace
cargo install --path crates/pileon-recorder
pileon-recorder live --root data/jetstream/live

cd lab && uv run pytest
```

The archive replay needs a Jetstream API key from bsky.network.

## License

MIT
