// Deterministic external-agent replies exercise the real workflow state machine.
import {run} from './workflow.mjs';
const scenario = process.argv[2];
const agentType = JSON.parse(process.argv[3]);
const make = (id, status, parents = []) => ({id, status, parents, title:id, runs:1, retried:false});
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
