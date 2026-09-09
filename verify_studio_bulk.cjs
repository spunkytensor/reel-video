// Copyright 2026 Spunky Tensor
// SPDX-License-Identifier: Apache-2.0

const { chromium } = require("playwright-core");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const output = "outputs/studio-verification";
fs.mkdirSync(output, { recursive: true });
const html = fs.readFileSync("studio.html", "utf8");
const jobs = [
  { id: "ready-1", status: "completed", local: { request: { prompt: "Amber coast", size: "960x544" } } },
  { id: "active-2", status: "in_progress", local: { request: { prompt: "Running river", size: "960x544" } } },
  { id: "ready-3", status: "completed", local: { request: { prompt: "Quiet forest", size: "544x960" } } },
];

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: "dark" });
  const errors = [];
  const deletes = [];
  let concurrent = 0;
  let maxConcurrent = 0;
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://127.0.0.1:8088/", route => route.fulfill({ contentType: "text/html", body: html }));
  await page.route("**/assets/studio-tokens.css", route => route.fulfill({ contentType: "text/css", body: fs.readFileSync("assets/studio-tokens.css", "utf8") }));
  await page.route("**/session", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/videos/models", route => route.fulfill({ json: { data: [] } }));
  await page.route("**/api/v1/videos", route => route.fulfill({ json: { data: jobs } }));
  await page.route(/\/api\/v1\/videos\/(?:ready-1|active-2|ready-3|mobile-4)$/, async route => {
    if (route.request().method() !== "DELETE") return route.fulfill({ status: 404 });
    const id = decodeURIComponent(route.request().url().split("/").pop());
    deletes.push(id); concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
    await new Promise(resolve => setTimeout(resolve, 30));
    concurrent--;
    if (id === "active-2") return route.fulfill({ status: 409, json: { error: { message: "Active jobs cannot be deleted" } } });
    const index = jobs.findIndex(job => job.id === id);
    if (index >= 0) jobs.splice(index, 1);
    return route.fulfill({ status: 204 });
  });

  await page.goto("http://127.0.0.1:8088/");
  await page.locator(".project-card").first().waitFor();
  assert.equal(await page.locator("a.project input").count(), 0, "checkboxes must not be nested in tile anchors");
  assert.equal(await page.locator(".project[data-job-id]").count(), 3, "project rendering contract remains intact");
  await page.locator('.project-select[data-job-id="ready-1"]').check();
  assert.equal(await page.locator("#bulkDeleteButton").isVisible(), true);
  page.once("dialog", dialog => dialog.dismiss());
  await page.locator("#bulkDeleteButton").click();
  assert.deepEqual(deletes, [], "canceling confirmation must make no requests");

  await page.locator('.project-select[data-job-id="active-2"]').check();
  await page.screenshot({ path: `${output}/bulk-selected-desktop.png`, fullPage: true });
  let confirmations = 0;
  page.once("dialog", dialog => { confirmations++; dialog.accept(); });
  await page.locator("#bulkDeleteButton").click();
  await page.locator("#bulkDeleteButton").waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.querySelector("#bulkDeleteButton").disabled);
  assert.equal(confirmations, 1);
  assert.deepEqual(deletes, ["ready-1", "active-2"]);
  assert.equal(maxConcurrent, 1, "deletes must be sequential");
  assert.equal(await page.locator('.project[data-job-id="ready-1"]').count(), 0);
  assert.equal(await page.locator('.project-select[data-job-id="active-2"]').isChecked(), true);
  assert.match(await page.locator("#projectNotice").innerText(), /1 of 2 videos deleted.*active-2.*Active jobs cannot be deleted/);
  await page.screenshot({ path: `${output}/bulk-partial-failure.png`, fullPage: true });

  await page.locator('a[data-route="settings"]').click();
  await page.locator("#settingsPage").waitFor();
  assert.equal(await page.locator("#bulkDeleteButton").isHidden(), true, "selection clears when leaving projects");
  await page.locator('a[data-route="projects"]').click();
  await page.locator("#projectsPage").waitFor();
  await page.locator('.project-select[data-job-id="ready-3"]').check();
  jobs.splice(jobs.findIndex(job => job.id === "ready-3"), 1);
  await page.locator("#refreshButton").click();
  await page.waitForFunction(() => !document.querySelector("#refreshButton").disabled);
  assert.equal(await page.locator("#bulkDeleteButton").isHidden(), true, "refresh prunes missing selections");

  await page.setViewportSize({ width: 390, height: 844 });
  jobs.push({ id: "mobile-4", status: "completed", local: { request: { prompt: "Mobile tile", size: "960x544" } } });
  await page.locator("#refreshButton").click();
  await page.locator('.project-select[data-job-id="mobile-4"]').check();
  const header = await page.locator("header").boundingBox();
  const button = await page.locator("#bulkDeleteButton").boundingBox();
  assert.ok(button.x >= header.x && button.x + button.width <= header.x + header.width, "mobile delete button fits header");
  await page.screenshot({ path: `${output}/bulk-selected-mobile.png`, fullPage: true });
  assert.deepEqual(errors, []);
  await browser.close();
  console.log("Studio bulk selection/deletion verification passed");
})().catch(error => { console.error(error); process.exit(1); });
