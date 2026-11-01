// Usage: node run_stream.mjs <index.html> < scenario.json
// Like run_page.mjs, but /api/chat/stream answers with a controllable byte stream. The scenario
// lists chunks (strings); the DOM is captured after each one so tests can see what the page
// showed before the stream finished. Nothing here talks to a network.
import fs from "node:fs";
import vm from "node:vm";
import { createPage, serialize } from "./fake_dom.mjs";

const html = fs.readFileSync(process.argv[2], "utf8");
const script = [...html.matchAll(/<script>\n([\s\S]*?)<\/script>/g)].map((m) => m[1])[0];
const scenario = JSON.parse(fs.readFileSync(0, "utf8"));
const { document, elements } = createPage();
const fetchCalls = [];
const intervals = { set: 0, cleared: 0 };
const encoder = new TextEncoder();
const tick = () => new Promise((resolve) => setImmediate(resolve));

const queue = [];
let waiting = null;
let cancelled = false;
const push = (item) => { if (waiting) { const w = waiting; waiting = null; w(item); } else queue.push(item); };
const reader = {
  read: () => {
    if (queue.length) return queue.shift()();
    return new Promise((resolve, reject) => { waiting = (item) => item().then(resolve, reject); });
  },
  cancel: async () => { cancelled = true; },
};
const header = (name) => (scenario.headers ?? {})[name] ?? (name === "Content-Type" ? "text/event-stream; charset=utf-8" : null);
const streamResponse = {
  ok: (scenario.status ?? 200) === 200, status: scenario.status ?? 200,
  headers: { get: (name) => header(name) },
  body: { getReader: () => reader },
  json: async () => scenario.errorBody ?? {},
};
const ordinary = { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => scenario.ordinaryBody };

const window = {
  setInterval: () => { intervals.set += 1; return intervals.set; },
  clearInterval: () => { intervals.cleared += 1; },
  location: { hash: "" }, addEventListener() {}, scrollTo() {},
};
const context = vm.createContext({
  document, window, console, Date, Promise, JSON, Math, Object, Array, String, Number,
  TextDecoder, ReadableStream: function ReadableStream() {},
  fetch: async (url, options) => {
    // The page's load-time GET for the agents' models is not a chat request; see run_page.mjs.
    if (String(url).startsWith("/api/team-models")) return { ok: false, json: async () => null };
    fetchCalls.push({ url, body: JSON.parse(options.body), accept: (options.headers ?? {}).Accept ?? null });
    if (url === "/api/chat/stream") return streamResponse;
    return ordinary;
  },
});
vm.runInContext(script, context);

const frames = [];
elements.input.value = scenario.message ?? "hello";
const finished = elements.form.listeners.submit({ preventDefault() {} });
await tick();
for (const chunk of scenario.chunks ?? []) {
  push(async () => ({ done: false, value: encoder.encode(chunk) }));
  await tick(); await tick();
  frames.push(serialize(elements.conversation));
}
if (scenario.error) push(async () => { throw new Error(scenario.error); });
else if (!scenario.hang) push(async () => ({ done: true }));
await Promise.race([finished, tick().then(tick).then(tick)]);

const clicks = [];
for (const name of scenario.clickTabs ?? []) {
  const button = elements.conversation.querySelector(`.tab-${name}`);
  if (button) button.listeners.click({}); 
  clicks.push(serialize(elements.conversation));
}
console.log(JSON.stringify({
  frames, clicks, final: serialize(elements.conversation), sendDisabled: elements.send.disabled,
  fetchCalls, intervals, cancelled,
}));
