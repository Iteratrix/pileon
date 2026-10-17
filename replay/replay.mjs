// Streams one Jetstream v2 host's archive window to stdout as JSON lines, for
// `pileon-recorder ingest`. The recorder owns progress: on restart this script
// resumes after the `last_seq` in the recorder's state.json, so the two never
// disagree about what has been recorded.
//
// Exit codes: 0 done, 1 error, 3 replayed up to the sealed tip but --before is
// not sealed yet (rerun later to continue).
import { Jetstream } from '@bsky/jetstream'
import { existsSync, readFileSync } from 'node:fs'
import { once } from 'node:events'
import { parseArgs } from 'node:util'

const COLLECTIONS = [
  'app.bsky.feed.post',
  'app.bsky.feed.like',
  'app.bsky.feed.repost',
  'app.bsky.graph.block',
  'app.bsky.graph.follow',
  'app.bsky.feed.postgate',
  'app.bsky.feed.threadgate',
  'app.bsky.graph.listitem',
  'app.bsky.graph.listblock',
]
const USER_AGENT = 'pileon-replay/0.1 (github.com/Iteratrix/pileon)'

const { values: args } = parseArgs({
  options: {
    host: { type: 'string', default: 'jetstream.us-west.bsky.network' },
    after: { type: 'string' },
    before: { type: 'string' },
    state: { type: 'string' },
    key: { type: 'string', default: `${process.env.HOME}/.config/pileon/jetstream.key` },
  },
})
if (!args.after || !args.before) {
  console.error('usage: replay.mjs --after SEQ --before SEQ [--state state.json] [--host H]')
  process.exit(1)
}
const before = Number(args.before)
let after = Number(args.after)
if (args.state && existsSync(args.state)) {
  const state = JSON.parse(readFileSync(args.state, 'utf8'))
  if (state.host !== args.host) {
    console.error(`state is for ${state.host}, not ${args.host}`)
    process.exit(1)
  }
  if (state.last_seq != null && state.last_seq > after) after = state.last_seq
}
if (after >= before) {
  console.error(`nothing to do: after ${after} >= before ${before}`)
  process.exit(0)
}

const apiKey = readFileSync(args.key, 'utf8').trim()
const service = `https://${args.host}`

const plan = await fetch(`${service}/xrpc/network.bsky.jetstream.planSnapshot`, {
  method: 'POST',
  headers: {
    Authorization: `Bearer ${apiKey}`,
    'Content-Type': 'application/json',
    'User-Agent': USER_AGENT,
  },
  body: JSON.stringify({
    kinds: ['commit'],
    collections: ['app.bsky.graph.listblock'],
    afterSeq: before - 1,
    beforeSeq: before,
  }),
})
if (!plan.ok) {
  console.error(`planSnapshot HTTP ${plan.status}: ${(await plan.text()).slice(0, 300)}`)
  process.exit(1)
}
const { sealedTipSeq } = await plan.json()
const target = Math.min(before, sealedTipSeq)
if (target <= after) {
  console.error(`archive sealed only through ${sealedTipSeq}; nothing new after ${after}. Retry later.`)
  process.exit(3)
}

process.stdout.on('error', (err) => {
  console.error(`stdout closed (${err.code}); stopping`)
  process.exit(err.code === 'EPIPE' ? 0 : 1)
})

const dropBinary = (_, value) => (value instanceof Uint8Array ? undefined : value)
const span = target - after
const started = Date.now()
let lastLog = started
let events = 0
let seq = after
console.error(`replaying ${args.host} seq (${after}, ${target}] of (${after}, ${before}]`)
const jetstream = new Jetstream({ service, apiKey })
for await (const evt of jetstream.snapshot({ collections: COLLECTIONS, afterSeq: after, beforeSeq: target })) {
  if (!process.stdout.write(`${JSON.stringify(evt, dropBinary)}\n`)) {
    await once(process.stdout, 'drain')
  }
  events += 1
  seq = Number(evt.seq)
  const now = Date.now()
  if (now - lastLog >= 60_000) {
    const secs = (now - started) / 1000
    const done = (seq - after) / span
    const eta = done > 0 ? ((secs / done) * (1 - done)) / 3600 : NaN
    console.error(
      `seq ${seq} (${(done * 100).toFixed(2)}%), ${events} events, ${(events / secs).toFixed(0)}/s, eta ${eta.toFixed(1)}h`,
    )
    lastLog = now
  }
}
console.error(`done: ${events} events through seq ${seq} in ${((Date.now() - started) / 1000).toFixed(0)}s`)
if (target < before) {
  console.error(`stopped at the sealed tip ${target}; ${before - target} seqs remain, rerun later`)
  process.exit(3)
}
