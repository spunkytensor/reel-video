// Copyright 2026 Spunky Tensor
// SPDX-License-Identifier: Apache-2.0

// Real browser + HTTP + SQLite + image upload. No response interception or fake inference.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { randomBytes } = require("node:crypto");
const { Script } = require("node:vm");
const { chromium } = require("playwright-core");

(async () => {
  const html = await fs.readFile("studio.html", "utf8");
  for (const [, script] of html.matchAll(
    /<script\b[^>]*>([\s\S]*?)<\/script>/gi,
  )) {
    new Script(script, { filename: "studio.html" });
  }
  const state = await fs.mkdtemp(path.join(os.tmpdir(), "reel-video-ci-"));
  const output = path.resolve("outputs/ci");
  await fs.mkdir(output, { recursive: true });
  const origin = "http://127.0.0.1:18088";
  const token = randomBytes(32).toString("hex");
  // This is the production server, explicitly without the CUDA worker.
  const server = spawn(
    process.env.PYTHON || "python",
    [
      "-c",
      [
        "import uvicorn",
        "from server import Settings, create_app",
        "uvicorn.run(create_app(Settings.from_env(), start_worker=False), host='127.0.0.1', port=18088)",
      ].join("; "),
    ],
    {
      env: {
        ...process.env,
        REEL_VIDEO_API_TOKEN: token,
        REEL_VIDEO_STATE_DIR: state,
        REEL_VIDEO_BASE_URL: origin,
        REEL_VIDEO_ALLOWED_ORIGINS: "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let logs = "";
  server.stdout.on("data", (data) => {
    logs += data;
  });
  server.stderr.on("data", (data) => {
    logs += data;
  });
  const stopped = once(server, "exit");
  let browser;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      if (server.exitCode !== null) throw new Error(`Server exited: ${logs}`);
      try {
        ready = (
          await fetch(`${origin}/health`, {
            headers: { Authorization: `Bearer ${token}` },
            signal: AbortSignal.timeout(500),
          })
        ).ok;
      } catch {}
      if (ready) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.ok(ready, `Real server did not become ready: ${logs}`);
    assert.equal((await fetch(`${origin}/api/v1/videos`)).status, 401);
    browser = await chromium.launch({
      executablePath: process.env.CHROMIUM_PATH || undefined,
      args: ["--no-sandbox"],
    });
    for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${origin}/#new`);
      await page.waitForFunction(
        () => !document.querySelector("#model").disabled,
      );
      assert.equal(
        (await page.locator(".brand").innerText()).trim(),
        "Reel Video",
      );
      assert.equal(await page.title(), "Create a video — Reel Video");
      const cookies = await page.context().cookies();
      assert.ok(
        cookies.some(
          (cookie) => cookie.name === "reel_video_session" && cookie.httpOnly && cookie.sameSite === "Strict",
        ),
      );
      assert.ok(!JSON.stringify(cookies).includes(token));
      await page
        .locator("#prompt")
        .fill(`CPU queue contract ${width}; no inference`);
      await page.locator("#customize summary").click();
      await page.locator("#inputMode").selectOption("references");
      const uploaded = page.waitForResponse(
        (response) =>
          response.url() === `${origin}/api/v1/videos/images` &&
          response.request().method() === "POST",
      );
      await page
        .getByLabel("Reference 1 image", { exact: true })
        .setInputFiles("assets/icon.png");
      assert.equal((await uploaded).status(), 201);
      await page.getByRole("img", { name: "Reference 1 preview" }).waitFor();
      await page
        .getByRole("button", { name: "Remove Reference 1", exact: true })
        .click();
      await page.locator("#inputMode").selectOption("text");
      await page.locator('[data-route="settings"]').click();
      await page.locator("#newVideoLink").click();
      assert.equal(
        await page.locator("#prompt").inputValue(),
        `CPU queue contract ${width}; no inference`,
      );
      const accepted = page.waitForResponse(
        (response) =>
          response.url() === `${origin}/api/v1/videos` &&
          response.request().method() === "POST",
      );
      await page.locator("#generateButton").click();
      const response = await accepted;
      assert.equal(response.status(), 202);
      const { id } = await response.json();
      await page.waitForFunction(() => sessionStorage.getItem("reel-video-pending") === null);
      // A real queued request, not a manufactured completion or pretend model output.
      const headers = { Authorization: `Bearer ${token}` };
      const queued = await fetch(`${origin}/api/v1/videos/${id}`, { headers });
      assert.equal((await queued.json()).status, "pending");
      await page.goto(`${origin}/#projects`);
      const checkbox = page.locator(".project-select").first();
      await checkbox.check();
      assert.equal(await page.locator("#projectCount").count(), 0);
      assert.ok(!(await page.locator("#projectsPage").innerText()).includes("saved on this computer"));
      page.once("dialog", (dialog) => dialog.dismiss());
      await page.locator("#bulkDeleteButton").click();
      assert.equal(
        (await fetch(`${origin}/api/v1/videos/${id}`, { headers })).status,
        200,
      );
      page.once("dialog", (dialog) => dialog.accept());
      const deleted = page.waitForResponse(
        (response) =>
          response.url().includes(encodeURIComponent(id)) &&
          response.request().method() === "DELETE",
      );
      await page.locator("#bulkDeleteButton").click();
      assert.ok((await deleted).ok());
      const tombstone = await (
        await fetch(`${origin}/api/v1/videos/${id}`, { headers })
      ).json();
      assert.equal(tombstone.local.deleted, true);
      assert.equal(tombstone.error.code, "job_deleted");
      assert.deepEqual(
        (await (await fetch(`${origin}/api/v1/videos`, { headers })).json())
          .data,
        [],
      );
      for (const theme of ["light", "dark"]) {
        assert.equal(await page.locator("#projectCount").count(), 0);
        await page.locator('[data-route="settings"]').click();
        await page.locator(`[data-theme-choice="${theme}"]`).click();
        assert.equal(await page.evaluate(() => localStorage.getItem("reel-video-theme")), theme);
        await page.goto(`${origin}/#projects`);
        assert.equal(await page.locator("html").getAttribute("data-theme"), theme);
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          true,
        );
        await page.screenshot({
          path: path.join(output, `browser-${width}-${theme}.png`),
        });
      }
      assert.deepEqual(errors, []);
      await page.close();
      console.log(
        `PASS ${width}: real session, discovery, upload, navigation, durable queue, confirm/cancel deletion; no model execution`,
      );
    }
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
    const force = setTimeout(() => server.kill("SIGKILL"), 5000);
    await stopped;
    clearTimeout(force);
    await fs.writeFile(
      path.join(output, "server.log"),
      logs.replaceAll(token, "[redacted]"),
    );
    await fs.rm(state, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
