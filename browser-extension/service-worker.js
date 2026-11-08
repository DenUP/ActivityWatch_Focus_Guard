"use strict";

// The Windows app starts a session; this extension owns the browser-side block.
// Chrome persists dynamic rules and storage across browser restarts and the
// service worker being suspended. Closing the Windows app does not reset them.
const STATUS_URL = "http://127.0.0.1:8765/status";
const DEADLINE_KEY = "studyDeadline";
const ALARM_NAME = "focus-guard-sync";
const RULE_IDS = [101, 102, 103, 104, 105, 106, 107, 108];
const rules = [
  { id: 101, priority: 1, action: { type: "block" },
    condition: { urlFilter: "||youtube.com^" } },
  { id: 102, priority: 1, action: { type: "block" },
    condition: { urlFilter: "||youtu.be^" } },
  { id: 103, priority: 1, action: { type: "block" },
    condition: { urlFilter: "||youtube-nocookie.com^" } },
  // API and streaming hosts prevent continued playback in already-loaded tabs.
  { id: 104, priority: 1, action: { type: "block" },
    condition: { urlFilter: "||youtubei.googleapis.com^" } },
  { id: 105, priority: 1, action: { type: "block" },
    condition: { urlFilter: "||googlevideo.com^" } },
  { id: 106, priority: 2,
    action: { type: "redirect", redirect: { extensionPath: "/blocked.html" } },
    condition: { urlFilter: "||youtube.com^", resourceTypes: ["main_frame", "sub_frame"] } },
  { id: 107, priority: 2,
    action: { type: "redirect", redirect: { extensionPath: "/blocked.html" } },
    condition: { urlFilter: "||youtu.be^", resourceTypes: ["main_frame", "sub_frame"] } },
  { id: 108, priority: 2,
    action: { type: "redirect", redirect: { extensionPath: "/blocked.html" } },
    condition: { urlFilter: "||youtube-nocookie.com^", resourceTypes: ["main_frame", "sub_frame"] } },
];

function isYouTubeUrl(rawUrl) {
  try {
    const hostname = new URL(rawUrl).hostname.toLowerCase();
    return ["youtube.com", "youtu.be", "youtube-nocookie.com"].some(
      domain => hostname === domain || hostname.endsWith("." + domain)
    );
  } catch (_) {
    return false;
  }
}

async function readWindowsStatus() {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 2500);
  try {
    const response = await fetch(STATUS_URL, { cache: "no-store", signal: abort.signal });
    if (!response.ok) return null;
    const payload = await response.json();
    if (!payload || typeof payload.active !== "boolean") return null;
    return payload;
  } catch (_) {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

async function redirectAlreadyOpenTabs() {
  const tabs = await chrome.tabs.query({});
  const blocked = chrome.runtime.getURL("blocked.html");
  await Promise.all(tabs.map(async tab => {
    if (tab.id == null || !isYouTubeUrl(tab.url || "")) return;
    try {
      await chrome.tabs.update(tab.id, { url: blocked });
    } catch (_) {
      // A tab may be closed or belong to another protected browser context.
    }
  }));
}

let inFlight = null;

async function reconcile(forceRules = false) {
  const stored = await chrome.storage.local.get(DEADLINE_KEY);
  const previousDeadline = Number(stored[DEADLINE_KEY]) || 0;
  const remote = await readWindowsStatus();
  const remoteDeadline = remote && remote.active ? Date.parse(remote.ends_at) : NaN;
  // A missing/offline desktop app cannot shorten a session already acknowledged
  // by the browser. The Windows app also refuses to cancel it early.
  const deadline = Math.max(previousDeadline, Number.isFinite(remoteDeadline) ? remoteDeadline : 0);
  const active = deadline > Date.now();

  if (deadline !== previousDeadline) {
    await chrome.storage.local.set({ [DEADLINE_KEY]: deadline });
  }

  const installed = await chrome.declarativeNetRequest.getDynamicRules();
  const ownIds = installed.filter(rule => RULE_IDS.includes(rule.id)).map(rule => rule.id);
  if (forceRules || (active && ownIds.length !== RULE_IDS.length) || (!active && ownIds.length)) {
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: RULE_IDS,
      addRules: active ? rules : [],
    });
  }

  if (active) await redirectAlreadyOpenTabs();
  return { active, endsAt: active ? deadline : null, appOnline: remote !== null,
           ruleCount: active ? RULE_IDS.length : 0 };
}

function sync(forceRules = false) {
  if (inFlight) return inFlight;
  inFlight = reconcile(forceRules).finally(() => { inFlight = null; });
  return inFlight;
}

async function ensureAlarm() {
  const old = await chrome.alarms.get(ALARM_NAME);
  if (!old) await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.5 });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm().then(() => sync(true)).catch(console.error);
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm().then(() => sync()).catch(console.error);
});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM_NAME) sync().catch(console.error);
});
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "FOCUS_GUARD_SYNC") {
    sync().then(sendResponse).catch(error => sendResponse({ error: String(error) }));
    return true;
  }
  return false;
});

// Also reconcile when a YouTube tab is activated: no dependence on AW's timer.
chrome.tabs.onActivated.addListener(() => { sync().catch(console.error); });
ensureAlarm().then(() => sync()).catch(console.error);
