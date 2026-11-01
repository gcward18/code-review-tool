// Fixed-size panel layout: two columns of two panels each, filling the window.
// Drag the gutters to resize; ⤢ on a panel makes it full screen (Esc to leave).
import { useEffect, useRef, useState } from "react";

const KEY = "autosde-layout";
const clamp = (n) => Math.min(0.85, Math.max(0.15, n));

function useLayout() {
  const [layout, setLayout] = useState(() => {
    try {
      return { col: 0.5, left: 0.4, right: 0.6, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
    } catch {
      return { col: 0.5, left: 0.4, right: 0.6 };
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(layout));
    } catch {
      /* private mode */
    }
  }, [layout]);
  return [layout, (change) => setLayout((l) => ({ ...l, ...change }))];
}

// A drag handle; reports the pointer's position as a 0..1 fraction of `container`.
function Gutter({ axis, container, onMove, onReset }) {
  const start = (e) => {
    e.preventDefault();
    const box = container.current.getBoundingClientRect();
    const move = (ev) => onMove(clamp(axis === "x"
      ? (ev.clientX - box.left) / box.width : (ev.clientY - box.top) / box.height));
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("dragging");
    };
    document.body.classList.add("dragging");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  return (
    <div className={`gutter gutter-${axis}`} onPointerDown={start} onDoubleClick={onReset}
      role="separator" aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      title="Drag to resize · double-click to reset" />
  );
}

export function Panel({ title, actions, children, className = "" }) {
  const [max, setMax] = useState(false);
  useEffect(() => {
    if (!max) return undefined;
    const key = (e) => e.key === "Escape" && setMax(false);
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [max]);
  return (
    <>
      {max && <div className="panel-scrim" onClick={() => setMax(false)} />}
      <section className={`panel ${max ? "max" : ""} ${className}`}>
        <header className="panel-head">
          <h3>{title}</h3>
          <span className="grow" />
          {actions}
          <button className="secondary icon" onClick={() => setMax(!max)}
            title={max ? "Exit full screen (Esc)" : "Full screen"}
            aria-label={max ? "Exit full screen" : "Full screen"}>{max ? "⤡" : "⤢"}</button>
        </header>
        <div className="panel-body">{children}</div>
      </section>
    </>
  );
}

function Stack({ ratio, onRatio, onReset, top, bottom }) {
  const box = useRef(null);
  if (!bottom) return <div className="stack"><div className="stack-bottom">{top}</div></div>;
  return (
    <div className="stack" ref={box}>
      <div className="stack-top" style={{ flexBasis: `${ratio * 100}%` }}>{top}</div>
      <Gutter axis="y" container={box} onMove={onRatio} onReset={onReset} />
      <div className="stack-bottom">{bottom}</div>
    </div>
  );
}

export default function Workspace({ topLeft, bottomLeft, topRight, bottomRight }) {
  const [layout, set] = useLayout();
  const box = useRef(null);
  return (
    <div className="workspace" ref={box}
      style={{ gridTemplateColumns: `minmax(0, ${layout.col}fr) 8px minmax(0, ${1 - layout.col}fr)` }}>
      <Stack ratio={layout.left} onRatio={(left) => set({ left })} onReset={() => set({ left: 0.4 })}
        top={topLeft} bottom={bottomLeft} />
      <Gutter axis="x" container={box} onMove={(col) => set({ col })} onReset={() => set({ col: 0.5 })} />
      <Stack ratio={layout.right} onRatio={(right) => set({ right })} onReset={() => set({ right: 0.6 })}
        top={topRight} bottom={bottomRight} />
    </div>
  );
}
