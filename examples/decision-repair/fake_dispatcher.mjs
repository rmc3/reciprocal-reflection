/** Fixed local fixture function. There is no live transport or injected callback. */
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";

export const MODES = new Set([
  "success", "receipt_only", "timeout_before", "timeout_after", "rejected",
  "completion_failed", "wrong_nonce", "wrong_id", "out_of_order",
  "unsafe_output", "duplicate",
]);
const fixture = fs.readFileSync(new URL("./fixture-output.json", import.meta.url));
export const fixtureHash = createHash("sha256").update(fixture).digest("hex");

export function fakeDispatch(request, permit, mode) {
  if (!MODES.has(mode) || permit.transport !== "fake") throw new Error("fake transport only");
  if (!/^[0-9a-f]{32}$/.test(permit.nonce)) throw new Error("invalid fixture nonce");
  if (mode === "timeout_before") throw new Error("simulated loss before fixture acceptance");
  const root = fs.lstatSync(permit.output_root);
  if (!root.isDirectory() || root.isSymbolicLink()
      || root.dev !== permit.root_dev || root.ino !== permit.root_ino) {
    throw new Error("fixture root changed");
  }
  // The journal is diagnostic only. The bridge never treats it as a receipt.
  const journal = path.join(permit.output_root, "fake-invocations.jsonl");
  const fd = fs.openSync(journal,
    fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT
      | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK, 0o600);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.nlink !== 1) throw new Error("fixture journal is not a regular single-link file");
    fs.writeSync(fd, JSON.stringify({request, action_id: permit.action_id, nonce: permit.nonce}) + "\n");
  } finally { fs.closeSync(fd); }
  const common = {transport: "fake", action_id: permit.action_id,
    nonce: permit.nonce, request_id: "fake-" + permit.nonce};
  const receipt = {...common, event_id: permit.nonce + "-receipt",
    kind: mode === "rejected" ? "rejected" : "accepted"};
  if (mode === "rejected" || mode === "receipt_only") return [receipt];
  const folder = path.join(permit.output_root, permit.nonce);
  fs.mkdirSync(folder, {mode: 0o700});
  fs.writeFileSync(path.join(folder, "result.json"), fixture, {flag: "wx", mode: 0o600});
  if (mode === "timeout_after") throw new Error("simulated receipt loss after fixture acceptance");
  const completion = {...common, event_id: permit.nonce + "-completion", kind: "completed",
    success: mode !== "completion_failed", outputs: [{path: "result.json", sha256: fixtureHash}]};
  if (mode === "wrong_nonce") {
    receipt.nonce = "0".repeat(32);
    completion.nonce = "0".repeat(32);
  }
  if (mode === "wrong_id") {
    receipt.request_id = "fake-wrong";
    completion.request_id = "fake-wrong";
  }
  if (mode === "unsafe_output") completion.outputs[0].path = "../outside.json";
  if (mode === "out_of_order") return [completion, receipt];
  if (mode === "duplicate") return [receipt, receipt, completion, completion];
  return [receipt, completion];
}
