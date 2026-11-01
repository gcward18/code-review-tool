import test from "node:test";
import assert from "node:assert/strict";
import { parseRoute, routeHref } from "./routes.js";

test("bookmarks round-trip every page and focused resource", () => {
  for (const view of [
    { kind: "home" }, { kind: "settings" },
    { kind: "agents", agent: "typescript" }, { kind: "agents", agent: "custom agent/#?" },
    { kind: "knowledge", base: 12 }, { kind: "knowledge", base: null },
    { kind: "review", id: 42 }, { kind: "project", id: 9 },
    { kind: "repo", repo: "/Users/a/Project #1 + 日本語?" },
  ]) assert.deepEqual(parseRoute(routeHref(view)), view);
});

test("entry URL and default agent selection", () => {
  assert.deepEqual(parseRoute(""), { kind: "home" });
  assert.deepEqual(parseRoute("#/agents"), { kind: "agents", agent: "python" });
});

test("malformed routes are recoverable and never select an invalid resource", () => {
  for (const hash of ["#/review/nope", "#/review/0", "#/review/-1", "#/knowledge/NaN", "#/repo", "#/settings/extra", "#/agents/%ZZ", "#/unknown", "#/review/1/extra"]) {
    assert.deepEqual(parseRoute(hash), { kind: "not-found" }, hash);
  }
});
