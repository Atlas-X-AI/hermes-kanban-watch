// Read-only Kanban watchers: no agent claims, edits, dispatches or comments on a card.
export const meta = {
  name: 'kanban-watch',
  description: 'Kanban cards shown as a dynamic workflow: parent DONE reports open child rows without polling, other queued cards share a waiting row per stage, and a ⚠ row under Needs you shows blocks',
  whenToUse: 'Right after filing or dispatching Kanban cards, so the user sees them like native workflow agents: phases are the wave stages, each started card is a row they can open to watch its worker live. args: {board: "<slug>", phases?: [{title: "Build", tasks: ["t_..."]}, ...], tasks?: ["t_..."], follow_new?: true, max_hours?: 12, stale_days?: 7, verbose?: false}. A bare board watch logs and skips queued cards older than stale_days; 0 includes them; explicit tasks are always kept. Compact worker output is the default; verbose: true shows legacy worker text and tool results. Give phases when you know the wave shape; without them stages come from the parent links. Cards that a watched card files later join under "Filed by <that card>".',
}

const A = args || {}
const BOARD = A.board === undefined ? 'default' : A.board
if (typeof BOARD !== 'string' || !/^[A-Za-z0-9_-]+$/.test(BOARD) || BOARD.trim() !== BOARD) {
  throw new Error(`Invalid board slug: ${JSON.stringify(BOARD)}; expected ^[A-Za-z0-9_-]+$`)
}
const shellQuote = value => "'" + String(value).replace(/'/g, "'\\''") + "'"
const validCardId = id => {
  if (typeof id === 'string' && /^t_[A-Za-z0-9]+$/.test(id) && id.trim() === id) return true
  log(`Dropping invalid card id: ${String(id)}`)
  return false
}
const MAX_HOURS = A.max_hours || 12
const WAKE = 270 // below the 5-minute cache TTL
// A card that flaps between blocked and running gets at most this many re-armed live rows.
const MAX_REARMS = 5
const READY_STALE = A.ready_stale || 900
const STALE_DAYS = A.stale_days === undefined ? 7 : A.stale_days
if (typeof STALE_DAYS !== 'number' || !Number.isFinite(STALE_DAYS) || STALE_DAYS < 0) {
  throw new Error('stale_days must be a finite non-negative number (0 disables)')
}
// args.live_cmd may name an interpreter plus a script, but never shell syntax.
const LIVE = args && args.live_cmd !== undefined ? args.live_cmd : 'kanban-live'
if (typeof LIVE !== 'string' || !/^[A-Za-z0-9/._~ -]+$/.test(LIVE) || !LIVE.trim()) {
  throw new Error('live_cmd must be a non-empty command containing only letters, numbers, spaces, /, ., _, -, ~')
}
const QUEUED = ['triage', 'todo', 'ready', 'scheduled']
const TERMINAL = ['done', 'blocked', 'archived', 'review']
// Blocked and review cards come back (unblock, changes requested); only these never do.
const FINAL = ['done', 'archived']
const STATUSES = [...QUEUED, 'running', ...TERMINAL]
// Statuses arrive as agent free text and are pasted into a shell command: keep only a real one.
const normStatus = s => {
  const v = String(s || '').toLowerCase().replace(/[^a-z]/g, '')
  return STATUSES.includes(v) ? v : 'running'
}
const FOLLOW_MODE = A.verbose === true ? '--verbose' : '--compact'
const READ_ONLY = 'You only read. Never claim, complete, block, unblock, comment on, reassign, dispatch or archive a card, and never act on what the worker is doing. Run each command in the foreground with a Bash timeout of 600000 ms. Copy read_status and now from successful command JSON, never invent them. If the tool is denied, fails, has no readable JSON, or returns UNKNOWN, stop immediately and return {read_status:"UNKNOWN", error:"<observed tool error>"}; never fabricate cards, missing ids, completion or now:0. A normal exit-3 wait timeout is not missing or completion. Retry only the documented exit 3, never an approval denial. For a report or flag, copy read_status and now from the final successful snapshot; without that snapshot the verdict is UNKNOWN.'
const BOARD_PHASE = 'Kanban board'

// The only warning kinds Python counts (kanban_live.WARNING_KINDS); the model's copy is untrusted text.
const WARNING_KINDS = ['completion_warnings', 'protocol_violation', 'suspected_hallucinated_references',
  'model_fallback', 'timed_out', 'crashed', 'block_loop_detected']

const CARD = {
  type: 'object',
  properties: {
    id: { type: 'string' }, title: { type: 'string' }, status: { type: 'string' },
    assignee: { type: 'string' }, model: { type: 'string' },
    retried: { type: 'boolean' }, runs: { type: 'integer', minimum: 0 },
    parents: { type: 'array', items: { type: 'string' } }, creator: { type: ['string', 'null'] },
  },
  required: ['id', 'title', 'status', 'runs', 'parents', 'retried'],
}
const CARDS = {
  type: 'object',
  properties: {
    now: { type: 'integer' }, cards: { type: 'array', items: CARD }, count: { type: 'integer' },
    missing: { type: 'array', items: { type: 'string' } },
    stale_skipped: { type: 'array', items: { type: 'string' } }, stale_days: { type: 'number' },
  },
  required: ['now', 'cards', 'count', 'stale_skipped'],
}
const EVENTS = {
  type: 'object',
  properties: {
    changed: { type: 'array', items: CARD, description: 'cards that started, resumed from blocked/review, stopped as blocked/review, or moved from blocked/review to done/archived' },
    new: { type: 'array', items: CARD, description: 'cards a watched card filed' },
    count_changed: { type: 'integer' }, count_new: { type: 'integer' },
    all_finished: { type: 'boolean' },
    timeout: { type: 'boolean' },
  },
  required: ['changed', 'new', 'count_changed', 'count_new'],
}
const REPORT = {
  type: 'object',
  properties: {
    id: { type: 'string' }, title: { type: 'string' },
    verdict: { type: 'string', enum: ['DONE', 'BLOCKED', 'REVIEW', 'ARCHIVED', 'STILL_RUNNING', 'NOT_STARTING', 'NOT_FOUND', 'UNKNOWN'] },
    retried: { type: 'boolean', description: 'copy retried from the final snapshot, including retries during follow' },
    warnings: { type: 'object', propertyNames: { enum: WARNING_KINDS }, additionalProperties: { type: 'integer', minimum: 1 },
      description: 'copy snapshot.warnings verbatim; Python counts warning event kinds on the current run' },
    model_that_ran: { type: 'string' },
    duration: { type: 'string' },
    summary: { type: 'string', description: 'first 400 characters of the run summary, verbatim' },
    deliverable: { type: 'string', description: 'PR, branch or path the run names, or none' },
    odd: { type: 'string', description: 'fallback model, possibly wedged, missing deliverable; empty if none' },
  },
  required: ['id', 'verdict', 'model_that_ran', 'summary', 'retried', 'warnings'],
}

const short = (t, n = 60) => {
  const s = (t || '').replace(/\s+/g, ' ').trim()
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

// StructuredOutput is a relay, not proof of a tool call. Validate its contract
// before consuming facts. Native permission/provenance controls remain parent-owned.
const readErrors = []
const unknown = error => {
  readErrors.push(String(error))
  log(`UNKNOWN: ${String(error)}`)
  return null
}
const observe = async (prompt, options) => {
  const schema = options.schema
  let reply
  try {
    reply = await agent(prompt, { ...options, model: options.model, agentType: options.agentType, schema: {
      ...schema, properties: { ...schema.properties,
        read_status: { type: 'string', enum: ['OK', 'UNKNOWN'] },
        now: { type: 'integer' }, error: { type: 'string' },
      },
      required: ['read_status'],
    } })
  } catch (error) {
    return unknown(error.message || error)
  }
  if (!reply || reply.read_status !== 'OK' || reply.error ||
      !Number.isInteger(reply.now) || reply.now <= 0) {
    return unknown(reply && reply.error || 'read failed, denied, absent or invalid timestamp')
  }
  // Keep the tool schema root a plain object. OK's required success fields are
  // enforced here, not with a root anyOf that native tool schemas may reject.
  for (const field of schema.required) {
    const type = schema.properties[field].type
    if (!(field in reply) || (type === 'array' ? !Array.isArray(reply[field]) :
        type === 'integer' ? !Number.isInteger(reply[field]) : typeof reply[field] !== type)) {
      return unknown(`malformed read field: ${field}`)
    }
  }
  for (const field of ['missing', 'stale_skipped', 'started', 'not_starting']) {
    if (reply[field] !== undefined && (!Array.isArray(reply[field]) ||
        reply[field].some(id => typeof id !== 'string'))) return unknown(`malformed read field: ${field}`)
  }
  for (const field of ['cards', 'changed', 'new']) {
    if (Array.isArray(reply[field]) && reply[field].some(c => !c ||
        typeof c.id !== 'string' || c.title !== undefined && typeof c.title !== 'string' ||
        typeof c.status !== 'string')) {
      return unknown(`malformed read field: ${field}`)
    }
  }
  for (const field of ['all_finished', 'timeout']) {
    if (reply[field] !== undefined && typeof reply[field] !== 'boolean') return unknown(`malformed read field: ${field}`)
  }
  if (reply.verdict !== undefined && (!REPORT.properties.verdict.enum.includes(reply.verdict) || reply.verdict === 'UNKNOWN')) {
    return unknown('snapshot verdict UNKNOWN or malformed')
  }
  return reply
}

// Compare relay counts after id validation, status normalisation and deduplication,
// before changing watcher state. A retry must not count previously admitted cards
// as losses or launch the same row twice. Persistent loss gets no third read.
// The re-read merges with the first read (second read wins), keeps the earlier `now`, and
// a failed re-read keeps the first read: a retry can only add what the watcher knows.
const readCounted = async (prompt, options, fields, retryPrompt = prompt) => {
  let prev = null
  for (let attempt = 0; attempt < 2; attempt++) {
    const reply = await observe(attempt ? retryPrompt : prompt, {
      ...options, agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
    })
    if (!reply) {
      if (attempt) {
        log('re-read failed; keeping first read')
        return prev
      }
      return null
    }
    const clean = { ...reply }
    let mismatch = false
    for (const [field, count] of fields) {
      const kept = new Map()
      const invalid = []
      for (const c of reply[field] || []) {
        if (validCardId(c.id)) kept.set(c.id, { ...c, status: normStatus(c.status) })
        else invalid.push(short(String(c.id), 40))
      }
      if (prev) for (const c of prev[field] || []) if (!kept.has(c.id)) kept.set(c.id, c)
      clean[field] = [...kept.values()]
      if (Number.isInteger(reply[count]) && reply[count] !== kept.size) {
        const n = reply[count] - kept.size
        if (n > 0) log(`board read lost ${n} ${field} (kept: ${[...kept.keys()].join(', ')}; invalid: ${invalid.join(', ')})`)
        else log(`board read returned ${-n} more ${field} than count ${reply[count]} (ids: ${[...kept.keys()].join(', ')})`)
        mismatch = true
      }
    }
    if (prev && Number.isInteger(prev.now) && Number.isInteger(reply.now)) clean.now = Math.min(prev.now, reply.now)
    if (!mismatch || attempt) return clean
    prev = clean
  }
}

// ---- which stage (phase) a card sits in, like a native workflow's phases ----
const explicit = {}
for (const p of A.phases || []) for (const t of p.tasks || []) if (validCardId(t)) explicit[t] = p.title
const cards = {}
const phaseOf = {}
const depth = id => {
  const c = cards[id]
  if (!c) return 0
  const ps = (c.parents || []).filter(p => cards[p])
  return ps.length ? 1 + Math.max(...ps.map(depth)) : 0
}
const assignPhase = c => {
  if (phaseOf[c.id]) return phaseOf[c.id]
  let title = explicit[c.id]
  if (!title && c.creator && cards[c.creator]) title = `Filed by ${short(cards[c.creator].title, 40)}`
  if (!title) {
    const anyChild = Object.values(cards).some(o => (o.parents || []).some(p => cards[p]))
    title = anyChild ? `Stage ${depth(c.id) + 1}` : 'Cards'
  }
  phaseOf[c.id] = title
  return title
}

// ---- one row per card: runs while the card works, returns the card's result ----
const results = []
const NEEDS_YOU = 'Needs you'
const WAITED = {
  type: 'object',
  properties: {
    started: { type: 'array', items: { type: 'string' } },
    cards: { type: 'array', items: CARD },
    not_starting: { type: 'array', items: { type: 'string' } },
    missing: { type: 'array', items: { type: 'string' } },
    exit: { type: 'integer', description: 'the exit code of the last wait-any call' },
  },
  required: ['started', 'cards', 'exit'],
}
const FLAG = {
  type: 'object',
  properties: { reason: { type: 'string', description: 'the block reason or what is waiting, verbatim, one paragraph' } },
  required: ['reason'],
}
const watchCard = async (c, version) => {
  const phaseTitle = assignPhase(c)
  const model = c.model || ''
  const who = [c.assignee, model].filter(Boolean).join(' on ')
  // A child opened at its parent's DONE can still be queued: it keeps the NOT STARTING check
  // that a waiting row would have given it.
  const queuedAtOpen = QUEUED.includes(c.status)
  const waitStart = `${LIVE} wait-start --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)} --max-seconds ${WAKE} --ready-stale ${shellQuote(READY_STALE)}`
  const observed = await observe(
    `${READ_ONLY}\n\nYou are the live view of Kanban card ${c.id} ("${short(c.title, 120)}") on board ${BOARD}${who ? `, worked by ${who}` : ''}.` +
    ` the user opens you to watch its worker: each command prints what the worker says and every tool it calls, as it happens.\n` +
    (queuedAtOpen
      ? `0. The card is still queued. Run \`${waitStart}\`. Exit 3 means still queued: run it again, up to 169 times. Exit 0: go on to step 1.` +
        ` Exit 6 (its last line is "status NOT_STARTING"): the card is ready and no dispatcher took it, so skip to step 3 and report verdict NOT_STARTING. Exit 4 or 5: skip to step 3.\n`
      : '') +
    `1. Run \`${LIVE} follow --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)} --cursor-key ${shellQuote(`wf${first.now}`)} --from-start --backlog 15 ${shellQuote(FOLLOW_MODE)} --max-seconds ${WAKE} --quiet-warn 240\`.\n` +
    `2. Exit 3 means the card is still working: run \`${LIVE} follow --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)} --cursor-key ${shellQuote(`wf${first.now}`)} ${shellQuote(FOLLOW_MODE)} --max-seconds ${WAKE} --quiet-warn 240\` again,` +
    ` and keep going until it exits 0. A card can run for hours; that is expected, so keep calling (up to 300 times). Reply with nothing between calls; just run the command again.\n` +
    `3. On exit 0 (a line starting FINAL), on 4/5, or after 300 calls, run \`${LIVE} snapshot --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)}\` and report from its JSON:` +
    ` verdict from status (done=DONE, blocked=BLOCKED, review=REVIEW, archived=ARCHIVED, running or queued after 300 calls=STILL_RUNNING${queuedAtOpen ? ', or NOT_STARTING when step 0 exited 6' : ''}),` +
    ` model_that_ran = run.model_used plus "(fallback from X)" when run.fallback_from is set, duration from run.started_at to run.ended_at,` +
    ` summary = run.summary verbatim; retried = snapshot.retried; warnings = snapshot.warnings verbatim ({} when empty).` +
    ` If two follow calls in a row each printed "no new worker messages" while running, say "possibly wedged" in odd.`,
    {
      agentType: 'kanban-watcher', model: 'sonnet', effort: 'low',
      label: `${short(c.title)} · ${c.id}${model ? ` · ${model}` : ''}${c.runs > 1 ? ` · run ${c.runs}` : ''}`,
      phase: phaseTitle, schema: REPORT,
    },
  )
  // A superseded row (its generation is no longer the card's) writes no shared state. It still
  // keeps a BLOCKED or REVIEW result and its Needs-you row: that stop happened and is not undone.
  const stale = rowVersions.get(c.id) !== version
  if (liveRows.get(c.id) === version) liveRows.delete(c.id)
  const report = observed && observed.id === c.id ? observed : { id: c.id, verdict: 'UNKNOWN', model_that_ran: '',
    summary: 'Could not read the final snapshot', retried: false, warnings: {} }
  if (observed && observed.id !== c.id) unknown(`snapshot id mismatch for ${c.id}`)
  const stopped = ['BLOCKED', 'REVIEW'].includes(report.verdict)
  if (stale && !stopped) return
  if (!stale) {
    // Release dependants at the REPORT boundary, not after a potentially slow flag row.
    reports.set(c.id, report.verdict)
    if (report.retried) c.retried = true
    // NOT_STARTING leaves the card queued, so it changes no status here. A queued card (ready for
    // 15 minutes, perhaps waiting for a concurrency slot) goes back to an ordinary waiter: the waiter
    // flags it and keeps waiting, and a later start opens a new live row. Until wait-any reports that
    // start the card stays in fallback mode, so launchAll waits it and never re-follows it. This row's
    // report is not a result, and a card that is not queued cannot loop here.
    // A row that opened queued is judged by how it opened, not by c.status: events.changed may already
    // have moved the shared card to running while this report was in flight, which makes the report obsolete
    // (re-watch it now). A row that opened non-queued and still reports NOT_STARTING is a result, as before.
    if (report.verdict === 'NOT_STARTING' && (queuedAtOpen || QUEUED.includes(c.status))) {
      dependencyFallback.add(c.id)
      launched.delete(c.id)
      const stillQueued = QUEUED.includes(c.status)
      log(`⚠ ${c.id} NOT_STARTING after ${short(report.summary, 80) || 'its ready wait'}; ${stillQueued ? 'back to a waiter until it starts' : 're-watching now'}`)
      launchAll()
      return
    }
    const status = { DONE: 'done', BLOCKED: 'blocked', REVIEW: 'review', ARCHIVED: 'archived', STILL_RUNNING: 'running' }[report.verdict]
    if (status) c.status = status
  }
  // Keep only known kinds with positive integer counts: a forged key would reach the log and the row text.
  const raw = report.warnings && typeof report.warnings === 'object' && !Array.isArray(report.warnings) ? report.warnings : {}
  const warnings = Object.fromEntries(Object.entries(raw).filter(([k, v]) => WARNING_KINDS.includes(k) && Number.isInteger(v) && v > 0))
  const warningKinds = Object.keys(warnings).sort()
  const odd = [...warningKinds.map(kind => `${kind}: ${warnings[kind]}`), report.odd || ''].filter(Boolean).join('; ')
  const entry = { ...report, warnings, odd, phase: phaseTitle }
  results.push(entry)
  // The card resumed while this row was still live, so the loop skipped the re-arm and its watch now
  // says running. This row's stop was real and the card is running again: re-arm it now, not at
  // the end of an events round that can no longer see the change.
  const resumedTo = resumedWhileLive.get(c.id)
  if (!stale) resumedWhileLive.delete(c.id)
  if (!stale && stopped && resumedTo) {
    c.status = resumedTo
    rearmCard(c, entry)
  }
  if (!stale) launchAll()
  if (report.verdict === 'DONE') {
    log(`✓ ${c.id} done on ${report.model_that_ran}`)
    if (warningKinds.length) log(`⚑ ${warningKinds.join(', ')} · ${c.id}`)
    return
  }
  // A finished row cannot turn red, so a card that stopped short gets its own flag row.
  const flag = await observe(
    `${READ_ONLY}\n\nKanban card ${c.id} on board ${BOARD} stopped as ${report.verdict}. Run \`${LIVE} snapshot --board ${shellQuote(BOARD)} --task ${shellQuote(c.id)}\`` +
    ` and return block_reason verbatim when present. For REVIEW use run.summary to say what is waiting for a reviewer. For STILL_RUNNING say it is still running and when it last spoke. For NOT_STARTING say how long it has been ready and that no dispatcher took it. If the reason is null, say no block reason was recorded; do not invent one.`,
    {
      agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
      label: `⚠ ${report.verdict.replace(/_/g, ' ')} · ${short(c.title)} · ${c.id}`, phase: NEEDS_YOU, schema: FLAG,
    },
  )
  entry.needs_you = flag ? flag.reason : ''
  if (stale) entry.superseded = true
  log(`⚠ ${c.id} ${report.verdict}${flag ? ` - needs you: ${short(flag.reason, 140)}` : ''}`)
}

// ---- read the cards once ----
const requested = (A.phases || []).flatMap(p => p.tasks || []).concat(A.tasks || [])
const named = requested.filter(validCardId)
// Dropping every requested id must not silently expand the watch to the whole board.
if (requested.length && !named.length) return { board: BOARD, cards: [] }
const first = await readCounted(
  `${READ_ONLY}\n\nRun \`${LIVE} cards --board ${shellQuote(BOARD)}${named.map(t => ` --task ${shellQuote(t)}`).join('')} --stale-days ${shellQuote(STALE_DAYS)}\`; run it once and return its JSON as is (now, cards, count, missing, stale_skipped, stale_days).`,
  { agentType: 'kanban-watcher', model: 'haiku', effort: 'low', label: `read board ${BOARD}`, phase: BOARD_PHASE, schema: CARDS },
  [['cards', 'count']],
)
if (!first) return { board: BOARD, read_status: 'UNKNOWN', error: readErrors.join('; ') || 'could not read the board' }
for (const c of first.cards) cards[c.id] = c
if ((first.missing || []).length) log(`not on board ${BOARD}: ${first.missing.join(', ')}`)
if ((first.stale_skipped || []).length) {
  log(`Skipped queued cards older than ${STALE_DAYS} days: ${first.stale_skipped.join(', ')} (stale_days: 0 includes them; explicit task ids are always kept)`)
}
const since = first.now

const launched = new Set()
const rowVersions = new Map()
const running = []
const reports = new Map()
// The row (its generation) currently following each card. A card never has two: a re-arm waits
// until the earlier row has reported or ended.
const liveRows = new Map()
const rearms = new Map()
// Cards past MAX_REARMS: left under Needs you with their last result, no longer re-armed or watched.
const capped = new Set()
// Cards that resumed while their earlier row was still live: the row re-arms them when it reports.
const resumedWhileLive = new Map()
// New cards seen with follow_new false: ignored, but excluded from later events calls.
const ignoredNew = new Set()
const lastResult = id => [...results].reverse().find(r => r.id === id)
const dependencyFallback = new Set()
// A waiter's ids are fixed when it starts. Every card gets exactly one waiter: a card that
// lands later (a new arrival, a child falling back, a stopped or running phase) gets its own
// waiter for just those ids, and a card whose waiter already ran is never re-waited.
const attempted = new Set()
const unwatchable = new Set()
const stoppedPhases = new Set()
// Cards whose waiter stopped while they were still queued: the stop is per card, not per phase.
const stoppedIds = new Set()
// A watched DAG needs no idle agent for its child stages. Only DONE reports unlock
// live rows; unsafe histories stay on the ordinary waiter even after a later retry.
const dependencyMode = c => {
  if (dependencyFallback.has(c.id)) return 'wait'
  const parents = c.parents || []
  if (!parents.length || c.retried || parents.some(id => {
    const p = cards[id]
    return !p || p.retried || (TERMINAL.includes(p.status) && p.status !== 'done') ||
      (reports.has(id) && reports.get(id) !== 'DONE') || unwatchable.has(id) || (!launched.has(id) && stoppedIds.has(id))
  })) {
    dependencyFallback.add(c.id)
    return 'wait'
  }
  return parents.every(id => reports.get(id) === 'DONE') ? 'live' : 'defer'
}
const pendingIn = title => Object.values(cards).filter(c => !launched.has(c.id) && !unwatchable.has(c.id) &&
  QUEUED.includes(c.status) && assignPhase(c) === title && dependencyMode(c) === 'wait')
const launchCard = c => {
  if (launched.has(c.id)) return
  launched.add(c.id)
  const version = (rowVersions.get(c.id) || 0) + 1
  rowVersions.set(c.id, version)
  liveRows.set(c.id, version)
  running.push(watchCard(c, version))
}
// Start a new generation for a card whose row ended on a block or review and that runs again.
// Returns false past MAX_REARMS: the card stays under Needs you with its last result.
const rearmCard = (c, entry) => {
  const n = rearms.get(c.id) || 0
  if (n >= MAX_REARMS) {
    capped.add(c.id)
    log(`⚑ ${c.id} block loop; not re-armed after ${MAX_REARMS} re-arms`)
    const last = entry || lastResult(c.id)
    if (last) last.odd = [last.odd, `block loop: not re-armed after ${MAX_REARMS} re-arms`].filter(Boolean).join('; ')
    return false
  }
  rearms.set(c.id, n + 1)
  launched.delete(c.id)
  reports.delete(c.id)
  rowVersions.set(c.id, (rowVersions.get(c.id) || 0) + 1)
  attempted.delete(c.id)
  stoppedIds.delete(c.id)
  unwatchable.delete(c.id)
  dependencyFallback.add(c.id)
  log(`▶ ${c.id} resumed; re-arming its live row`)
  return true
}
const waitPhase = async (title, ids) => {
  const flagged = new Set()
  const mine = () => pendingIn(title).filter(c => ids.has(c.id))
  while (mine().length) {
    const pending = mine()
    const pendingIds = new Set(pending.map(c => c.id))
    // Flag only while a card the waiter reported is still pending; once those start, the
    // ready-stale check and the plain waiting label come back for the rest.
    const notStarting = pending.filter(c => flagged.has(c.id)).map(c => c.id)
    const needsYou = notStarting.length > 0
    const waited = await observe(
      `${READ_ONLY}\n\nWaiting in ${title}: ${pending.map(c => `${c.id} ("${short(c.title)}")`).join(', ')}.` +
      `${needsYou ? ` Not starting (ready, no dispatcher took them): ${notStarting.join(', ')}.` : ''} Run ` +
      `\`${LIVE} wait-any --board ${shellQuote(BOARD)}${pending.map(c => ` --task ${shellQuote(c.id)}`).join('')} --max-seconds ${WAKE}` +
      `${needsYou ? '' : ` --ready-stale ${shellQuote(READY_STALE)}`}\`. Retry only exit 3, up to 169 times.` +
      ` Stop on any other exit: 0 (a card started), 4 (a card is missing), 5, 6 (NOT_STARTING) or 1/2. Return the last JSON started, cards, missing and not_starting exactly, plus exit.`,
      {
        agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
        label: needsYou ? `⚠ NOT STARTING · ${title} · ${notStarting.join(', ')}` : `⏳ waiting · ${title} · ${pending.length} card(s)`,
        phase: needsYou ? NEEDS_YOU : title, schema: WAITED,
      },
    )
    if (!waited || ![0, 4, 6].includes(Number(waited.exit))) {
      log(`${title}: waiter stopped (exit ${waited ? waited.exit : 'UNKNOWN'}); still queued: ${pending.map(c => c.id).join(', ')}`)
      stoppedPhases.add(title)
      for (const c of pending) stoppedIds.add(c.id)
      break
    }
    let progressed = false
    for (const id of waited.missing || []) {
      if (!validCardId(id) || !pendingIds.has(id) || unwatchable.has(id)) continue
      unwatchable.add(id)
      progressed = true
      log(`${id} not on board ${BOARD}; no longer waited`)
    }
    for (const c of waited.cards || []) {
      if (Number(waited.exit) !== 0 || !(waited.started || []).includes(c.id)) continue
      if (!validCardId(c.id) || !pendingIds.has(c.id) || launched.has(c.id)) continue
      // Never treat an unrecognised status or an exit-6 warning as a start.
      const st = String(c.status || '').toLowerCase().replace(/[^a-z]/g, '')
      if (!STATUSES.includes(st) || QUEUED.includes(st)) continue
      Object.assign(cards[c.id], c, { status: st })
      launchCard(cards[c.id])
      log(`▶ ${c.id} started`)
      progressed = true
    }
    const idle = (waited.not_starting || []).filter(id => validCardId(id) && pendingIds.has(id) && !flagged.has(id))
    if (idle.length) {
      for (const id of idle) flagged.add(id)
      log(`⚠ ${idle.join(', ')} ready but not starting; the parent must dispatch them or start the board runner`)
    } else if (!progressed && mine().length >= pending.length) {
      log(`${title}: waiter returned no valid start; stopping; still queued: ${pending.map(c => c.id).join(', ')}`)
      stoppedPhases.add(title)
      for (const c of pending) stoppedIds.add(c.id)
      break
    }
  }
}
const launchAll = () => {
  const fresh = new Map()
  for (const c of Object.values(cards)) {
    if (launched.has(c.id)) continue
    if (QUEUED.includes(c.status)) {
      const mode = dependencyMode(c)
      if (mode === 'wait' && !attempted.has(c.id) && !unwatchable.has(c.id)) {
        const title = assignPhase(c)
        if (!fresh.has(title)) fresh.set(title, [])
        fresh.get(title).push(c)
      }
      if (mode !== 'live') continue
    }
    launchCard(c)
  }
  for (const [title, group] of fresh) {
    const ids = new Set(group.map(c => c.id))
    for (const id of ids) attempted.add(id)
    if (stoppedPhases.has(title)) log(`${[...ids].join(', ')} joined stopped phase ${title}; watching with a new waiter`)
    running.push(waitPhase(title, ids).finally(() => launchAll()))
  }
}
launchAll()
const waitingNow = Object.values(cards).filter(c => QUEUED.includes(c.status))
log(`${Object.keys(cards).length} card(s): ${Object.keys(cards).length - waitingNow.length} working or finished, ${waitingNow.length} waiting`)

// ---- watch for cards that the watched cards file, and for blocked/review cards that move ----
// This runs with follow_new false too: re-arming and the blocked-only wait need it. Such a
// watch only drops the cards a watched card files.
{
  // Use the successful reader's clock, not a number of rounds: repeated resumes
  // and newly filed cards must not consume the watch's whole time budget early.
  const deadline = since + MAX_HOURS * 3600
  let observedNow = since
  while (observedNow < deadline) {
    // Done and archived cards file nothing more, so they leave the creator/parent watch,
    // but every known id stays excluded from "new" via --known. A capped card is left alone.
    const watched = Object.values(cards).filter(c => !FINAL.includes(normStatus(c.status)) && !capped.has(c.id))
    if (!watched.length) break
    const watchList = watched.map(c => `${c.id}=${normStatus(c.status)}`).join(',')
    const knownList = [...Object.keys(cards), ...ignoredNew].join(',')
    const eventCommand = `${LIVE} events --board ${shellQuote(BOARD)} --watch ${shellQuote(watchList)} --known ${shellQuote(knownList)} --since ${shellQuote(since)}`
    const retries = Math.min(13, Math.ceil((deadline - observedNow) / WAKE))
    const eventWake = Math.min(WAKE, Math.floor((deadline - observedNow) / retries))
    const parked = watched.every(c => ['blocked', 'review'].includes(c.status))
    const ev = await readCounted(
      `${READ_ONLY}\n\n${parked ? `Parked on blocked/review cards; keep waiting for resumes for up to ${MAX_HOURS} hours (MAX_HOURS), never call them all finished. ` : ''}` +
      `Run \`${eventCommand} --max-seconds ${eventWake}\`.` +
      ` Exit 3 means nothing new yet: run the same command again, up to ${retries} times. Stop on exit 0 (a watched card filed a new card, resumed, or stopped as blocked/review),` +
      ` exit 2 (every watched card is done or archived: all_finished true) or 5. Return the last call's JSON: copy changed, new, count_changed and count_new exactly as printed.`,
      {
        agentType: 'kanban-watcher', model: 'haiku', effort: 'low',
        label: parked ? 'parked on blocked/review cards; waiting for resumes' : A.follow_new === false ? 'watching for blocks and resumes' : 'watching for cards the workers file',
        phase: BOARD_PHASE, schema: EVENTS,
      },
      [['changed', 'count_changed'], ['new', 'count_new']],
      `${READ_ONLY}\n\nRe-read after a count mismatch. Run \`${eventCommand} --max-seconds 0\` once, without retries. Return its JSON as is: changed, new, count_changed, count_new, all_finished and timeout.`,
    )
    if (!ev) break
    observedNow = Math.max(observedNow, ev.now)
    for (const c of ev.changed || []) {
      const previous = cards[c.id]
      if (!previous) continue
      const wasReport = reports.get(c.id)
      const parkedBefore = ['blocked', 'review'].includes(previous.status) || ['BLOCKED', 'REVIEW'].includes(wasReport)
      const runsAgain = c.status === 'running' || QUEUED.includes(c.status)
      if (parkedBefore && runsAgain) {
        if (liveRows.has(c.id)) {
          // The earlier row has not reported yet: never a second row for the card. It re-arms on its report.
          resumedWhileLive.set(c.id, c.status)
        } else {
          rearmCard(previous)
        }
      } else if (parkedBefore && FINAL.includes(c.status)) {
        const from = ['blocked', 'review'].includes(previous.status) ? previous.status : String(wasReport).toLowerCase()
        log(`✓ ${c.id} ${c.status} after ${from}`)
        // A live row will report the real final status itself; otherwise fix the earlier stop result.
        const last = lastResult(c.id)
        if (last && !liveRows.has(c.id)) {
          last.previous_verdict = last.verdict
          last.verdict = c.status.toUpperCase()
        }
      }
      Object.assign(previous, c)
    }
    for (const c of ev.new || []) {
      if (cards[c.id]) continue
      if (A.follow_new === false) { ignoredNew.add(c.id); continue }
      cards[c.id] = c
      log(`new card ${c.id} "${short(c.title)}" in ${assignPhase(c)}`)
    }
    launchAll()
    if (ev.all_finished) break
  }
  const unfinished = Object.values(cards).filter(c => !FINAL.includes(c.status))
  if (observedNow >= deadline && unfinished.length) {
    log(`MAX_HOURS reached; still watched, not all finished: ${unfinished.map(c => `${c.id}=${c.status}`).join(', ')}`)
  }
}

// Rows added while earlier ones ran push onto `running`, so wait until it stops growing.
let settled = 0
while (settled < running.length) {
  const batch = running.slice(settled)
  await Promise.all(batch)
  settled += batch.length
}

return { board: BOARD, cards: results, read_status: readErrors.length ? 'UNKNOWN' : 'OK',
  ...(readErrors.length ? { error: readErrors.join('; ') } : {}) }
