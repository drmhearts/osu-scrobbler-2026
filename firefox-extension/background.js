/* Firefox alone owns website cookies. The desktop app only receives public
 * metadata and paths of downloads this extension started. */
"use strict";

const BRIDGE = "http://127.0.0.1:27895";
const OSU = "https://osu.ppy.sh";
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function bridge(path, token, options = {}) {
  const response = await fetch(BRIDGE + path, {
    ...options,
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    cache: "no-store"
  });
  if (!response.ok) throw new Error("bridge unavailable");
  return response.json();
}

function websiteError(response) {
  if (response.status === 429) return { error: "rate", retry: response.headers.get("Retry-After") };
  if (response.status === 401 || response.status === 403) return { error: "auth" };
  if (response.status >= 500) return { error: "transient", retry: response.headers.get("Retry-After") };
  return { error: "website" };
}

async function website(path, json = true) {
  const response = await fetch(OSU + path, {
    credentials: "include",
    headers: json ? { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }
                  : { Accept: "text/html" },
    redirect: "follow",
    cache: "no-store"
  });
  if (!response.ok) throw websiteError(response);
  if (!json) return response.text();
  try { return await response.json(); }
  catch (_) { throw { error: "auth" }; }
}

async function account() {
  const html = await website("/home", false);
  const page = new DOMParser().parseFromString(html, "text/html");
  const raw = page.querySelector("#json-current-user")?.textContent;
  let user;
  try { user = JSON.parse(raw || "{}"); }
  catch (_) { throw { error: "auth" }; }
  if (!Number.isInteger(user.id) || user.id <= 0) throw { error: "auth" };
  return String(user.id);
}

async function currentDownload(setId) {
  const stored = await browser.storage.local.get("receipts");
  const receipts = stored.receipts || {};
  const id = receipts[String(setId)];
  if (id === undefined) return null;
  const [item] = await browser.downloads.search({ id });
  if (item && item.state !== "interrupted" && (item.state !== "complete" || item.exists)) return id;
  delete receipts[String(setId)];
  await browser.storage.local.set({ receipts });
  return null;
}

async function saveDownload(setId, id) {
  const stored = await browser.storage.local.get("receipts");
  await browser.storage.local.set({ receipts: { ...(stored.receipts || {}), [String(setId)]: id } });
}

async function download(setId, token, taskId) {
  if (!Number.isInteger(setId) || setId <= 0) throw { error: "website" };
  let id = await currentDownload(setId);
  if (id === null) {
    id = await browser.downloads.download({
      url: `${OSU}/beatmapsets/${setId}/download`,
      filename: `osu-life-hacks/${setId}.osz`,
      conflictAction: "uniquify", saveAs: false, allowHttpErrors: false
    });
    await saveDownload(setId, id);
  }
  for (let attempt = 0; attempt < 1200; attempt++) {
    const [item] = await browser.downloads.search({ id });
    if (!item) throw { error: "transient" };
    if (item.state === "complete") {
      if (!item.exists) throw { error: "transient" };
      return { filename: item.filename, bytes: item.fileSize };
    }
    if (item.state === "interrupted") {
      if (item.error === "SERVER_UNAUTHORIZED" || item.error === "SERVER_FORBIDDEN") throw { error: "auth" };
      if (item.error === "SERVER_TOO_MANY_REQUESTS") throw { error: "rate" };
      throw { error: "transient" };
    }
    const status = await bridge("/status?id=" + encodeURIComponent(taskId), token);
    if (status.cancel) {
      await browser.downloads.cancel(id);
      throw { error: "cancelled" };
    }
    await wait(500);
  }
  await browser.downloads.cancel(id);
  throw { error: "transient" };
}

async function execute(task, token) {
  try {
    let value;
    if (task.action === "connect") value = await account();
    else if (task.action === "search") value = await website("/beatmapsets/search?" + new URLSearchParams(task.query));
    else if (task.action === "details") value = await website(`/beatmapsets/${task.set_id}`);
    else if (task.action === "download") value = await download(task.set_id, token, task.id);
    else if (task.action === "forget") {
      const saved = await browser.storage.local.get("receipts");
      const receipts = saved.receipts || {};
      delete receipts[String(task.set_id)];
      await browser.storage.local.set({ receipts });
      value = true;
    }
    else throw { error: "website" };
    await bridge("/result", token, { method: "POST", body: JSON.stringify({ id: task.id, value }) });
  } catch (failure) {
    const error = ["auth", "rate", "transient", "website", "cancelled"].includes(failure?.error)
      ? failure.error : "transient";
    if (error !== "cancelled") {
      try { await bridge("/result", token, { method: "POST", body: JSON.stringify({
        id: task.id, error, retry: failure?.retry || null
      }) }); } catch (_) { /* The worker may have closed after pause/cancel. */ }
    }
  }
}

async function loop() {
  while (true) {
    try {
      const saved = await browser.storage.local.get("pairing");
      if (saved.pairing) {
        const reply = await bridge("/next", saved.pairing);
        if (reply.task) await execute(reply.task, saved.pairing);
      }
    } catch (_) { /* The desktop app is not running or the pairing changed. */ }
    await wait(1000);
  }
}

loop();
