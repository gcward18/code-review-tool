import { useEffect, useLayoutEffect, useState } from "react";

export const TOUR_KEY = "autosde-tour";

export function tourDone() {
  try {
    return localStorage.getItem(TOUR_KEY) === "done";
  } catch {
    return false;  // private mode: show it, but dismissing still works for this visit
  }
}

function remember() {
  try {
    localStorage.setItem(TOUR_KEY, "done");
  } catch { /* private mode */ }
}

const STEPS = [
  { title: "Welcome to AutoSDE", text: "A team of AI agents reviews the commits you choose. This quick tour shows where things are. You can skip it at any time." },
  { target: "aside", title: "Your projects", text: "Repositories appear here: Recent ones you've reviewed and Available ones found on this machine. Search filters both." },
  { target: ".add-gh-btn", title: "GitHub repositories", text: "Add a GitHub repository by owner/name to review it straight from GitHub, without cloning." },
  { target: "[data-tour=agents]", title: "Agents", text: "Choose which specialists run, their models and prompts, and attach knowledge bases with your team's rules." },
  { target: "[data-tour=knowledge]", title: "Knowledge", text: "Upload docs such as conventions or runbooks; agents receive the most relevant passages while reviewing." },
  { target: "[data-tour=settings]", title: "Settings", text: "Theme, agent details and blocked folders. The ☰ button collapses the sidebar." },
  { target: "[data-tour=help]", title: "Help", text: "Videos of each page in use and answers to common questions. You can replay this tour from there." },
];

export default function Tour({ onClose }) {
  const [index, setIndex] = useState(0);
  const [box, setBox] = useState(null);
  const step = STEPS[index];
  const last = index === STEPS.length - 1;

  useLayoutEffect(() => {
    const measure = () => {
      const el = step.target && document.querySelector(step.target);
      const rect = el && el.getBoundingClientRect();
      setBox(rect && rect.width ? { top: rect.top, left: rect.left, width: rect.width, height: rect.height } : null);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [index]);

  const finish = () => (remember(), onClose());
  useEffect(() => {
    const key = (e) => {
      if (e.key === "Escape") finish();
      if (e.key === "ArrowRight" && !last) setIndex(index + 1);
      if (e.key === "ArrowLeft" && index) setIndex(index - 1);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [index]);

  // Place the card beside the highlighted element, or centred for the welcome step.
  const pad = 8;
  const card = box
    ? { top: Math.min(box.top + box.height + 14, window.innerHeight - 220),
        left: Math.max(16, Math.min(box.left, window.innerWidth - 376)) }
    : { top: "50%", left: "50%", transform: "translate(-50%, -50%)" };
  if (box && box.left > window.innerWidth / 2 && box.top < 80) card.left = Math.max(16, box.left + box.width - 360);
  if (box && box.height > window.innerHeight / 2) {
    card.top = Math.max(80, box.top + 80);
    card.left = Math.min(box.left + box.width + 16, window.innerWidth - 376);
  }

  return (
    <div className="tour" role="dialog" aria-modal="true" aria-labelledby="tour-title">
      {box
        ? <div className="tour-spot" style={{ top: box.top - pad, left: box.left - pad, width: box.width + pad * 2,
            height: box.height + pad * 2 }} />
        : <div className="tour-shade" />}
      <div className="tour-card" style={card}>
        <div className="small muted">{index + 1} of {STEPS.length}</div>
        <h3 id="tour-title">{step.title}</h3>
        <p>{step.text}</p>
        <div className="actions">
          <button className="secondary" onClick={finish}>Skip tour</button>
          <span className="grow" />
          {index > 0 && <button className="secondary" onClick={() => setIndex(index - 1)}>Back</button>}
          <button onClick={() => (last ? finish() : setIndex(index + 1))}>{last ? "Done" : "Next"}</button>
        </div>
      </div>
    </div>
  );
}
