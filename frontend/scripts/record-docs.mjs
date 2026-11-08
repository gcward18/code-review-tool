// Records the Help page videos (and the README's knowledge video) with Playwright against the demo server
// (scripts/demo_server.py: sample repository, scripted model, nothing of yours is shown).
//
//   npm run build && node scripts/record-docs.mjs [--gitleaks /path/to/gitleaks]
//
// Pass --only name[,name] to record some of them.
// Writes src/docs/{review,agents,settings,knowledge}.webm and .jpg previews; rebuild afterwards to publish them.
import { spawn } from "node:child_process";
import { mkdtempSync, renameSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const out = resolve(here, "../src/docs");
const port = 8790;
const url = `http://127.0.0.1:${port}/`;
const size = { width: 1280, height: 800 };
const gitleaks = process.argv.includes("--gitleaks") ? process.argv[process.argv.indexOf("--gitleaks") + 1] : undefined;
const python = process.env.AUTOSDE_PYTHON || join(root, ".venv/bin/python");

function startDemo() {
  const args = [join(root, "scripts/demo_server.py"), "--port", String(port), ...(gitleaks ? ["--gitleaks", gitleaks] : [])];
  // A clean environment: recordings must not reveal keys or settings from your shell.
  const env = Object.fromEntries(Object.entries(process.env)
    .filter(([key]) => !/^(LANGSMITH|LANGCHAIN|OPENAI|AWS|AUTOSDE)_/.test(key)));
  const server = spawn(python, args, { stdio: ["ignore", "pipe", "inherit"], env });
  return new Promise((ok, fail) => {
    server.stdout.on("data", (chunk) => String(chunk).includes("listening") && ok(server));
    server.on("exit", (code) => fail(new Error(`demo server exited (${code})`)));
  });
}

const pause = (page, ms = 900) => page.waitForTimeout(ms);

async function record(browser, name, script) {
  const dir = mkdtempSync(join(tmpdir(), "autosde-video-"));
  const context = await browser.newContext({ viewport: size, recordVideo: { dir, size }, colorScheme: "light" });
  // Recordings skip the first-visit tour and start from the light theme with default layout.
  await context.addInitScript(() => {
    localStorage.setItem("autosde-tour", "done");
    localStorage.setItem("autosde-theme", "light");
    localStorage.removeItem("autosde-layout");
  });
  const page = await context.newPage();
  await page.goto(url);
  await pause(page, 1200);
  await script(page);
  // The video's key moment, used as its preview image on the Help page.
  await page.screenshot({ path: join(out, `${name}.jpg`), type: "jpeg", quality: 80 });
  await pause(page, 1500);
  const video = page.video();
  await context.close();
  renameSync(await video.path(), join(out, `${name}.webm`));
  rmSync(dir, { recursive: true, force: true });
  console.log(`recorded ${name}.webm`);
}

async function review(page) {
  await page.locator("aside").getByText("shop", { exact: false }).first().click();
  await pause(page, 1500);
  await page.getByRole("button", { name: "Plan review" }).click();
  await page.getByText("Approve the plan").waitFor({ timeout: 30_000 });
  await pause(page, 2500);  // the plan: files, flags and the cost estimate
  await page.getByRole("button", { name: "Approve and review" }).click();
  await page.getByText("Total is overwritten on every item").first().waitFor({ timeout: 30_000 });
  await pause(page, 2500);  // findings with the diff fix
  await page.locator(".file-head", { hasText: "calculator.py" }).click();
  await pause(page, 800);
  await page.locator(".changed-files").scrollIntoViewIfNeeded();
  await page.mouse.wheel(0, 400);
  await pause(page, 2500);
}

async function agentsPage(page) {
  await page.getByRole("link", { name: "Agents" }).click();
  await pause(page, 1500);
  const agent = page.locator(".panel", { hasText: "Agent" }).first();
  await agent.locator("select").first().selectOption("javascript");
  await pause(page);
  await agent.locator("select").first().selectOption("python");
  await pause(page);
  const focus = agent.locator("textarea").first();
  await focus.click();
  await focus.press("End");
  await focus.pressSequentially(" Watch for loops that overwrite totals.", { delay: 35 });
  await pause(page);
  const prompts = page.locator(".prompt-panel");
  await prompts.locator("select").selectOption("aggregator");
  await pause(page, 1500);
  await page.getByRole("button", { name: "Save" }).click();
  await pause(page, 2000);
}

async function settingsPage(page) {
  await page.getByRole("link", { name: "Settings" }).click();
  await pause(page, 1200);
  const theme = page.locator(".setting select").first();
  for (const value of ["dark", "dracula", "light"]) {
    await theme.selectOption(value);
    await pause(page, 1300);
  }
  await page.getByText("Show agent details").click();
  await pause(page);
  await page.getByRole("link", { name: "Review Crew" }).click();
  await pause(page, 2000);  // the Agents column now shows on Home
  await page.getByRole("button", { name: "Toggle projects menu" }).click();
  await pause(page, 1500);
  await page.getByRole("button", { name: "Toggle projects menu" }).click();
  await pause(page);
  const gutter = page.locator(".workspace > .gutter-x");
  const box = await gutter.boundingBox();
  if (box) {
    await page.mouse.move(box.x + 4, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x - 180, box.y + box.height / 2, { steps: 20 });
    await page.mouse.up();
    await pause(page);
  }
  await page.getByRole("button", { name: "Full screen" }).first().click();
  await pause(page, 1800);
  await page.keyboard.press("Escape");
  await pause(page);
  await page.getByRole("link", { name: "Settings" }).click();
  await page.getByText("Show agent details").click();  // leave it as it was
}

const DOC = `# Error handling

Never swallow exceptions. Catch the narrowest exception type and log the error class, not the message.

## Retries

Retry network calls at most once, and only when the operation is idempotent.

## Totals

Accumulate totals with +=; never reassign the running total inside a loop.
`;

async function knowledgePage(page) {
  await page.getByRole("link", { name: "Knowledge" }).click();
  await pause(page, 1200);
  await page.getByRole("button", { name: "+ New knowledge base" }).click();
  await page.getByPlaceholder("team-conventions").pressSequentially("team-conventions", { delay: 35 });
  await page.getByPlaceholder("Coding standards and review rules").fill("Coding standards and review rules");
  await page.getByRole("button", { name: "Create knowledge base" }).click();
  await pause(page, 1200);
  await page.getByRole("button", { name: "✎ Paste text" }).click();
  await page.getByPlaceholder("error-handling-guidelines.md").fill("error-handling.md");
  await page.getByPlaceholder(/# Error handling/).fill(DOC);
  await pause(page);
  await page.getByRole("button", { name: "Add document" }).click();
  await page.getByText("ready", { exact: false }).first().waitFor({ timeout: 30_000 });
  await pause(page, 1500);
  await page.getByRole("button", { name: /error-handling\.md/ }).click();
  await pause(page, 2000);  // the document's chunks
  await page.getByRole("tab", { name: "Retrieval" }).click();
  await page.getByPlaceholder("Ask what an agent would look up…").pressSequentially("how should loops accumulate totals?", { delay: 35 });
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByText("retrieved").waitFor({ timeout: 30_000 });
  await page.getByRole("button", { name: "Expand all" }).click();
  await pause(page, 2500);  // ranked passages, fused from vector and BM25 search
}

const only = process.argv.includes("--only") ? process.argv[process.argv.indexOf("--only") + 1].split(",") : null;
const scenes = { review, agents: agentsPage, settings: settingsPage, knowledge: knowledgePage };
const server = await startDemo();
const browser = await chromium.launch();
try {
  for (const [name, script] of Object.entries(scenes)) {
    if (!only || only.includes(name)) await record(browser, name, script);
  }
} finally {
  await browser.close();
  server.kill("SIGINT");
}
