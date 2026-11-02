// Small Markdown renderer for model answers: builds React elements (never raw HTML),
// so text from the model cannot inject markup. Covers what the agents write:
// headings, paragraphs, lists (nested), quotes, tables, rules, fenced code, and
// inline code, bold, italic, strikethrough and http(s) links.

const KEYWORDS = {
  python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
  js: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield interface type enum implements",
  java: "abstract boolean break case catch char class continue default do double else enum extends final finally float for if implements import instanceof int interface long new null package private protected public return static super switch this throw throws true false try void while var string bool using namespace func go defer chan map struct range package nil",
  bash: "if then else elif fi for while do done case esac function in export local return echo",
};
const ALIASES = {
  py: "python", python: "python", js: "js", jsx: "js", ts: "js", tsx: "js", javascript: "js",
  typescript: "js", json: "js", java: "java", go: "java", cs: "java", csharp: "java", c: "java",
  cpp: "java", rust: "java", sh: "bash", bash: "bash", shell: "bash", zsh: "bash",
};
const HASH_COMMENTS = new Set(["python", "bash"]);

function isDiff(lang, code) {
  if (lang === "diff") return true;
  const lines = code.split("\n").filter(Boolean);
  return lines.some((l) => l.startsWith("@@")) ||
    (lines.length > 0 && lines.every((l) => /^([-+] |  |@@)/.test(l)) && lines.some((l) => /^[-+] /.test(l)));
}

function highlight(code, lang) {
  const kind = ALIASES[lang] || (lang ? null : "js");
  if (!kind) return code;
  const words = new Set(KEYWORDS[kind].split(" "));
  const comment = HASH_COMMENTS.has(kind) ? "#.*" : "\\/\\/.*|\\/\\*[\\s\\S]*?\\*\\/";
  const token = new RegExp(
    `(${comment})|("(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'|\`(?:\\\\.|[^\`\\\\])*\`)|\\b(\\d+(?:\\.\\d+)?)\\b|\\b([A-Za-z_]\\w*)\\b`,
    "g",
  );
  const out = [];
  let last = 0;
  for (const m of code.matchAll(token)) {
    if (m.index > last) out.push(code.slice(last, m.index));
    const [text, isComment, isString, isNumber, word] = m;
    const cls = isComment ? "tok-comment" : isString ? "tok-string" : isNumber ? "tok-number"
      : words.has(word) ? "tok-keyword" : code[m.index + text.length] === "(" ? "tok-call" : null;
    out.push(cls ? <span key={m.index} className={cls}>{text}</span> : text);
    last = m.index + text.length;
  }
  if (last < code.length) out.push(code.slice(last));
  return out;
}

function Code({ lang, code }) {
  if (isDiff(lang, code)) {
    return (
      <pre className="md-code fix">
        {code.split("\n").map((line, i) => {
          const cls = line.startsWith("@@") ? "hunk" : /^\+/.test(line) ? "add" : /^-/.test(line) ? "del" : "";
          return <div key={i} className={cls}>{line || " "}</div>;
        })}
      </pre>
    );
  }
  return (
    <pre className="md-code">
      {lang && <span className="md-lang">{lang}</span>}
      <code>{highlight(code, lang)}</code>
    </pre>
  );
}

// Inline: `code`, **bold**, *italic* / _italic_, ~~strike~~, [text](https://...).
const INLINE = /(`+)([\s\S]+?)\1|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|~~([\s\S]+?)~~|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;

function inline(text, key = "i") {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${m.index}`;
    if (m[2] !== undefined) out.push(<code key={k} className="md-inline">{m[2].trim()}</code>);
    else if (m[3] !== undefined || m[4] !== undefined) out.push(<strong key={k}>{inline(m[3] ?? m[4], k)}</strong>);
    else if (m[5] !== undefined) out.push(<del key={k}>{inline(m[5], k)}</del>);
    else if (m[6] !== undefined || m[7] !== undefined) out.push(<em key={k}>{inline(m[6] ?? m[7], k)}</em>);
    else out.push(<a key={k} href={m[9]} target="_blank" rel="noreferrer noopener">{inline(m[8], k)}</a>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Inline formatting only (code, bold, italic, links), for titles and one-line labels.
export function MarkdownInline({ text }) {
  return <>{inline(text || "", "x")}</>;
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

// Lists nest by indentation: deeper items become a child list of the item above.
function list(lines, start, key) {
  const indent = lines[start].match(LIST_ITEM)[1].length;
  const ordered = /\d/.test(lines[start].match(LIST_ITEM)[2]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    const m = lines[i].match(LIST_ITEM);
    if (m && m[1].length === indent && /\d/.test(m[2]) === ordered) {
      items.push({ text: [m[3]], children: [] });
      i += 1;
    } else if (m && m[1].length > indent && items.length) {
      const [child, next] = list(lines, i, `${key}-${i}`);
      items[items.length - 1].children.push(child);
      i = next;
    } else if (!m && lines[i].trim() && /^\s+/.test(lines[i]) && items.length) {
      items[items.length - 1].text.push(lines[i].trim()); // wrapped continuation
      i += 1;
    } else break;
  }
  const Tag = ordered ? "ol" : "ul";
  const first = ordered ? parseInt(lines[start].match(LIST_ITEM)[2], 10) : undefined;
  return [
    <Tag key={key} start={first !== 1 ? first : undefined}>
      {items.map((item, n) => <li key={n}>{inline(item.text.join(" "), `${key}-${n}`)}{item.children}</li>)}
    </Tag>,
    i,
  ];
}

const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export default function Markdown({ text }) {
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${i}`;
    const fence = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)/);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i += 1;
      blocks.push(<Code key={key} lang={fence[2].toLowerCase()} code={body.join("\n")} />);
    } else if (!line.trim()) {
      i += 1;
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length + 2, 6); // keep headings small in chat
      const H = `h${level}`;
      blocks.push(<H key={key} className="md-h">{inline(line.replace(/^#+\s*/, ""), key)}</H>);
      i += 1;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push(<hr key={key} />);
      i += 1;
    } else if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
      blocks.push(<blockquote key={key}><Markdown text={body.join("\n")} /></blockquote>);
    } else if (LIST_ITEM.test(line)) {
      const [el, next] = list(lines, i, key);
      blocks.push(el);
      i = next;
    } else if (line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || "")) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      blocks.push(
        <div key={key} className="md-table">
          <table>
            <thead><tr>{head.map((c, n) => <th key={n}>{inline(c, `${key}h${n}`)}</th>)}</tr></thead>
            <tbody>{rows.map((r, n) => <tr key={n}>{r.map((c, m) => <td key={m}>{inline(c, `${key}${n}-${m}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
    } else {
      const body = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>)/.test(lines[i])
             && !LIST_ITEM.test(lines[i])) body.push(lines[i++].trim());
      blocks.push(<p key={key}>{inline(body.join(" "), key)}</p>);
    }
  }
  return <div className="md">{blocks}</div>;
}
