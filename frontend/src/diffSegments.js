export const CONTEXT = 5;

// Which rows are visible: every change, CONTEXT lines around it, and anything revealed.
export function segments(rows, revealed) {
  const show = new Array(rows.length).fill(false);
  rows.forEach((row, i) => {
    if (row.t !== " ") {
      for (let j = Math.max(0, i - CONTEXT); j <= Math.min(rows.length - 1, i + CONTEXT); j++) show[j] = true;
    }
  });
  for (const i of revealed) show[i] = true;
  const out = [];
  let i = 0;
  while (i < rows.length) {
    const start = i;
    const visible = show[i];
    while (i < rows.length && show[i] === visible) i++;
    out.push({ visible, start, end: i });  // [start, end)
  }
  return out;
}
