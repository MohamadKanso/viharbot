// Usage: node run_page.mjs <index.html> < scenario.json
// Runs the page's inline script against the fake DOM, submits one message, and prints the DOM
// while the request is pending and after it settles.
import fs from "node:fs";
import vm from "node:vm";
import { createPage, serialize, getActive } from "./fake_dom.mjs";

const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script>\n([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length !== 1) throw new Error(`expected one inline script, found ${scripts.length}`);
const scenario = JSON.parse(fs.readFileSync(0, "utf8"));

const { document, elements, created } = createPage();
const fetchCalls = [];
let settle;
const fetchPromise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
const intervals = { set: 0, cleared: 0 };
const listeners = {};
const scrolls = [];
const window = {
  setInterval: () => { intervals.set += 1; return intervals.set; },
  clearInterval: () => { intervals.cleared += 1; },
  location: { hash: scenario.initialHash ?? "" },
  addEventListener: (type, handler) => { listeners[type] = handler; },
  scrollTo: (x, y) => { scrolls.push([x, y]); },
};
const context = vm.createContext({
  document, window, console, Date, Promise, JSON, Math, Object, Array, String, Number,
  // The page asks once, at load, which model each agent uses. That GET is not a chat request, so
  // it is answered "not available" and kept out of the recorded calls.
  fetch: (url, options) => {
    if (String(url).startsWith("/api/development/availability")) return Promise.resolve({ ok: false, json: async () => null });
    if (String(url).startsWith("/api/team-models")) return Promise.resolve({ ok: false, json: async () => null });
    fetchCalls.push({ url, options });
    return fetchPromise;
  },
});
vm.runInContext(scripts[0], context);

const pageState = () => ({
  hash: window.location.hash,
  chatHidden: elements.chatPage.hidden,
  teamHidden: elements.teamPage.hidden,
  title: document.title,
  linkCurrent: elements.teamLink.getAttribute("aria-current"),
  teamHeadingTabindex: elements.teamHeading.getAttribute("tabindex"),
  focusedTag: getActive()?.tagName ?? null,
  focusedIsTeamHeading: getActive() === elements.teamHeading,
  focusedIsInput: getActive() === elements.input,
  conversationChildren: elements.conversation.children.length,
  draft: elements.input.value,
});
const initialPage = pageState();

let pending = null;
if (!scenario.skipSubmit) {
  elements.input.value = scenario.message ?? "hello";
  const submit = elements.form.listeners.submit;
  var finished = submit({ preventDefault() {} });
  await Promise.resolve();
  pending = {
    tree: serialize(elements.conversation),
    sendDisabled: elements.send.disabled,
  };

  if (scenario.reject) {
    settle.reject(new Error(scenario.reject));
  } else {
    const { status = 200, body } = scenario.response;
    settle.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
  }
  await finished;
}

// Navigate like a browser would: change the hash, then fire hashchange.
const navigation = [];
for (const hash of scenario.navigate ?? []) {
  if (scenario.draftBeforeNavigate !== undefined) elements.input.value = scenario.draftBeforeNavigate;
  window.location.hash = hash;
  listeners.hashchange?.({});
  navigation.push(pageState());
}

console.log(JSON.stringify({
  initialPage,
  navigation,
  scrolls,
  hasHashchangeListener: typeof listeners.hashchange === "function",
  pending,
  final: { tree: serialize(elements.conversation), sendDisabled: elements.send.disabled },
  fetchCalls: fetchCalls.map((c) => ({ url: c.url, method: c.options.method, body: JSON.parse(c.options.body) })),
  intervals,
  createdTags: created.map((el) => el.tagName),
}));
