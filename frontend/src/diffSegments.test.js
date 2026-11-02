import assert from "node:assert/strict";
import test from "node:test";
import { segments } from "./diffSegments.js";

const rows = (pattern) => [...pattern].map((t) => ({ t: t === "." ? " " : t }));

test("changes show five lines of context and fold the rest", () => {
  const parts = segments(rows(".".repeat(20) + "+" + ".".repeat(20)), new Set());
  assert.deepEqual(parts.map((p) => [p.visible, p.start, p.end]),
    [[false, 0, 15], [true, 15, 26], [false, 26, 41]]);
});

test("revealed rows open part of a fold", () => {
  const parts = segments(rows(".".repeat(20) + "-"), new Set([0, 1]));
  assert.deepEqual(parts.map((p) => [p.visible, p.start, p.end]), [[true, 0, 2], [false, 2, 15], [true, 15, 21]]);
});

test("nearby changes merge into one visible block", () => {
  const parts = segments(rows("+" + ".".repeat(8) + "+"), new Set());
  assert.deepEqual(parts.map((p) => p.visible), [true]);
});
