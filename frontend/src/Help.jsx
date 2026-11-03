import reviewVideo from "./docs/review.webm";
import reviewPoster from "./docs/review.jpg";
import agentsVideo from "./docs/agents.webm";
import agentsPoster from "./docs/agents.jpg";
import settingsVideo from "./docs/settings.webm";
import settingsPoster from "./docs/settings.jpg";

const VIDEOS = [
  {
    id: "video-review", title: "Run a review", src: reviewVideo, poster: reviewPoster,
    steps: ["Pick a project in the sidebar", "Choose From (newer) and To (older)", "Plan the review: only file paths are sent",
      "Untick files you don't want reviewed, check the estimate, approve", "Read findings with diff fixes, then the changed files"],
  },
  {
    id: "video-agents", title: "Configure your agents", src: agentsVideo, poster: agentsPoster,
    steps: ["Pick an agent from the dropdown", "Turn specialists on or off and set their file extensions",
      "Choose a model per agent", "Edit a focus or a system prompt", "Save; the next review uses it"],
  },
  {
    id: "video-settings", title: "Settings and layout", src: settingsVideo, poster: settingsPoster,
    steps: ["Switch between Light, Dark and Dracula", "Show or hide agent details everywhere",
      "Show blocked folders", "Collapse the sidebar, resize panels, open one full screen"],
  },
];

const FAQ = [
  {
    section: "Reviews",
    items: [
      ["What does a review look at?",
        "Only committed changes between two points in history: every commit after **To** up to and including **From**. Uncommitted edits are ignored."],
      ["Does anything get sent before I approve?",
        "Only file paths and sizes go to the coordinator, which plans who reviews what. Code is sent to the specialists only after you approve the plan, and only for the files you leave ticked.", "video-review"],
      ["Why are some files unticked?",
        "Lockfiles, build output, IDE settings, minified bundles and similar files rarely contain defects but cost tokens, so they're flagged. Tick them if you want them reviewed anyway."],
      ["How do I read a finding's fix?",
        "Fixes are diffs: lines starting with `-` are removed and `+` are added. **Changed files** below the findings shows each file's full diff with five lines of context; expand the folds for more."],
      ["Can I review many commits at once?",
        "Yes: the **History batch** tab reviews every commit, merge commits only, every Nth commit or the latest N, with one approval and a cost cap."],
    ],
  },
  {
    section: "Agents",
    items: [
      ["Which agents review my code?",
        "A coordinator assigns files to language specialists (Python, JavaScript, TypeScript, Java, Go, C# and a general catch-all), and an aggregator removes duplicates and weak findings.", "video-agents"],
      ["Can I use different models per agent?",
        "Yes. On the Agents page each agent has a model dropdown; **Default** uses the server's configured model."],
      ["How do I give agents my team's rules?",
        "Create a knowledge base on the **Knowledge** page, upload your conventions, then tick it for the agents that should use it. Relevant passages are added to their prompts."],
    ],
  },
  {
    section: "Settings and privacy",
    items: [
      ["Where are my preferences stored?",
        "Theme, agent-detail visibility and layout are remembered in this browser.", "video-settings"],
      ["Is my code stored?",
        "No. Reviews keep findings, summaries and costs; the code itself is read from git or GitHub when needed and never saved. Every outgoing request is scanned for secrets first."],
      ["How do I see this tour again?",
        "Use **Show the tour again** at the top of this page."],
    ],
  },
];

// Inline `code` and **bold** only; FAQ text is ours, not user content.
function Rich({ text }) {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/).map((part, i) =>
    part.startsWith("`") ? <code key={i}>{part.slice(1, -1)}</code>
      : part.startsWith("**") ? <b key={i}>{part.slice(2, -2)}</b> : part);
}

export default function Help({ onTour }) {
  return (
    <div className="help">
      <div className="card help-hero">
        <div className="actions title">
          <h2 className="grow">Help</h2>
          <button className="secondary" onClick={onTour}>Show the tour again</button>
        </div>
        <p className="muted">Short videos of Review Crew in use, then answers to common questions.</p>
      </div>
      <h3 className="kb-h">Watch how it works</h3>
      <div className="video-grid">
        {VIDEOS.map((v) => (
          <figure key={v.id} id={v.id} className="card video-card">
            <video src={v.src} poster={v.poster} controls muted playsInline preload="metadata" aria-label={v.title} />
            <figcaption>
              <b>{v.title}</b>
              <ol className="small">{v.steps.map((s) => <li key={s}>{s}</li>)}</ol>
            </figcaption>
          </figure>
        ))}
      </div>
      <h3 className="kb-h">Frequently asked questions</h3>
      {FAQ.map((group) => (
        <section key={group.section} className="card faq">
          <h3>{group.section}</h3>
          {group.items.map(([q, a, video]) => (
            <details key={q}>
              <summary>{q}</summary>
              <p><Rich text={a} />{video && <> <a href={`#${video}`} onClick={(e) => {
                e.preventDefault();
                document.getElementById(video)?.scrollIntoView({ behavior: "smooth", block: "center" });
              }}>Watch the video</a></>}</p>
            </details>
          ))}
        </section>
      ))}
    </div>
  );
}
