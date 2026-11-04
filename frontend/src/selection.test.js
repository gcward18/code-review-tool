import assert from "node:assert/strict";
import test from "node:test";

import { coverage, toggleAll, toggleOne } from "./selection.js";

const paths = ["a.py", "b.py", "c.py"];

test("coverage reports all, none or some of the files", () => {
  assert.equal(coverage(paths, new Set(paths)), "all");
  assert.equal(coverage(paths, new Set()), "none");
  assert.equal(coverage(paths, new Set(["b.py"])), "some");
  assert.equal(coverage([], new Set()), "none");
});

test("the header checkbox selects every file, or clears them when all are selected", () => {
  assert.deepEqual([...toggleAll(paths, new Set())], paths);
  assert.deepEqual([...toggleAll(paths, new Set(["a.py"]))], paths); // partial selects all
  assert.deepEqual([...toggleAll(paths, new Set(paths))], []);
});

test("a file checkbox toggles only that file", () => {
  assert.deepEqual([...toggleOne("b.py", new Set(["a.py"]))].sort(), ["a.py", "b.py"]);
  assert.deepEqual([...toggleOne("a.py", new Set(["a.py"]))], []);
});
