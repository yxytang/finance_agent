// 不依赖浏览器/DOM 库，执行真实页面脚本，检查事件与请求之间的不变量。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/boot\(\);\s*$/, '');

class Element {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.className = '';
    this.style = {};
    this.attributes = {};
    this.value = '';
    this.scrollHeight = 42;
    this.scrollTop = 0;
    this.clientHeight = 400;
    this.dataset = {};
    this.listeners = {};
    this.classList = {
      contains: (name) => this.className.split(' ').includes(name),
      add: (name) => { if (!this.classList.contains(name)) this.className += ' ' + name; },
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.contains(name) : force;
        this.className = this.className.split(' ').filter((c) => c !== name).join(' ');
        if (enabled) this.classList.add(name);
        return enabled;
      },
    };
  }
  set innerHTML(value) { this.content = value; this.children = []; }
  get innerHTML() { return this.content || ''; }
  appendChild(node) { node.parentElement = this; this.children.push(node); return node; }
  remove() { this.parentElement.children = this.parentElement.children.filter((n) => n !== this); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  focus() {}
  close() { this.open = false; }
  showModal() { this.open = true; }
  querySelectorAll(selector) {
    const matches = (node) => selector.startsWith('.') ? node.classList.contains(selector.slice(1)) : node.tag === selector;
    return this.children.flatMap((node) => [...(matches(node) ? [node] : []), ...node.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function setup() {
  const nodes = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map((m) => [m[1], new Element()]));
  nodes.main.appendChild(nodes.log);
  const body = new Element();
  const pane = new Element();
  const requests = [];
  const context = vm.createContext({
    document: {
      body, createElement: (tag) => new Element(tag),
      getElementById: (id) => nodes[id],
      querySelectorAll: () => [], querySelector: () => pane,
      addEventListener: () => {},
    },
    location: { href: 'http://localhost:8000/?token=test-token', origin: 'http://localhost:8000', hash: '' },
    history: { replaceState: () => {} },
    localStorage: { getItem: () => null, setItem: () => {} },
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    navigator: { clipboard: { writeText: async () => {} } },
    window: { confirm: () => true },
    console, URL, setTimeout: () => 1, clearTimeout: () => {},
    EventSource: class { close() {} },
    fetch: async (url, options) => {
      requests.push({ url: String(url), options });
      return { ok: true, json: async () => ({ sessions: [], accepted: true }) };
    },
  });
  vm.runInContext(source, context);
  const run = (code) => vm.runInContext(code, context);
  run('sessionId = "test-session"');
  return { nodes, requests, context, run };
}

test('knowledge upload sends original file with auth and prevents double submission', async () => {
  const { nodes, context, run, requests } = setup();
  const file = { name: '预算 资料.pdf', size: 120 };
  nodes['knowledge-files'].files = [file];
  let release;
  context.fetch = (url, options = {}) => {
    requests.push({ url: String(url), options });
    if (options.method === 'POST') return new Promise(resolve => { release = resolve; });
    if (String(url).includes('/api/jobs/')) return Promise.resolve({ ok: true, json: async () => ({ status: 'succeeded', result: { methods: ['ocr'] } }) });
    return Promise.resolve({ ok: true, json: async () => ({ files: [], indexed: true }) });
  };
  const pending = run('uploadKnowledge()');
  await run('uploadKnowledge()');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.body, file);
  assert.match(requests[0].url, /token=test-token/);
  assert.match(requests[0].url, /%E9%A2%84/);
  assert.equal(nodes['knowledge-upload'].disabled, true);
  release({ ok: true, json: async () => ({ id: 'job-1', status: 'queued' }) });
  await pending;
  assert.match(nodes['knowledge-progress'].textContent, /已入库/);
  assert.match(nodes['knowledge-progress'].textContent, /核对数字/);
  assert.equal(nodes['knowledge-upload'].disabled, false);
});

test('knowledge batch retains parse errors and continues valid files', async () => {
  const { nodes, context, run } = setup();
  nodes['knowledge-files'].files = [{ name: 'bad.pdf', size: 5 }, { name: 'good.pdf', size: 5 }];
  context.fetch = async (url, options = {}) => {
    if (String(url).includes('/api/jobs/')) return { ok: true, json: async () => ({ status: 'succeeded', result: { methods: ['native'] } }) };
    if (options.method !== 'POST') return { ok: true, json: async () => ({ files: [] }) };
    if (String(url).includes('bad.pdf')) return { ok: false, json: async () => ({ detail: '文件损坏' }) };
    return { ok: true, json: async () => ({ id: 'job-2', status: 'queued' }) };
  };
  await run('uploadKnowledge()');
  assert.match(nodes['knowledge-progress'].textContent, /bad.pdf：文件损坏/);
  assert.match(nodes['knowledge-progress'].textContent, /good.pdf 已入库/);
});

test('final answer keeps tool evidence and chart DOM attached', () => {
  const { run, nodes } = setup();
  run('render({type: "turn_start", text: "看看消费"})');
  run('render({type: "tool_call", call_id: "q1", name: "query_transactions", args: {}})');
  run('render({type: "tool_result", call_id: "q1", ok: true, content: "合计 300"})');
  // 没有 CDN 时依然保留图表降级提示和原始查询结果。
  run('render({type: "chart", call_id: "q1", kind: "observed", group: "类目", rows: [{label: "餐饮", value: 300}]})');
  const card = nodes.log.querySelector('.tool');
  const chart = card.querySelector('.chart');
  run('render({type: "answer", text: "支出 **300**"})');
  assert.equal(nodes.log.querySelector('.tool'), card);
  assert.equal(nodes.log.querySelector('.chart'), chart);
  assert.equal(card.querySelector('.out').textContent, '合计 300');
  assert.match(nodes.log.querySelector('.answer-content').innerHTML, /<strong>300<\/strong>/);
  assert.equal(nodes.log.querySelector('.process').open, true);
  run('render({type: "turn_end"})');
  assert.equal(nodes.log.querySelector('.running'), null);
});

test('double submission starts only one request before HTTP resolves', async () => {
  const { nodes, context, run, requests } = setup();
  let release;
  context.fetch = (url, options) => {
    requests.push({ url: String(url), options });
    return new Promise((resolve) => { release = resolve; });
  };
  nodes.input.value = '分析账单';
  const pending = run('send()');
  nodes.input.value = '第二条草稿';
  await run('send()');
  assert.equal(requests.length, 1);
  assert.equal(nodes.send.disabled, true);
  assert.match(requests[0].url, /token=test-token/);
  release({ ok: true, json: async () => ({ accepted: true }) });
  await pending;
  assert.equal(nodes.input.value, '第二条草稿');
});

test('failed submission restores original question without losing new draft', async () => {
  const { nodes, context, run } = setup();
  let release;
  context.fetch = () => new Promise((resolve) => { release = resolve; });
  nodes.input.value = '原问题';
  const pending = run('send()');
  nodes.input.value = '新草稿';
  release({ ok: false, status: 429, json: async () => ({ detail: '太快了' }) });
  await pending;
  assert.equal(nodes.input.value, '原问题\n新草稿');
  assert.equal(nodes.send.disabled, false);
  assert.equal(nodes.stop.hidden, true);
});

test('IME Enter does not submit; ordinary Enter submits', async () => {
  const { nodes, requests } = setup();
  nodes.input.value = '中文问题';
  let prevented = 0;
  const event = { key: 'Enter', shiftKey: false, isComposing: true, preventDefault: () => prevented++ };
  nodes.input.onkeydown(event);
  assert.equal(requests.length, 0);
  assert.equal(prevented, 0);
  nodes.input.listeners.compositionstart();
  nodes.input.onkeydown({ ...event, isComposing: false });
  assert.equal(requests.length, 0);
  nodes.input.listeners.compositionend();
  nodes.input.onkeydown({ ...event, isComposing: false });
  assert.equal(requests.length, 1);
  assert.equal(prevented, 1);
});

test('connection does not scroll the welcome page; historical reading stays put', () => {
  const { nodes, run } = setup();
  nodes.main.scrollHeight = 2000;
  run('render({type: "connected"})');
  assert.equal(nodes.main.scrollTop, 0);
  nodes.main.scrollTop = 150;
  run('followLatest = false; render({type: "step_start", step: 2})');
  assert.equal(nodes.main.scrollTop, 150);
  assert.equal(nodes.jump.hidden, false);
});

test('switching sessions destroys charts and dismisses the old confirmation', async () => {
  const { nodes, run, requests, context } = setup();
  let destroyed = 0;
  context.oldChart = { destroy: () => destroyed++ };
  run('charts.push(oldChart); askConfirm("修改旧会话的分类")');
  assert.equal(nodes.confirm.open, true);
  run('openSession("other-session")');
  assert.equal(nodes.confirm.open, false);
  assert.equal(destroyed, 1);
  await run('answer(true)');
  assert.equal(requests.length, 0);
});

test('SSE reconnect ignores delivered sequence IDs and fresh view replays history', () => {
  const { run, nodes } = setup();
  run('connect(); source.onmessage({lastEventId:"1",data:JSON.stringify({type:"turn_start",text:"核对账单",seq:1})})');
  const count = nodes.log.children.length;
  run('source.onmessage({lastEventId:"1",data:JSON.stringify({type:"turn_start",text:"核对账单",seq:1})})');
  assert.equal(nodes.log.children.length, count);
  run('source.onmessage({lastEventId:"2",data:JSON.stringify({type:"answer",text:"金额10",seq:2})})');
  const actions = nodes.log.querySelectorAll('.answer-actions').length;
  run('source.onmessage({lastEventId:"2",data:JSON.stringify({type:"answer",text:"金额10",seq:2})})');
  assert.equal(nodes.log.querySelectorAll('.answer-actions').length, actions);
  run('openSession("other"); source.onmessage({lastEventId:"1",data:JSON.stringify({type:"turn_start",text:"历史消息",seq:1})})');
  assert.equal(nodes.log.querySelector('.user').textContent, '历史消息');
});

test('confirmation submits its ID and stale resolution cannot close newer dialog', async () => {
  const { run, nodes, requests } = setup();
  run('render({type:"confirm_request",summary:"确认分类",confirmation_id:"current"})');
  run('render({type:"confirm_resolved",confirmation_id:"old"})');
  assert.equal(nodes.confirm.open, true);
  await run('answer(true)');
  assert.deepEqual(JSON.parse(requests[0].options.body), { allow: true, confirmation_id: 'current' });
  run('render({type:"confirm_request",summary:"确认分类",confirmation_id:"next"}); render({type:"confirm_resolved",confirmation_id:"next"})');
  assert.equal(nodes.confirm.open, false);
});

test('background upload polls state and preserves task ID on connection failure', async () => {
  const { run, nodes, context } = setup();
  const storage = new Map();
  context.localStorage = { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v) };
  context.fetch = async () => { throw new Error('连接中断'); };
  await assert.rejects(run('waitForKnowledgeJob({id:"pending-job"})'), /连接中断/);
  assert.deepEqual(JSON.parse(storage.get('fa.document-jobs')), ['pending-job']);
  context.fetch = async () => ({ ok: true, json: async () => ({ status:'succeeded', result:{chunks:3} }) });
  assert.equal((await run('waitForKnowledgeJob({id:"pending-job"})')).chunks, 3);
  assert.deepEqual(JSON.parse(storage.get('fa.document-jobs')), []);
  assert.equal(nodes['knowledge-cancel'].hidden, true);
});
