"use strict";

const code = document.getElementById("code");
const status = document.getElementById("status");

browser.storage.local.get("pairing").then(saved => {
  status.textContent = saved.pairing ? "Code saved. Open osu! in this Firefox profile and sign in normally." : "No code saved yet.";
});

document.getElementById("save").addEventListener("click", async () => {
  const token = code.value.trim();
  if (token.length < 24) { status.textContent = "Paste the full pairing code from the app."; return; }
  await browser.storage.local.set({ pairing: token });
  code.value = "";
  status.textContent = "Code saved. Use Maps → Sign in to check the connection.";
});
