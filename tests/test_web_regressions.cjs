const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function between(start, end) {
  const offset = source.indexOf(start);
  const until = source.indexOf(end, offset);
  assert.ok(offset >= 0 && until > offset, `Missing source range: ${start}`);
  return source.slice(offset, until);
}
const readHelper = between('async function fetchJSONWithTimeout(', 'async function measureBridgeLatency(');
const plain = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const turn = id => ({id, status: 'completed', items: [
  {id: `${id}-user`, type: 'userMessage', text: id},
  {id: `${id}-answer`, type: 'agentMessage', phase: 'final_answer', text: `answer ${id}`},
]});

class Element {
  constructor(type = 'div') {
    this.type = type;
    this.children = [];
    this.textContent = '';
    this.replacements = 0;
  }
  get childElementCount() { return this.children.length; }
  append(...nodes) {
    for (const node of nodes) {
      if (node.type === 'fragment') this.children.push(...node.children.splice(0));
      else this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.replacements++;
    this.append(...nodes);
  }
}

function renderingHarness() {
  const history = new Element();
  const context = vm.createContext({
    document: {documentElement: {scrollHeight: 100},
      createDocumentFragment: () => new Element('fragment'), createElement: type => new Element(type)},
    window: {scrollY: 0, scrollTo() {}}, conversationNearBottom: () => false,
    renderedThreadSignatures: new Map(), renderedThreadId: '', threads: [],
    elements: {threadHistory: history, threadMeta: new Element()},
    localizedThreadStatus: () => '', formatThreadTime: () => '', persistedManagedTurnIds: new Set(),
    desktopDispatchState: undefined, desktopActivityEvidence: undefined,
    renderHistoryNotice() {}, renderProjectGroups() {}, requestAnimationFrame: fn => fn(),
    scrollToLatest() {}, followLatestOrNotify() {}, updateComposerState() {},
    appendWorkedGroup() {},
    appendHistoryItem(item, target) {
      const node = new Element('article'); node.textContent = item.text; target.append(node);
    },
  });
  vm.runInContext(between('function threadContentSignature(', 'function scrollMetrics(')
    + between('function prependHistoryPage(', 'async function loadOlderHistory(')
    + between('function appendTurnHistory(', 'function appendHistoryItem(')
    + between('function renderThreadDetail(', 'async function respondToManagedRequest('), context);
  return {context, history};
}

test('an older page renders its prepended turns even when the newest three are unchanged', () => {
  const {context, history} = renderingHarness();
  const current = {id: 't', historyPaged: true, historyNextCursor: 'older', turns: ['4', '5', '6'].map(turn)};
  context.renderThreadDetail(current, 3, {scroll: false});
  assert.equal(history.childElementCount, 6);
  const merged = context.prependHistoryPage(current, {historyNextCursor: null, turns: ['1', '2', '3'].map(turn)});
  context.renderThreadDetail(merged, 6, {scroll: false, preservePosition: true});
  assert.equal(history.childElementCount, 12);
  assert.deepEqual(history.children.map(node => node.textContent),
    ['1', 'answer 1', '2', 'answer 2', '3', 'answer 3', '4', 'answer 4', '5', 'answer 5', '6', 'answer 6']);
});

test('unchanged history skips DOM replacement but a changed source revision updates it', () => {
  const {context, history} = renderingHarness();
  const current = {id: 't', turns: [turn('1')]};
  context.renderThreadDetail(current, 1, {scroll: false});
  context.renderThreadDetail(plain(current), 1, {scroll: false});
  assert.equal(history.replacements, 1);
  const changed = {id: 't', turns: [{...turn('1'), itemsTruncated: true, omittedItemCount: 23,
    sourceItemCount: 225, sourceRevision: 'changed'}]};
  context.renderThreadDetail(changed, 1, {scroll: false});
  assert.equal(history.replacements, 2);
  assert.ok(history.children.some(node => /23 条记录未显示/.test(node.textContent)));
});

function manualTimers() {
  let id = 0;
  const callbacks = new Map();
  return {
    callbacks,
    setTimeout(callback) { callbacks.set(++id, callback); return id; },
    clearTimeout(timer) { callbacks.delete(timer); },
    expire() {
      const scheduled = [...callbacks.values()]; callbacks.clear();
      for (const callback of scheduled) callback();
    },
  };
}

test('read deadline includes a stalled response body and aborts its request', async () => {
  const timers = manualTimers(); let signal, releaseBody;
  const context = vm.createContext({AbortController, window: timers,
    fetch: async (_, options) => {
      signal = options.signal;
      return {status: 200, ok: true, json: () => new Promise(resolve => {releaseBody = resolve;})};
    }});
  vm.runInContext(readHelper, context);
  const pending = context.fetchJSONWithTimeout('/read');
  await tick();
  assert.equal(timers.callbacks.size, 1, 'headers must not clear the body deadline');
  const rejected = assert.rejects(pending, /read_timeout/);
  timers.expire();
  await rejected;
  assert.equal(signal.aborted, true);
  releaseBody({late: true});
  await tick();
  assert.equal(timers.callbacks.size, 0);
});

test('successful reads clear their deadline and preserve response metadata', async () => {
  const timers = manualTimers();
  const context = vm.createContext({AbortController, window: timers,
    fetch: async () => ({status: 200, ok: true, json: async () => ({ok: true})})});
  vm.runInContext(readHelper, context);
  const value = await context.fetchJSONWithTimeout('/read');
  assert.equal(value.response.status, 200);
  assert.deepEqual(plain(value.result), {ok: true});
  assert.equal(timers.callbacks.size, 0);
});

test('a hung status body times out, releases single-flight, and allows the next refresh', async () => {
  const timers = manualTimers(); let calls = 0;
  const context = vm.createContext({AbortController, window: timers, deviceToken: 'token',
    statusRefreshPromise: undefined, authorizationHeaders: () => ({}),
    renderManagedRun() {}, renderProjectGroups() {}, updateComposerState() {}, setDeviceState() {},
    hostLabel: () => 'Mac', currentStopCandidates: 1, desktopStatusKnown: true, desktopRequest: {},
    managedRenderSignature: 'old',
    fetch: async () => {
      calls++;
      return {status: 502, ok: false, json: calls === 1 ? () => new Promise(() => {})
        : async () => ({error: 'accessibility_probe_failed', reason: 'probe_failed'})};
    },
  });
  vm.runInContext(readHelper + between('function desktopStatusFailureText(',
    'async function refreshDesktopConversationIfRunning('), context);
  const first = context.refreshStatus();
  const duplicate = context.refreshStatus();
  await tick(); assert.equal(calls, 1);
  timers.expire(); await Promise.all([first, duplicate]);
  assert.equal(context.statusRefreshPromise, undefined);
  await context.refreshStatus();
  assert.equal(calls, 2);
  assert.equal(context.statusRefreshPromise, undefined);
});

function managedHarness() {
  const requests = [], rendered = [];
  const context = vm.createContext({AbortController, window: {setTimeout, clearTimeout},
    selectedThread: {id: 't'}, deviceToken: 'token', managedRun: undefined,
    managedRunRefreshes: new Map(), managedRunRequestVersions: new Map(),
    authorizationHeaders: () => ({}), completedRunsSeen: new Set(),
    persistedManagedTurnIds: new Set(), elements: {composerState: {}},
    managedRunIsActive: run => (run || context.managedRun)?.status === 'inProgress',
    renderManagedRun: () => rendered.push(context.managedRun?.agentText), updateComposerState() {},
    fetch: () => new Promise(resolve => requests.push(resolve)),
  });
  vm.runInContext(readHelper + between('async function refreshManagedRun(', 'function nativeMessageIdentity('), context);
  function reply(index, text) {
    requests[index]({status: 200, ok: true, json: async () => ({run: {
      threadId: 't', turnId: 'turn', status: 'inProgress', agentText: text,
    }})});
  }
  return {context, requests, rendered, reply};
}

test('managed polling coalesces concurrent reads and releases the slot for the next poll', async () => {
  const h = managedHarness();
  const first = h.context.refreshManagedRun(), duplicate = h.context.refreshManagedRun();
  assert.equal(h.requests.length, 1);
  h.reply(0, 'first'); await Promise.all([first, duplicate]);
  const next = h.context.refreshManagedRun();
  assert.equal(h.requests.length, 2);
  h.reply(1, 'newer'); await next;
  assert.deepEqual(h.rendered, ['first', 'newer']);
  assert.equal(h.context.managedRunRefreshes.size, 0);
});

test('a read started before a send or stop cannot overwrite its newer state', async () => {
  const h = managedHarness();
  const pending = h.context.refreshManagedRun();
  h.context.managedRun = {threadId: 't', turnId: 'new-turn', status: 'inProgress', agentText: 'new state'};
  h.reply(0, 'old state'); await pending;
  assert.equal(h.context.managedRun.agentText, 'new state');
  assert.deepEqual(h.rendered, []);
});

test('a managed response for a previously selected thread is ignored', async () => {
  const h = managedHarness();
  const pending = h.context.refreshManagedRun();
  h.context.selectedThread = {id: 'other'};
  h.reply(0, 'wrong task'); await pending;
  assert.equal(h.context.managedRun, undefined);
  assert.deepEqual(h.rendered, []);
});

test('BFCache hide/show restores exactly one set of polling timers', () => {
  const active = new Set(), handlers = new Map(); let next = 0, statusReads = 0;
  const context = vm.createContext({
    refreshTimer: undefined, managedPollTimer: undefined, desktopHistoryPollTimer: undefined,
    drawerStatusPollTimer: undefined, desktopHistoryRefreshInFlight: false,
    document: {visibilityState: 'visible'}, selectedThread: undefined, notificationsEnabled: false,
    elements: {projectDrawer: {classList: {contains: () => false}}},
    refreshStatus() {statusReads++;}, refreshDesktopConversationIfRunning() {},
    window: {setInterval() {active.add(++next); return next;}, clearInterval(id) {active.delete(id);},
      addEventListener(event, handler) {handlers.set(event, handler);}},
  });
  vm.runInContext(between('function startPolling()', '\nrenderNotificationState();'), context);
  for (const event of ['pageshow', 'pagehide', 'online']) {
    const line = source.split('\n').find(line => line.startsWith(`window.addEventListener("${event}"`));
    assert.ok(line, `missing ${event} handler`); vm.runInContext(line, context);
  }
  context.startPolling(); context.startPolling(); assert.equal(active.size, 4);
  handlers.get('pagehide')({persisted: true}); assert.equal(active.size, 0);
  handlers.get('pageshow')({persisted: true}); assert.equal(active.size, 4);
  handlers.get('pageshow')({persisted: true}); assert.equal(active.size, 4);
  assert.equal(statusReads, 2);
});

function cacheHarness() {
  let encodedCharacters = 0, stringifyCalls = 0, fullHistorySerializations = 0;
  const writes = [], removals = [];
  const context = vm.createContext({
    THREAD_SESSION_CACHE_MAX_CHARS: 1500000, THREAD_SESSION_CACHE_KEY: 'history',
    persistedThreadSessionPayload: '',
    sessionStorage: {setItem: (_, value) => writes.push(value), removeItem: key => removals.push(key)},
    JSON: {stringify(value) {
      stringifyCalls++;
      if (value?.thread?.turns?.length) fullHistorySerializations++;
      const text = JSON.stringify(value); encodedCharacters += text.length; return text;
    }},
  });
  vm.runInContext(between('function persistSessionThreadDetail(', 'function rememberThreadDetail('), context);
  return {context, writes, removals, encodedCharacters: () => encodedCharacters,
    stringifyCalls: () => stringifyCalls, fullHistorySerializations: () => fullHistorySerializations};
}

test('large paged histories are capped with linear serialization work and honest cursors', () => {
  const h = cacheHarness();
  const turns = Array.from({length: 300}, (_, index) => ({id: String(index), items: [{text: 'x'.repeat(10000)}]}));
  const entry = {threadId: 't', fetchedAt: 100, turnLimit: 300, complete: true,
    thread: {id: 't', historyCursor: {turnId: '299', revision: 'r'}, historyNextCursor: 'older', turns}};
  h.context.persistSessionThreadDetail(entry);
  assert.equal(h.writes.length, 1);
  assert.ok(h.writes[0].length <= 1500000);
  assert.ok(h.encodedCharacters() < 5000000, 'must not repeatedly serialize the shrinking full history');
  assert.ok(h.stringifyCalls() <= turns.length + 5);
  assert.equal(h.fullHistorySerializations(), 1, 'only the selected tail needs a full snapshot serialization');
  const saved = JSON.parse(h.writes[0]);
  assert.equal(saved.complete, false);
  assert.equal(saved.thread.historyCursor, undefined);
  assert.equal(saved.thread.historyNextCursor, undefined);
  assert.equal(saved.thread.turns.at(-1).id, '299');
  assert.equal(entry.thread.turns.length, 300, 'persistent cache trimming must not trim the visible history');
});

test('identical history does not rewrite Web Storage just because fetchedAt advanced', () => {
  const h = cacheHarness();
  const entry = {threadId: 't', fetchedAt: 100, turnLimit: 1, thread: {id: 't', turns: [turn('1')]}};
  h.context.persistSessionThreadDetail(entry);
  h.context.persistSessionThreadDetail({...plain(entry), fetchedAt: 200});
  assert.equal(h.writes.length, 1);
  assert.equal(JSON.parse(h.writes[0]).fetchedAt, 100);
  const changed = plain(entry); changed.thread.turns[0].items[1].text = 'changed';
  h.context.persistSessionThreadDetail(changed);
  assert.equal(h.writes.length, 2);
});

function macDeliveryHarness(reply) {
  const storage = new Map(), calls = [], timers = [], opened = [];
  const context = vm.createContext({
    crypto: require('node:crypto').webcrypto, TextEncoder, AbortController,
    bridgeCapabilities: {platform: 'macos', deliveryReceipts: true, nativeTextSend: false, newTasks: true, desktopControl: true},
    selectedThread: {id: 't', title: 'Task'}, selectedThreadLastTurnId: 'old-turn',
    selectedThreadLastTurnStatus: 'completed', selectedThreadHasFinalAnswer: true,
    selectedThreadRuntimeStatus: 'idle', desktopDispatchState: undefined,
    managedRun: undefined, isSendingMessage: false, isUploadingAttachments: false,
    isCreatingTask: false, newTaskTarget: {projectId: null, name: 'Recents'}, newTaskAttachments: [],
    modelSettingsCache: new Map(), threads: [], managedRenderSignature: '',
    nativeDeliveryNotices: new Map(), threadDrafts: new Map([['t', 'message']]),
    threadAttachments: new Map(), threadHistoryCache: new Map(),
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    localStorage: {setItem() {}}, SELECTED_THREAD_KEY: 'selected',
    elements: {composerInput: {value: 'message'}, composerState: {},
      managedLiveHistory: {replaceChildren() {}}, newTaskMessage: {value: 'message'},
      newTaskError: {}, newTaskDialog: {close() {}}},
    window: {setTimeout(fn) {timers.push(fn); return timers.length;}, clearTimeout() {}},
    selectedAttachments: () => [], managedRunIsActive: () => false,
    authorizationHeaders: () => ({}), handleUnauthorized() {}, updateComposerState() {},
    updateNewTaskControls() {}, renderProjectGroups() {}, renderNewTaskAttachments() {},
    resizeComposer() {}, renderComposerAttachments() {}, setDeviceState() {}, hostLabel: () => 'Mac',
    renderManagedRun() {}, refreshStatus() {}, loadThreads: async () => {},
    openThread: async (id, options) => opened.push({id, options}),
    nativeDeliveryResponse: async (url, options) => {
      const body = JSON.parse(options.body); calls.push({url, body});
      return reply(calls.length, body, url);
    },
  });
  vm.runInContext(between('function nativeMessageIdentity(', 'async function nativeDeliveryResponse(')
    + between('async function startManagedTurn(', 'function sendManagedMessage(')
    + between('async function createNewTask()', 'function renderProjectGroups()'), context);
  return {context, storage, calls, timers, opened};
}

function macReceipt(body, extra = {}) {
  return {response: {status: 202, ok: true}, result: {
    ok: true, mode: 'desktop', desktop: {ok: true, taskTitle: 'Task'},
    delivery: {requestId: body.requestId, replayed: false, receiptOnly: false}, ...extra,
  }};
}

test('Mac retry preserves its request ID and old receipt never silently sends the same text again', async () => {
  const h = macDeliveryHarness((attempt, body) => {
    if (attempt === 1) throw Error('response lost');
    return macReceipt(body, attempt === 2 ? {delivery: {requestId: body.requestId, replayed: true, receiptOnly: false}} : {});
  });
  await h.context.startManagedTurn();
  assert.equal(h.context.isSendingMessage, false);
  assert.equal(h.context.elements.composerInput.value, 'message');
  assert.match(h.context.elements.composerState.textContent, /结果待确认/);
  assert.ok(h.context.elements.composerState.textContent.includes(h.calls[0].body.requestId));
  assert.match(h.context.elements.composerState.textContent, /范围：t/);
  assert.ok(h.storage.has('pocket-native-delivery:t'));
  await h.context.startManagedTurn();
  assert.equal(h.calls[0].body.requestId, h.calls[1].body.requestId);
  assert.equal(h.context.elements.composerInput.value, 'message');
  assert.equal(h.context.selectedThreadLastTurnStatus, 'completed');
  assert.equal(h.storage.has('pocket-native-delivery:t'), false);
  await h.context.startManagedTurn();
  assert.notEqual(h.calls[2].body.requestId, h.calls[1].body.requestId);
  assert.equal(h.context.elements.composerInput.value, '');
});

test('Mac automatic receipt confirmation clears only its draft without inventing a new active turn', async () => {
  const h = macDeliveryHarness((_, body) => macReceipt(body,
    {delivery: {requestId: body.requestId, replayed: true, receiptOnly: true}}));
  await h.context.startManagedTurn();
  assert.equal(h.context.elements.composerInput.value, '');
  assert.equal(h.context.selectedThreadLastTurnStatus, 'completed');
  assert.equal(h.context.desktopDispatchState, undefined);
  assert.equal(h.storage.size, 0);
  for (const timer of h.timers) timer();
  assert.deepEqual(h.opened.map(item => item.id), ['t']);
});

test('Mac success without a matching receipt cannot discard the draft or request ID', async () => {
  const h = macDeliveryHarness((_, body) => macReceipt(body, {delivery: {requestId: 'unrelated'}}));
  await h.context.startManagedTurn();
  assert.equal(h.context.elements.composerInput.value, 'message');
  assert.ok(h.storage.has('pocket-native-delivery:t'));
  assert.match(h.context.elements.composerState.textContent, /待确认/);
});

test('Mac creation uses the same ID after a lost response and reconciles the existing task', async () => {
  const h = macDeliveryHarness((attempt, body) => {
    if (attempt === 1) throw Error('response lost');
    return macReceipt(body, {thread: {id: 'created', title: 'Task'},
      delivery: {requestId: body.requestId, replayed: true, receiptOnly: true}});
  });
  await h.context.createNewTask();
  assert.equal(h.context.elements.newTaskMessage.value, 'message');
  assert.ok(h.storage.has('pocket-native-delivery:create'));
  assert.ok(h.context.elements.newTaskError.textContent.includes(h.calls[0].body.requestId));
  assert.match(h.context.elements.newTaskError.textContent, /范围：create/);
  await h.context.createNewTask();
  assert.equal(h.calls[0].body.requestId, h.calls[1].body.requestId);
  assert.equal(h.context.selectedThreadLastTurnStatus, 'completed');
  assert.equal(h.context.desktopDispatchState, undefined);
  assert.deepEqual(h.opened.map(item => item.id), ['created']);
  assert.equal(h.storage.size, 0);
});

test('Mac partial creation transfers its original request ID into the known task receipt check', async () => {
  const h = macDeliveryHarness(() => ({response: {status: 409, ok: false}, result: {
    ok: false, error: 'delivery_uncertain', retryAllowed: false,
    threadCreated: true, threadId: 'created', thread: {id: 'created'},
  }}));
  await h.context.createNewTask();
  assert.equal(h.context.threadDrafts.get('created'), 'message');
  assert.equal(await h.context.nativeDeliveryRequestId('created', 'message'), h.calls[0].body.requestId);
  assert.equal(h.storage.has('pocket-native-delivery:create'), false);
  assert.match(h.context.elements.composerState.textContent, /待确认/);
});

for (const outcome of ['confirmed', 'refused']) {
  test(`edited Mac draft only checks its original receipt and can proceed after local ${outcome} resolution`, async () => {
    const h = macDeliveryHarness((attempt, body) => {
      if (attempt < 3) return {response: {status: 409, ok: false}, result: {
        ok: false, error: 'delivery_uncertain', retryAllowed: false,
        delivery: {requestId: body.requestId, replayed: attempt > 1, receiptOnly: !!body.receiptOnly},
      }};
      if (attempt === 3 && outcome === 'refused') return {response: {status: 409, ok: false}, result: {
        ok: false, error: 'delivery_manually_refused', retryAllowed: true,
        delivery: {requestId: body.requestId, replayed: true, receiptOnly: true},
      }};
      return macReceipt(body, attempt === 3
        ? {delivery: {requestId: body.requestId, replayed: true, receiptOnly: true}} : {});
    });
    await h.context.startManagedTurn();
    const original = h.calls[0].body;
    h.context.elements.composerInput.value = 'edited draft';
    await h.context.startManagedTurn();
    assert.deepEqual(h.calls[1].body, {...original, receiptOnly: true});
    assert.equal(JSON.parse(h.storage.get('pocket-mac-pending:t')).body.requestId, original.requestId);
    assert.equal(h.context.elements.composerInput.value, 'edited draft');
    assert.match(h.context.elements.composerState.textContent, /只核对原回执/);
    assert.ok(h.context.elements.composerState.textContent.includes(original.requestId));
    assert.ok(!h.context.elements.composerState.textContent.includes('edited draft'));
    await h.context.startManagedTurn();
    assert.deepEqual(h.calls[2].body, {...original, receiptOnly: true});
    assert.equal(h.context.elements.composerInput.value, 'edited draft');
    assert.equal(h.storage.size, 0);
    assert.match(h.context.elements.composerState.textContent, /再次点击/);
    assert.doesNotMatch(h.context.elements.composerState.textContent, /请求编号/);
    await h.context.startManagedTurn();
    assert.equal(h.calls[3].body.message, 'edited draft');
    assert.notEqual(h.calls[3].body.requestId, original.requestId);
    assert.equal(h.calls[3].body.receiptOnly, undefined);
    assert.equal(h.context.elements.composerInput.value, '');
  });
}

test('an edited Mac creation draft survives receipt resolution without allocating another task', async () => {
  const h = macDeliveryHarness((attempt, body) => {
    if (attempt === 1) throw Error('response lost');
    if (attempt === 2) return {response: {status: 409, ok: false}, result: {
      ok: false, error: 'delivery_manually_refused', retryAllowed: true,
      delivery: {requestId: body.requestId, replayed: true, receiptOnly: true},
    }};
    return macReceipt(body, {thread: {id: 'new-task', title: 'New'}});
  });
  await h.context.createNewTask();
  const original = h.calls[0].body;
  h.context.elements.newTaskMessage.value = 'new creation draft';
  await h.context.createNewTask();
  assert.deepEqual(h.calls[1].body, {...original, receiptOnly: true});
  assert.equal(h.context.elements.newTaskMessage.value, 'new creation draft');
  assert.deepEqual(h.opened, []);
  assert.equal(h.storage.size, 0);
  await h.context.createNewTask();
  assert.equal(h.calls[2].body.message, 'new creation draft');
  assert.notEqual(h.calls[2].body.requestId, original.requestId);
  assert.deepEqual(h.opened.map(item => item.id), ['new-task']);
});

test('Mac original receipt context survives a page reload while the current draft changes', async () => {
  const before = macDeliveryHarness(() => {throw Error('response lost');});
  await before.context.startManagedTurn();
  const after = macDeliveryHarness((_, body) => ({response: {status: 409, ok: false}, result: {
    ok: false, error: 'delivery_receipt_pending', retryAllowed: false,
    delivery: {requestId: body.requestId, replayed: true, receiptOnly: true},
  }}));
  for (const [key, value] of before.storage) after.storage.set(key, value);
  after.context.elements.composerInput.value = 'changed after reload';
  await after.context.startManagedTurn();
  assert.deepEqual(after.calls[0].body, {...before.calls[0].body, receiptOnly: true});
  assert.equal(after.context.elements.composerInput.value, 'changed after reload');
  assert.ok(after.storage.has('pocket-mac-pending:t'));
});

for (const endpoint of ['/api/codex/threads', '/api/codex/threads/t/turn']) {
  test(`Mac lost response only triggers receipt queries for ${endpoint}`, async () => {
    const calls = [];
    const context = vm.createContext({AbortController,
      bridgeCapabilities: {deliveryReceipts: true, nativeTextSend: false},
      window: {setTimeout(fn, milliseconds) {
        if (milliseconds <= 5000) {queueMicrotask(fn); return null;}
        return setTimeout(fn, milliseconds);
      }, clearTimeout},
      fetch: async (url, options) => {
        const body = JSON.parse(options.body); calls.push({url, body});
        if (calls.length === 1) throw Error('response lost');
        return {status: 202, ok: true, json: async () => ({ok: true,
          delivery: {requestId: body.requestId, replayed: true, receiptOnly: true}})};
      },
    });
    vm.runInContext(between('async function nativeDeliveryResponse(', 'async function startManagedTurn('), context);
    const body = {requestId: 'stable', message: 'message', attachmentIds: ['image']};
    const result = await context.nativeDeliveryResponse(endpoint, {method: 'POST', body: JSON.stringify(body)});
    assert.equal(result.result.ok, true);
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[0].body, body);
    assert.deepEqual(calls[1].body, {...body, receiptOnly: true});
  });
}
