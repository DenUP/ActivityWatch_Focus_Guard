// Optional Node.js tests for the Manifest V3 rule synchronization logic.
// Run: node --test test_extension.cjs
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");

const script = fs.readFileSync("browser-extension/service-worker.js", "utf8");
const manifest = JSON.parse(fs.readFileSync("browser-extension/manifest.json", "utf8"));

function harness() {
  let clock = 2_000_000_000_000;
  let online = true;
  let remote = { active: false, ends_at: null };
  let installed = [];
  let tabs = [{ id: 1, url: "https://www.youtube.com/watch?v=a" },
              { id: 2, url: "https://github.com/DenUP" }];
  const storage = {};
  const FakeDate = class extends Date { static now() { return clock; } };
  const chrome = {
    storage: { local: {
      async get(key) { return { [key]: storage[key] }; },
      async set(values) { Object.assign(storage, values); },
    } },
    declarativeNetRequest: {
      async getDynamicRules() { return installed; },
      async updateDynamicRules({ removeRuleIds, addRules }) {
        installed = installed.filter(rule => !removeRuleIds.includes(rule.id)).concat(addRules);
      },
    },
    tabs: {
      async query() { return tabs; },
      async update(id, values) { tabs = tabs.map(tab => tab.id === id ? { ...tab, ...values } : tab); },
      onActivated: { addListener() {} },
    },
    runtime: {
      getURL(file) { return "chrome-extension://focusguard/" + file; },
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener() {} },
    },
    alarms: {
      async get() { return { name: "focus-guard-sync" }; },
      async create() {},
      onAlarm: { addListener() {} },
    },
  };
  const context = vm.createContext({
    chrome, URL, Date: FakeDate, AbortController, setTimeout, clearTimeout, console,
    async fetch() {
      if (!online) throw Error("EXE is closed");
      return { ok: true, async json() { return remote; } };
    },
  });
  vm.runInContext(script, context);
  return {
    sync: () => vm.runInContext("sync()", context),
    isYouTubeUrl: url => vm.runInContext(`isYouTubeUrl(${JSON.stringify(url)})`, context),
    getRules: () => installed,
    getTabs: () => tabs,
    getDeadline: () => storage.studyDeadline,
    setRemote: value => { remote = value; },
    setOnline: value => { online = value; },
    setClock: value => { clock = value; },
    now: () => clock,
  };
}

test("MV3 extension scopes navigation to YouTube hosts, never lookalikes", () => {
  assert.equal(manifest.manifest_version, 3);
  const app = harness();
  assert.equal(app.isYouTubeUrl("https://music.youtube.com/"), true);
  assert.equal(app.isYouTubeUrl("https://youtu.be/abc"), true);
  assert.equal(app.isYouTubeUrl("https://youtube.com.evil.example/"), false);
  assert.equal(app.isYouTubeUrl("https://github.com"), false);
});

test("a timed study block persists through EXE closure and expires", async () => {
  const app = harness();
  app.setRemote({ active: true, ends_at: new Date(app.now() + 25 * 60_000).toISOString() });
  let result = await app.sync();
  assert.equal(result.active, true);
  assert.equal(result.appOnline, true);
  assert.equal(app.getRules().length, 8);
  assert.equal(app.getTabs()[0].url, "chrome-extension://focusguard/blocked.html");
  assert.equal(app.getTabs()[1].url, "https://github.com/DenUP");
  app.setOnline(false);
  app.setClock(app.now() + 10 * 60_000);
  result = await app.sync();
  assert.equal(result.active, true);
  assert.equal(result.appOnline, false);
  assert.equal(app.getRules().length, 8);
  app.setClock(app.getDeadline() + 1000);
  result = await app.sync();
  assert.equal(result.active, false);
  assert.equal(app.getRules().length, 0);
});

test("remote updates cannot shorten an already-started session", async () => {
  const app = harness();
  const end = app.now() + 60 * 60_000;
  app.setRemote({ active: true, ends_at: new Date(end).toISOString() });
  await app.sync();
  app.setRemote({ active: false, ends_at: null });
  await app.sync();
  assert.equal(app.getDeadline(), end);
  assert.equal(app.getRules().length, 8);
});
