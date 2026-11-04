// Usage: node run_wiki.mjs <index.html> < {api, hashes}. Navigates the Development Wiki on the fake DOM.
import fs from "node:fs";
import vm from "node:vm";
import { createPage, Element } from "./fake_dom.mjs";

const html = fs.readFileSync(process.argv[2], "utf8");
const script = [...html.matchAll(/<script>\n([\s\S]*?)<\/script>/g)][0][1];
const { api, hashes } = JSON.parse(fs.readFileSync(0, "utf8"));
const { document, root, elements } = createPage();
const add = (tag, id) => { const e = new Element(tag); e.attrs.id = id; root.append(e); return e; };
const wiki = add("main", "wiki-page"); wiki.hidden = true;
add("h1", "wiki-heading"); const link = add("a", "wiki-link"); link.hidden = true;
["dashboard", "docs", "timeline"].forEach((k) => add("a", `wiki-tab-${k}`));
add("p", "wiki-sync"); add("div", "wiki-body");
const listeners = {};
const window = { setInterval() { return 1; }, clearInterval() {}, location: { hash: "" },
  addEventListener: (t, h) => { listeners[t] = h; }, scrollTo() {} };
const fetched = [];
const ctx = vm.createContext({ document, window, console, Date, Promise, JSON, Math, Object, Array,
  String, Number, encodeURIComponent, decodeURIComponent,
  fetch: async (url) => {
    if (String(url).startsWith("/api/team-models")) return { ok: false, json: async () => null };
    fetched.push(url);
    const d = api[decodeURI(url.split("?")[0])];
    return { ok: !!d, status: d ? 200 : 404, json: async () => d };
  } });
vm.runInContext(script, ctx);
const text = (n) => (n.nodeType === 3 ? n.text : n.children.map(text).join(" "));
const tags = (n, acc = []) => { if (n.nodeType === 1) { acc.push(n.tagName); n.children.forEach((c) => tags(c, acc)); } return acc; };
const out = [];
await new Promise((r) => setTimeout(r, 10));
const linkHiddenAtLoad = link.hidden;
for (const hash of hashes) {
  window.location.hash = hash; listeners.hashchange({});
  await new Promise((r) => setTimeout(r, 10));
  const body = root.querySelector("#wiki-body");
  out.push({ hash, wikiHidden: wiki.hidden, chatHidden: elements.chatPage.hidden, current: link.getAttribute("aria-current"),
    sync: text(root.querySelector("#wiki-sync")), body: text(body), tags: tags(body) });
}
console.log(JSON.stringify({ out, fetched, linkHiddenAtLoad }));
