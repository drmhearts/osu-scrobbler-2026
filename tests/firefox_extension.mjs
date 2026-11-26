import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../firefox-extension/background.js", import.meta.url), "utf8")
  .replace(/\nloop\(\);\s*$/, "");
const calls = [];
const storage = {};
const browser = {
  storage: { local: {
    get: async key => ({ [key]: storage[key] }),
    set: async values => Object.assign(storage, values)
  } },
  downloads: {
    download: async options => { calls.push(["download", options]); return 7; },
    search: async () => [{ id: 7, state: "complete", exists: true,
                            filename: "C:/Users/Test/Downloads/osu-life-hacks/42.osz", fileSize: 11 }]
  }
};
const context = vm.createContext({
  browser, URLSearchParams, setTimeout,
  DOMParser: class { parseFromString() { return { querySelector: () => ({ textContent: '{"id":123}' }) }; } },
  fetch: async (url, options) => {
    calls.push(["fetch", url, options]);
    return { ok: true, json: async () => ({ beatmapsets: [] }), text: async () => "<html></html>" };
  }
});
vm.runInContext(source + "\nglobalThis.testApi = {website, account, download};", context);

assert.equal(await context.testApi.account(), "123");
assert.equal(calls[0][2].credentials, "include");
assert.equal((await context.testApi.website("/beatmapsets/search?m=0")).beatmapsets.length, 0);
assert.equal(calls[1][2].credentials, "include");
const result = await context.testApi.download(42, "pairing", "task-id");
assert.equal(result.fileSize, undefined);
assert.equal(result.bytes, 11);
assert.equal(calls[2][1].url, "https://osu.ppy.sh/beatmapsets/42/download");
assert.equal(calls[2][1].conflictAction, "uniquify");
assert.equal(calls[2][1].saveAs, false);
assert.equal(storage.receipts["42"], 7);
await context.testApi.download(42, "pairing", "later-task");
assert.equal(calls.filter(call => call[0] === "download").length, 1);
console.log("Firefox extension browser-session and receipt checks passed");
