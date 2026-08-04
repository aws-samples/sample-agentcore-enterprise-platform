#!/usr/bin/env node
// Self-check for the dashboard's statusClass logic (extracted from index.html,
// so it tests the shipped code, not a copy). Fails loudly if the substring
// ordering bug ever returns. Run: node dashboard/check-status-logic.mjs
import { readFileSync, existsSync } from "node:fs";
import { strict as assert } from "node:assert";

const html = readFileSync(new URL("./public/index.html", import.meta.url), "utf8");
const src = html.match(/function statusClass\(s\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(src, "statusClass not found in index.html");
const statusClass = new Function(`return ${src}`)();

const cases = {
  NOT_DEPLOYED: "not-deployed",       // the 10/10 bug: contains "deployed"
  DELETE_COMPLETE: "not-deployed",    // contains "complete"
  CREATE_COMPLETE: "deployed",
  UPDATE_COMPLETE: "deployed",
  ROLLBACK_COMPLETE: "failed",        // contains "complete"
  UPDATE_ROLLBACK_FAILED: "failed",
  CREATE_IN_PROGRESS: "in-progress",
  "": "not-deployed",
};
for (const [input, want] of Object.entries(cases)) {
  assert.equal(statusClass(input), want, `statusClass(${JSON.stringify(input)})`);
}

// Suffix resolution (the all-cards-NOT-DEPLOYED bug): cards use short names,
// monitor.py keys by full stack name. Same expression as render()'s bySuffix.
{
  const stacks = { "agentcore-workshop-dev-auth": { status: "CREATE_COMPLETE" } };
  const allNames = Object.keys(stacks);
  const bySuffix = short => stacks[allNames.find(n => n.endsWith("-" + short)) || short];
  assert.equal(bySuffix("auth")?.status, "CREATE_COMPLETE", "short name resolves to full stack key");
  assert.equal(bySuffix("networking"), undefined, "missing stack stays undefined");
  console.log("suffix resolution: OK");
}

// Against the live monitor output, if present: counts must match monitor.py's summary.
const statusFile = new URL("./public/status.json", import.meta.url);
if (existsSync(statusFile)) {
  const data = JSON.parse(readFileSync(statusFile, "utf8"));
  const got = Object.values(data.stacks).filter(s => statusClass(s.status) === "deployed").length;
  assert.equal(got, data.summary.deployed, "deployed count vs monitor summary");
  console.log(`live status.json: ${got}/${Object.keys(data.stacks).length} deployed — matches monitor summary`);
}
console.log("statusClass self-check: OK");
