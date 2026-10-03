/** Node/Python fake bridge. Fixed Python program, no shell, no external tool API. */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { fakeDispatch, fixtureHash, MODES } from "./fake_dispatcher.mjs";

const store = fileURLToPath(new URL("./bridge_store.py", import.meta.url));

export function ledger(database, operation, payload) {
  const proc = spawnSync("python3", [store, "--db", database, operation], {
    input: JSON.stringify(payload), encoding: "utf8", shell: false,
    timeout: 30000, maxBuffer: 2 * 1024 * 1024,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: "1"},
  });
  if (proc.error) throw proc.error;
  let answer;
  try { answer = JSON.parse(proc.stdout); }
  catch { throw new Error("ledger response unavailable; do not redispatch"); }
  if (proc.status !== 0 || answer.ok !== true) throw new Error(answer.error || "ledger operation failed");
  return answer.result;
}

function encodedManifest(filename) {
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size > 65536) throw new Error("manifest must be a bounded regular file");
    // Bounded read even if another process grows the file after fstat.
    const buffer = Buffer.alloc(65537);
    const count = fs.readSync(fd, buffer, 0, buffer.length, 0);
    if (count > 65536) throw new Error("manifest grew beyond size bound");
    return buffer.subarray(0, count).toString("base64");
  } finally { fs.closeSync(fd); }
}

export function runFake({database, manifestPath, actionId, mode = "success"}) {
  if (!MODES.has(mode)) throw new Error("unknown mode; fake transport only");
  const reserved = ledger(database, "reserve", {
    action_id: actionId, manifest_b64: encodedManifest(manifestPath),
  });
  if (reserved.status !== "RESERVED") return {action: reserved, observations: [], fake_called: false};
  // A second read detects drift; the outgoing request comes from the frozen permit,
  // not from independently reconstructed Node arguments.
  const permit = ledger(database, "begin", {
    action_id: actionId, manifest_b64: encodedManifest(manifestPath),
  });
  let observations;
  try {
    observations = fakeDispatch(permit.request, permit, mode);
  } catch (error) {
    const action = ledger(database, "uncertain", {
      action_id: actionId, nonce: permit.nonce, reason: error.message,
    });
    return {action, observations: [], fake_called: true, error: error.message};
  }
  const results = [];
  for (const observation of observations) {
    // If persistence fails here, the committed attempt remains a hold. Never retry dispatch.
    results.push(ledger(database, "observe", {action_id: actionId, observation}));
  }
  let action = ledger(database, "inspect", {identity: actionId});
  if (action.status === "IN_FLIGHT") {
    action = ledger(database, "uncertain", {
      action_id: actionId, nonce: permit.nonce, reason: "no acceptable dispatch receipt",
    });
  }
  if (action.status === "COMPLETION_OBSERVED") {
    action = ledger(database, "verify", {action_id: actionId});
  }
  return {action, observations: results, fake_called: true};
}

export function exampleManifest() {
  return {schema: 1, transport: "fake", request: {
    model: "fixture-model-not-invoked", reasoning_effort: "high",
    objective: "Return a fixed local fixture; do not call any model.",
    task_name: "Fake dispatch fixture", preload_skills: [], extended_context: false,
  }, outputs: [{path: "result.json", sha256: fixtureHash}]};
}

function demo() {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "fake-bridge-demo-"));
  try {
    const database = path.join(folder, "ledger.sqlite");
    const output = path.join(folder, "outputs");
    const manifestPath = path.join(folder, "manifest.json");
    fs.mkdirSync(output);
    const raw = Buffer.from(JSON.stringify(exampleManifest()));
    fs.writeFileSync(manifestPath, raw);
    ledger(database, "init", {manifest_b64: raw.toString("base64"),
      output_root: output, max_attempts: 1, approval_ref: "fixture:demo-only"});
    const result = runFake({database, manifestPath, actionId: "demo"});
    return {demo: "synthetic only", ...result, ledger: ledger(database, "export", {})};
  } finally { fs.rmSync(folder, {recursive: true, force: true}); }
}

function main(args) {
  if (args.length === 1 && args[0] === "demo") return demo();
  if (args.shift() !== "run") throw new Error("usage: bridge.mjs demo | run --db D --manifest M --action A [--mode MODE]");
  const allowed = new Set(["--db", "--manifest", "--action", "--mode"]);
  const options = {};
  while (args.length) {
    const flag = args.shift();
    if (!allowed.has(flag) || flag in options || !args.length || args[0].startsWith("--")) {
      throw new Error("unknown, duplicate or incomplete flag");
    }
    options[flag] = args.shift();
  }
  if (!options["--db"] || !options["--manifest"] || !options["--action"]) {
    throw new Error("--db, --manifest and --action are required");
  }
  return runFake({database: options["--db"], manifestPath: options["--manifest"],
    actionId: options["--action"], mode: options["--mode"] || "success"});
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify({ok: true, result: main(process.argv.slice(2))}, null, 2)); }
  catch (error) {
    console.log(JSON.stringify({ok: false, error: error.message}));
    process.exitCode = 2;
  }
}
