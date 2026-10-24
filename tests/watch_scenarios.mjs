// Deterministic external-agent replies exercise the real workflow state machine.
import assert from 'node:assert/strict';
import {mkdtempSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';

if (!process.argv[2]) {
  // Standalone fixture gate. All generated modules live in this temporary directory.
  const directory = mkdtempSync(join(tmpdir(), 'watch-scenarios-'));
  try {
    const source = readFileSync(new URL('../plugin/workflows/kanban-watch.js', import.meta.url), 'utf8');
    const offset = source.indexOf('const A = args || {}');
    assert(offset > 0);
    writeFileSync(join(directory, 'workflow.mjs'), source.slice(0, offset) +
      'export async function run(args, agent, log) {\n' + source.slice(offset) + '\n}\n');
    writeFileSync(join(directory, 'watch.mjs'), readFileSync(new URL(import.meta.url)));
    const wedgedScenarios = ['progress-flat', 'progress-heartbeat', 'progress-anthropic-quiet'];
    const scenarios = ['progress-growing', 'progress-flat', 'progress-heartbeat', 'progress-anthropic',
      'progress-anthropic-quiet', 'waiting', 'resume', 'new', 'ignore-new'];
    for (const scenario of scenarios) {
      const child = spawnSync(process.execPath, [join(directory, 'watch.mjs'), scenario, 'null'],
        {encoding:'utf8', timeout:10000});
      assert.equal(child.status, 0, child.stderr);
      const data = JSON.parse(child.stdout);
      assert.equal(data.result.read_status, 'OK');
      if (scenario.startsWith('progress-')) {
        assert.equal(data.result.cards[0].odd, wedgedScenarios.includes(scenario) ? 'possibly wedged' : '', scenario);
      }
    }
    console.log(`${scenarios.length} scenarios passed`);
  } finally { rmSync(directory, {recursive:true, force:true}); }
} else {
const {run} = await import('./workflow.mjs');
const scenario = process.argv[2];
const agentType = JSON.parse(process.argv[3]);
const make = (id, status, parents = []) => ({id, status, parents, title:id, runs:1, retried:false});
if (scenario.startsWith('progress-')) {
  // A deterministic relay: it reads the wedged rule out of the prompt and applies it
  // to fixed follow transcripts, so a prompt with the wrong rule gives the wrong odd.
  // It checks the prompt's decision, not a live model's compliance with it.
  const unavailable = 'progress baseline\nworker messages unavailable; using card log\nstill running';
  const transcripts = {
    'progress-growing': [unavailable, 'progress advanced\nstill running', 'progress advanced\nstill running'],
    'progress-flat': [unavailable, 'progress flat\nstill running', 'progress flat\nstill running'],
    // Codex card whose heartbeat advances while its log stays flat: follow reports flat.
    'progress-heartbeat': [unavailable, 'progress flat\nstill running', 'progress flat\nstill running'],
    'progress-anthropic': ['progress baseline\nBuilding\nstill running', 'progress flat\nNext step\nstill running',
      'progress flat\nMore work\nstill running'],
    // Anthropic card that is message-quiet (heartbeat may advance): origin/main flags it.
    'progress-anthropic-quiet': ['progress baseline\nBuilding\nstill running',
      'progress flat\nno new worker messages\nstill running', 'progress flat\nno new worker messages\nstill running'],
  }[scenario];
  assert(transcripts, `unknown scenario ${scenario}`);
  const calls = [];
  const result = await run({tasks:['t_one']}, async (prompt, options) => {
    calls.push({prompt,options});
    const ok = data => ({read_status:'OK',now:100000,...data});
    if (options.schema.properties.count) return ok({cards:[make('t_one','running')],count:1,stale_skipped:[]});
    if (options.schema.properties.verdict) {
      assert(prompt.includes('A heartbeat is never progress'));
      assert(prompt.includes('--max-seconds 270'));
      const rules = prompt.split(/(?<=\.)\s+/).filter(s => s.includes('say "possibly wedged" in odd')).map(s => ({
        unavailable: s.startsWith('If follow reported "worker messages unavailable"'),
        available: s.startsWith('If worker messages are available'),
        marker: (s.match(/two follow calls in a row.*each (?:printed|report) "([^"]+)"/) || [])[1],
      }));
      assert.equal(rules.length, 2, 'prompt must state one rule per message availability');
      const noMessages = transcripts.some(t => t.includes('worker messages unavailable'));
      const rule = rules.find(r => noMessages ? r.unavailable : r.available);
      assert(rule && rule.marker, 'no applicable wedged rule in prompt');
      const wedged = transcripts.slice(-2).every(t => t.split('\n').includes(rule.marker) ||
        t.split('\n').some(line => line.startsWith(rule.marker)));
      return ok({id:'t_one',verdict:'STILL_RUNNING',model_that_ran:'fixture-model',summary:'Still running',
        retried:false,warnings:{},odd:wedged ? 'possibly wedged' : ''});
    }
    if (options.schema.properties.reason) return ok({reason:'Still running'});
    if (options.schema.properties.count_new) return ok({changed:[],new:[],count_changed:0,count_new:0,all_finished:true});
    throw Error('Unexpected progress request');
  }, () => {});
  console.log(JSON.stringify({calls,result}));
} else if (scenario.startsWith('round-child-') || scenario.startsWith('round-parked-')) {
  const queued = scenario.startsWith('round-child-');
  const status = scenario.split('-')[2];
  const deadline = scenario.endsWith('-deadline');
  const calls = [], logs = [];
  let now = 100000, eventCalls = 0, snapshotCalls = 0;
  let finishParent, finishChild, childStarted, flagSeen;
  const parentDone = new Promise(resolve => {finishParent = resolve});
  const childDone = new Promise(resolve => {finishChild = resolve});
  const childLive = new Promise(resolve => {childStarted = resolve});
  const flagged = new Promise(resolve => {flagSeen = resolve});
  const result = await run({
    tasks:queued ? ['t_one','t_two'] : ['t_one'],
    ...(deadline ? {max_wait_seconds:7100} : {}),
  }, async (prompt, options) => {
    calls.push({prompt, options});
    const ok = data => ({read_status:'OK', now, ...data});
    const props = options.schema.properties;
    if (props.count) {
      const cards = queued ? [make('t_one','running'),make('t_two',status,['t_one'])] : [make('t_one',status)];
      return ok({cards,count:cards.length,stale_skipped:[]});
    }
    if (props.verdict) {
      const id = prompt.match(/Kanban card (t_\w+)/)[1];
      let verdict;
      if (queued && id === 't_one') verdict = await parentDone;
      else if (!queued && !calls.some(c => c.options.schema.properties.reason)) verdict = status.toUpperCase();
      else { childStarted(); verdict = await childDone; }
      return ok({id,verdict,model_that_ran:'fixture-model',summary:'Observed summary',retried:false,warnings:{}});
    }
    if (props.reason) { flagSeen(); return ok({reason:'Waiting for input'}); }
    if (props.exit) return ok({exit:3,started:[],cards:[]});
    if (props.count_new) {
      if (!queued) await flagged;
      eventCalls++;
      const retries = Number(prompt.match(/up to (\d+) times/)[1]);
      const wake = Number(prompt.match(/--max-seconds (\d+)/)[1]);
      now += retries * wake;
      if (deadline || eventCalls < 3) return ok({changed:[],new:[],count_changed:0,count_new:0,timeout:true});
      if (eventCalls === 3) {
        if (queued) { finishParent('DONE'); await childLive; }
        return ok({changed:queued ? [make('t_one','done'),make('t_two','running',['t_one'])] : [make('t_one','running')],
          new:[],count_changed:queued ? 2 : 1,count_new:0});
      }
      await childLive;
      finishChild('DONE');
      return ok({changed:[make(queued ? 't_two' : 't_one','done')],new:[],count_changed:1,count_new:0,all_finished:true});
    }
    if (props.cards) {
      snapshotCalls++;
      const cards = queued ? [
        {id:'t_one',status:'running',current_run_id:1,
          run:{id:1,status:'running',started_at:99900,ended_at:null},last_heartbeat_at:now - 1},
        {id:'t_two',status},
      ] : [{id:'t_one',status}];
      return ok({cards});
    }
    throw Error('Unexpected queued/parked round request');
  }, message => {
    logs.push(message);
    // Settle live rows if the workflow rejects a snapshot, so a regression
    // produces an assertion failure rather than an unresolved fixture promise.
    if (message.startsWith('UNKNOWN:')) { finishParent('STILL_RUNNING'); finishChild('STILL_RUNNING'); }
  });
  console.log(JSON.stringify({calls,logs,eventCalls,snapshotCalls,result}));
} else if (scenario.startsWith('round-')) {
  const calls = [], logs = [];
  const longScenario = ['round-long', 'round-mixed-long', 'round-at-deadline'].includes(scenario);
  let now = 100000, eventCalls = 0, snapshotCalls = 0, finish;
  const finished = new Promise(resolve => {finish = resolve});
  const result = await run({
    ...(agentType === null ? {} : {agent_type:agentType}),
    tasks:['t_one'], ...(scenario === 'round-deadline' ? {max_wait_seconds:7100} : {}),
    ...(scenario === 'round-at-deadline' ? {max_wait_seconds:10530} : {}),
  }, async (prompt, options) => {
    calls.push({prompt, options});
    const ok = data => ({read_status:'OK', now, ...data});
    if (options.schema.properties.count) {
      const cards = [make('t_one','running')];
      if (scenario === 'round-mixed-long' || scenario === 'round-second-stale') cards.push(make('t_two','running'));
      return ok({cards, count:cards.length, stale_skipped:[]});
    }
    if (options.schema.properties.verdict) {
      const verdict = await finished;
      const id = prompt.match(/Kanban card (t_\w+)/)[1];
      return ok({id,verdict,model_that_ran:'fixture-model',summary:'Observed summary',retried:false,warnings:{}});
    }
    if (options.schema.properties.reason) return ok({reason:'Still running'});
    if (options.schema.properties.count_new) {
      eventCalls++;
      // Replay the observed old-agent failure at its documented context cap. A
      // bounded-round protocol must explicitly hand ordinary exhaustion back as
      // the last exit-3 JSON, rather than ask this agent to decide when to give up.
      if (!prompt.includes('If every call exits 3')) {
        finish('STILL_RUNNING');
        return {read_status:'UNKNOWN',error:'Watch exhausted its 13 documented exit-3 retries with no exit 0, 2 or 5'};
      }
      const retries = Number(prompt.match(/up to (\d+) times/)[1]);
      const wake = Number(prompt.match(/--max-seconds (\d+)/)[1]);
      now += retries * wake;
      if (longScenario && eventCalls === 3) {
        finish('DONE');
        return ok({changed:[],new:[],count_changed:0,count_new:0,all_finished:true});
      }
      if ((!longScenario && !['round-deadline', 'round-default-deadline', 'round-finished'].includes(scenario)) ||
          now >= (scenario === 'round-default-deadline' ? 114400 : 107100)) finish('STILL_RUNNING');
      return ok({changed:[],new:[],count_changed:0,count_new:0,timeout:true});
    }
    if (options.schema.properties.cards) {
      snapshotCalls++;
      if (scenario === 'round-read-error') return {read_status:'UNKNOWN',error:'snapshot permission denied'};
      const snapshot = {
        id:'t_one',status:scenario === 'round-unknown' ? 'unrecognised' : scenario === 'round-not-found' ? 'NOT_FOUND' : 'running',
        current_run_id:1,run:{id:1,status:'running',started_at:99900,ended_at:null},
        last_heartbeat_at:now - (scenario === 'round-stale' ? 3601 : 1),
      };
      if (scenario === 'round-wrong-run') snapshot.run.id = 2;
      if (scenario === 'round-ended-run') snapshot.run.ended_at = now - 1;
      if (scenario === 'round-no-heartbeat') snapshot.last_heartbeat_at = null;
      if (scenario === 'round-prior-heartbeat') snapshot.last_heartbeat_at = 99899;
      if (scenario === 'round-future-heartbeat') snapshot.last_heartbeat_at = now + 1;
      if (scenario === 'round-missing') return ok({cards:[]});
      if (scenario === 'round-duplicate') return ok({cards:[snapshot,snapshot]});
      if (scenario === 'round-malformed') return ok({cards:{}});
      if (scenario === 'round-old-snapshot') return ok({cards:[snapshot],now:now - 1});
      if (scenario === 'round-mixed-long' || scenario === 'round-second-stale') {
        return ok({cards:[{id:'t_one',status:'done'}, {...snapshot,id:'t_two',
          last_heartbeat_at:now - (scenario === 'round-second-stale' ? 3601 : 1)}]
          .filter(c => prompt.includes(`--task '${c.id}'`))});
      }
      if (scenario === 'round-finished') { snapshot.status = 'done'; finish('DONE'); }
      return ok({cards:[snapshot]});
    }
    throw Error('Unexpected round request');
  }, s => logs.push(s));
  console.log(JSON.stringify({calls,logs,eventCalls,snapshotCalls,result}));
} else {
const one = make('t_one', scenario === 'resume' ? 'blocked' : scenario === 'waiting' ? 'ready' : 'running');
const two = make('t_two', scenario === 'waiting' ? 'ready' : 'done');
const rows = [], prompts = [], logs = [];
let reports = 0, eventCalls = 0, flagResolve, secondResolve;
const flagSeen = new Promise(r => {flagResolve = r});
const secondSeen = new Promise(r => {secondResolve = r});
const envelope = data => ({read_status:'OK',now:101,...data});
const result = await run({
  ...(agentType === null ? {} : {agent_type:agentType}),
  follow_new: scenario !== 'ignore-new', max_hours: 1,
  phases: [{title:'Build',tasks:['t_one','t_two']}],
}, async (prompt, options) => {
  if (options.agentType !== (agentType ?? 'hermes-kanban-watch:kanban-watcher')) {
    throw Error(`Unexpected agentType: ${options.agentType}`);
  }
  prompts.push(prompt);
  if (options.schema.properties.count) {
    return envelope({cards:scenario === 'waiting' ? [one,two] : [one], count:scenario === 'waiting' ? 2 : 1, stale_skipped:[]});
  }
  if (options.schema.properties.exit) {
    rows.push({kind:'wait', phase:options.phase});
    return envelope({exit:0,started:['t_one','t_two'],cards:[make('t_one','done'),make('t_two','done')]});
  }
  if (options.schema.properties.verdict) {
    reports++;
    const id = prompt.match(/Kanban card (t_\w+)/)[1];
    rows.push({kind:'live',id,phase:options.phase});
    if (reports === 2) secondResolve();
    return envelope({id,verdict:scenario === 'resume' && reports === 1 ? 'BLOCKED' : 'DONE',
      model_that_ran:'fixture-model',summary:'Observed summary',retried:false,warnings:{}});
  }
  if (options.schema.properties.reason) {
    rows.push({kind:'flag',phase:options.phase});
    flagResolve();
    return envelope({reason:'Waiting for input'});
  }
  if (options.schema.properties.count_new) {
    eventCalls++;
    if (scenario === 'resume' && eventCalls === 1) {
      await flagSeen;
      return envelope({changed:[make('t_one','running')],new:[],count_changed:1,count_new:0});
    }
    if (scenario === 'resume') {
      await secondSeen;
      return envelope({changed:[make('t_one','done')],new:[],count_changed:1,count_new:0,all_finished:true});
    }
    if (scenario === 'new' || scenario === 'ignore-new') {
      return envelope({changed:[make('t_one','done')],new:[two],count_changed:1,count_new:1,all_finished:true});
    }
    return envelope({changed:[make('t_one','done'),make('t_two','done')],new:[],count_changed:2,count_new:0,all_finished:true});
  }
  throw Error('Unexpected external request');
}, s => logs.push(s));
console.log(JSON.stringify({rows,prompts,logs,result}));
}
}
