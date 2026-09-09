// Copyright 2026 Spunky Tensor
// SPDX-License-Identifier: Apache-2.0

// Run against an empty Studio with start_worker=False; intercept all generation work.
const { chromium } = require("playwright-core");
const assert = require("node:assert/strict");
const fs = require("node:fs");
fs.mkdirSync("outputs/studio-verification", { recursive: true });
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1080 },
      colorScheme: "light",
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const shot = async (name) => {
      await page.evaluate(() => window.scrollTo(0, 0));
      assert.equal(
        await page
          .locator("header")
          .evaluate((header) => header.getBoundingClientRect().height),
        page.viewportSize().width <= 700 ? 168 : 90,
      );
      assert.equal(
        await page
          .locator("header .mark")
          .evaluate((icon) => icon.getBoundingClientRect().width),
        65.25,
      );
      await page.screenshot({
        path: `outputs/studio-verification/${name}.png`,
        fullPage: true,
      });
    };
    await page.goto(process.env.REEL_VIDEO_BASE_URL || "http://127.0.0.1:8088");
    await page
      .locator('#model option[value="local/minimax-h3"]')
      .waitFor({ state: "attached" });
    await page
      .getByText("Your next video starts here", { exact: true })
      .waitFor();
    assert.equal(await page.locator("#projectsPage").isVisible(), true);
    assert.equal(await page.locator("#generationForm").isVisible(), false);
    assert.deepEqual(await page.locator("header nav a").allTextContents(), [
      "Videos",
      "Settings",
    ]);
    assert.equal(await page.locator("input[type=password]").count(), 0);
    assert.equal(
      await page.evaluate(() => document.cookie.includes("reel_video_session")),
      false,
    );
    await page.locator(".corner-logo").evaluate((img) => img.decode());
    await shot("projects-empty");

    await page.locator("#newVideoLink").click();
    await page.locator("#generationForm").waitFor();
    await page.locator("#prompt").fill("A quiet coastal train at blue hour");
    await page.locator("#customize summary").click();
    assert.equal(await page.locator("#steps").inputValue(), "25");
    for (const id of [
      "model",
      "inputMode",
      "steps",
      "duration",
      "size",
      "exportType",
      "seed",
      "audio",
    ]) {
      assert.equal(await page.locator(`#${id}`).isVisible(), true, id);
    }
    await page.locator("#duration").selectOption("10");
    await page.locator("#size").selectOption("544x960");
    await page.locator("#exportType").selectOption("1080p");
    await page.locator("#audio").uncheck();
    await shot("new-options");
    await page.locator('header nav a[href="#settings"]').click();
    await page.locator("#settingsPage").waitFor();
    assert.equal(await page.locator("#generationForm").isVisible(), false);
    await page.locator('[data-theme-choice="dark"]').click();
    assert.equal(
      await page.evaluate(() => document.documentElement.dataset.theme),
      "dark",
    );
    await shot("settings");
    await page.locator('[data-theme-choice="system"]').click();
    await page.emulateMedia({ colorScheme: "dark" });
    await page.waitForFunction(
      () => document.documentElement.dataset.theme === "dark",
    );
    await page.emulateMedia({ colorScheme: "light" });
    await page.waitForFunction(
      () => document.documentElement.dataset.theme === "light",
    );
    await page.goBack();
    await page.locator("#generationForm").waitFor();
    assert.equal(
      await page.locator("#prompt").inputValue(),
      "A quiet coastal train at blue hour",
    );
    assert.equal(await page.locator("#size").inputValue(), "544x960");
    await page.goForward();
    await page.locator("#settingsPage").waitFor();
    await page.locator("#newVideoLink").click();

    let submitted;
    let fixtureJobs = [];
    await page.route("**/api/v1/videos", async (route) => {
      if (route.request().method() === "GET")
        return route.fulfill({ json: { data: fixtureJobs } });
      submitted = route.request().postDataJSON();
      const accepted = {
        id: "video-preview",
        status: "in_progress",
        local: {
          phase: "denoising",
          created_at: Date.now() / 1000,
          started_at: Date.now() / 1000,
          request: submitted,
          progress: { unit: "denoising_steps", completed: 3, total: 24 },
        },
      };
      fixtureJobs = [accepted];
      return route.fulfill({ status: 202, json: accepted });
    });
    await page.locator("#generateButton").click();
    await page.locator("#activityDialog").waitFor();
    await page.waitForFunction(
      () =>
        sessionStorage.getItem("reel-video-pending") === null &&
        !document.querySelector("#generateButton").disabled,
    );
    assert.equal(
      submitted.provider.options.local.parameters.num_inference_steps,
      25,
    );
    assert.equal(submitted.size, "544x960");
    assert.equal(submitted.local.export, "1080p");
    assert.equal(submitted.generate_audio, false);
    assert.equal(await page.locator("#submitNotice").innerText(), "");
    assert.equal(await page.locator("#latestJob, #modelNote").count(), 0);
    assert.match(await page.locator("#activityJobs").innerText(), /Generating/);
    await shot("activity");
    await page.keyboard.press("Escape");
    await page.locator("#activityDialog").waitFor({ state: "hidden" });
    assert.equal(
      await page.locator("#activityButton").getAttribute("aria-expanded"),
      "false",
    );
    await page.locator("#activityButton").click();
    await page.locator(".open-project").click();
    await page.locator("#videoPage").waitFor();
    await page.locator("#videoDetail .job").waitFor();
    assert.equal(await page.locator("#jobs .project").count(), 1);
    assert.equal(
      await page
        .locator("#jobs .facts, #jobs .job-actions, #jobs video[controls]")
        .count(),
      0,
    );
    const infoBox = await page.locator("#videoDetail .job-copy").boundingBox();
    const previewBox = await page
      .locator("#videoDetail .preview")
      .boundingBox();
    assert.ok(
      infoBox.x + infoBox.width <= previewBox.x,
      "Information must be left of playback",
    );
    await page.locator('#videoPage a[href="#projects"]').click();
    await shot("projects-active");
    // A tiny CPU-generated fixture verifies library playback and download.
    const fixturePath = "outputs/studio-verification/preview-fixture.mp4";
    require("node:child_process").execFileSync("ffmpeg", [
      "-nostdin",
      "-v",
      "error",
      "-y",
      "-f",
      "lavfi",
      "-i",
      "testsrc2=size=108x192:rate=24",
      "-t",
      "1",
      "-c:v",
      "libx264",
      "-pix_fmt",
      "yuv420p",
      fixturePath,
    ]);
    await page.route("**/api/v1/videos/video-completed/content", (route) =>
      route.fulfill({
        contentType: "video/mp4",
        body: fs.readFileSync(fixturePath),
      }),
    );
    fixtureJobs.push({
      id: "video-completed",
      status: "completed",
      unsigned_urls: "/api/v1/videos/video-completed/content",
      local: {
        phase: "complete",
        request: { ...submitted, prompt: "A completed video" },
      },
    });
    await page.locator("#refreshButton").click();
    await page.waitForFunction(
      () => document.querySelector("#jobs video")?.readyState >= 2,
    );
    await shot("projects-videos");
    await page.locator('#jobs a[href="#video/video-completed"]').click();
    await page.locator("#videoPage").waitFor();
    await page.waitForFunction(
      () => document.querySelector("#videoDetail video")?.readyState >= 1,
    );
    await page.locator("#videoDetail video").evaluate(async (video) => {
      video.muted = true;
      await video.play();
      video.pause();
      video.dataset.preserved = "yes";
    });
    fixtureJobs[1].local.metadata = { seed: 42 };
    await page.locator("#refreshVideo").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#videoDetail .facts")
        .textContent.includes("Seed 42"),
    );
    assert.equal(
      await page.locator("#videoDetail video").getAttribute("data-preserved"),
      "yes",
    );
    assert.equal(
      new URL(
        await page.locator("#videoDetail a[download]").getAttribute("href"),
      ).pathname,
      "/api/v1/videos/video-completed/content",
    );
    const playerBox = await page.locator("#videoDetail video").boundingBox();
    const stageBox = await page.locator("#videoDetail .preview").boundingBox();
    assert.ok(
      playerBox.height <= stageBox.height + 1,
      "Portrait playback must fit the stage without clipping",
    );
    await shot("video-detail");
    await page.reload();
    await page.locator("#videoDetail video").waitFor();
    assert.equal(
      await page.locator("#videoTitle").innerText(),
      "A completed video",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await shot("video-detail-mobile");
    await page.setViewportSize({ width: 1440, height: 1080 });
    await page.locator('#videoPage a[href="#projects"]').click();
    await page.locator("#projectsPage").waitFor();
    await page.locator('#jobs a[href="#video/video-preview"]').press("Enter");
    await page.locator("#videoPage").waitFor();
    await page.locator("#videoDetail .reuse").click();
    await page.locator("#generationForm").waitFor();
    assert.equal(await page.locator("#prompt").inputValue(), submitted.prompt);
    assert.equal(await page.locator("#duration").inputValue(), "10");
    assert.equal(await page.locator("#customize").getAttribute("open"), "");

    // Activity cancellation hits the existing endpoint; no real work is submitted.
    await page.route("**/api/v1/videos/video-preview/cancel", async (route) => {
      fixtureJobs[0] = {
        ...fixtureJobs[0],
        status: "failed",
        error: { message: "Canceled" },
        local: { ...fixtureJobs[0].local, phase: "canceled" },
      };
      await route.fulfill({ json: fixtureJobs[0] });
    });
    page.on("dialog", (dialog) => dialog.accept());
    await page.locator("#activityButton").click();
    await page.locator("#activityJobs .cancel-job").click();
    await page.locator("#activityJobs .status.failed").waitFor();
    await page.locator("#closeActivity").click();

    fixtureJobs[1].local.content_expired = true;
    await page.evaluate(() => {
      location.hash = "video/video-completed";
    });
    await page.locator("#videoPage").waitFor();
    await page.locator("#refreshVideo").click();
    await page.getByText("Content expired", { exact: true }).waitFor();
    assert.equal(
      await page
        .locator("#videoDetail video, #videoDetail a[download]")
        .count(),
      0,
    );
    await page.route("**/api/v1/videos/video-completed", async (route) => {
      if (route.request().method() === "DELETE") {
        fixtureJobs = fixtureJobs.filter((job) => job.id !== "video-completed");
        return route.fulfill({ status: 204 });
      }
      return route.fulfill({
        status: 404,
        json: { error: { message: "Not found" } },
      });
    });
    await page.locator("#videoDetail .delete-job").click();
    await page.locator("#projectsPage").waitFor();
    await page.waitForFunction(
      () => document.querySelectorAll("#jobs .project").length === 1,
    );
    await page.evaluate(() => {
      location.hash = "video/video-completed";
    });
    await page
      .getByText("This video is no longer available.", { exact: true })
      .waitFor();

    await page.locator('header nav a[href="#settings"]').click();
    await page.locator('[data-theme-choice="dark"]').click();
    await page.reload();
    await page.locator("#settingsPage").waitFor();
    assert.equal(
      await page.evaluate(() => document.documentElement.dataset.theme),
      "dark",
    );
    assert.equal(
      await page
        .locator('[data-theme-choice="dark"]')
        .getAttribute("aria-pressed"),
      "true",
    );
    await page.locator('header nav a[href="#projects"]').click();
    await page.waitForFunction(
      () =>
        document.querySelector("#pollState").textContent === "Updated just now",
    );
    // Return to the real API to test automatic session renewal.
    await page.unroute("**/api/v1/videos");
    await page.context().clearCookies();
    const renewed = page.waitForResponse(
      (r) => r.url().endsWith("/session") && r.status() === 200,
    );
    await page.locator("#refreshButton").click();
    await renewed;
    await page
      .getByText("Your next video starts here", { exact: true })
      .waitFor();

    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      for (const route of ["projects", "settings", "new"]) {
        await page.evaluate((route) => {
          location.hash = route;
        }, route);
        await page.locator(`#${route}Page`).waitFor();
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          true,
          `${route} at ${width}`,
        );
        if (width === 390) await shot(`${route}-mobile`);
      }
    }
    assert.deepEqual(errors, []);
    console.log(
      "PASS: navigation, draft preservation, all creation options, System/Light/Dark settings, intercepted submission, tiled Projects, left/right video details, deep links, expired/deleted videos, Activity, cancellation, reuse, session renewal, theme persistence, desktop/mobile layout.",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
