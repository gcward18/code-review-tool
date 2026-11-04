// Selection state for the plan's file checkboxes.

/** "all", "none" or "some" of `paths` that are in `chosen`. */
export function coverage(paths, chosen) {
  const count = paths.filter((path) => chosen.has(path)).length;
  if (count === 0) return "none";
  return count === paths.length ? "all" : "some";
}

/** The header checkbox: select every path unless all are selected, then clear them. */
export function toggleAll(paths, chosen) {
  return coverage(paths, chosen) === "all" ? new Set() : new Set(paths);
}

/** One file's checkbox. */
export function toggleOne(path, chosen) {
  const next = new Set(chosen);
  next.has(path) ? next.delete(path) : next.add(path);
  return next;
}
