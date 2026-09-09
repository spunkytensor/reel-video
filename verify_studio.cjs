// Copyright 2026 Spunky Tensor
// SPDX-License-Identifier: Apache-2.0

// Opt-in browser checks. Uses real authentication and discovery, fixture job states.
// Does NOT submit real GPU work. Requires Playwright and a completed API job.
const { chromium } = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const base = process.env.REEL_VIDEO_BASE_URL || 'http://127.0.0.1:8088';
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || undefined,
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  fs.mkdirSync('outputs/studio-verification', { recursive: true });
  const shot = name => page.screenshot({ path: `outputs/studio-verification/${name}.png`, fullPage: true });
  try {
    await page.goto(`${base}/#new`);
    await page.locator('#model option[value="local/minimax-h3"]').waitFor({ state: 'attached' });
    assert.equal(await page.locator('input[type="password"]').count(), 0);
    await page.locator('#customize summary').click();
    assert.equal(await page.evaluate(() => document.cookie.includes('reel_video_session')), false);
    assert.equal(await page.locator('#duration option').count(), 2);
    assert.equal(await page.locator('#size option').count(), 3);
    assert.equal(await page.locator('#audio').isDisabled(), false);
    await page.locator('#conditioningControls').waitFor({ state: 'visible' });
    assert.deepEqual(await page.locator('#inputMode option').allTextContents(), ['Text only', 'First / last frames', 'General references']);
    assert.equal(await page.locator('#steps').inputValue(), '25');
    assert.match(await page.locator('#stepsHint').innerText(), /24 model evaluations/);
    await page.locator('#duration').selectOption('10');
    await page.locator('#exportType').selectOption('1080p');
    await shot('live-desktop');

    // Upload real image bytes, but intercept generation requests so these checks never enqueue GPU work.
    const fox = 'outputs/reference-inputs/fox.png';
    const foxStart = 'outputs/reference-inputs/fox-start.png';
    assert.ok(fs.existsSync(fox) && fs.existsSync(foxStart), 'Reference input fixtures are required.');
    const captured = [];
    const captureGeneration = async route => {
      if (route.request().method() !== 'POST') return route.fallback();
      captured.push(route.request().postDataJSON());
      await route.fulfill({ status: 202, json: { id: `video-ui-${captured.length}`, status: 'pending', local: { phase: 'queued' } } });
    };
    await page.route('**/api/v1/videos', captureGeneration);
    await page.locator('#prompt').fill('Browser payload contract check');
    for (const invalid of ['', '1', '101', '2.5']) {
      await page.locator('#steps').fill(invalid);
      assert.equal(await page.locator('#steps').evaluate(input => input.checkValidity()), false);
      await page.locator('#generateButton').click();
      assert.equal(captured.length, 0, 'Invalid step count must not submit a job');
    }
    await page.locator('#steps').fill('50');
    await page.locator('#audio').uncheck();
    await page.locator('.composer').screenshot({ path: 'outputs/studio-verification/silent-delivery-desktop.png' });
    await page.locator('#generateButton').click();
    await page.waitForFunction(() => sessionStorage.getItem('reel-video-pending') === null && !document.querySelector('#generateButton').disabled);
    await page.locator('#closeActivity').click();
    assert.equal('provider' in captured[0], true);
    assert.equal('frame_images' in captured[0], false);
    assert.equal('input_references' in captured[0], false);
    assert.equal(captured[0].generate_audio, false);
    await page.locator('#audio').check();
    await page.locator('#inputMode').selectOption('frames');
    assert.equal(await page.locator('#generateButton').isDisabled(), true);
    assert.equal(await page.locator('input[data-slot="first"]').evaluate(input => input.tabIndex), -1);
    const chooserEvent = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Upload First frame', exact: true }).press('Enter');
    await (await chooserEvent).setFiles(foxStart);
    await page.locator('[data-slot="first"] img').waitFor();
    await page.locator('input[data-slot="last"]').setInputFiles(fox);
    await page.locator('[data-slot="last"] img').waitFor();
    await page.locator('#steps').fill('12');
    assert.match(await page.locator('#stepsHint').innerText(), /smoke tests.*poor quality/);
    await shot('conditioning-frames');
    await page.locator('#conditioningControls').screenshot({ path: 'outputs/studio-verification/frame-controls-desktop.png' });
    await page.locator('#generateButton').click();
    await page.waitForFunction(() => sessionStorage.getItem('reel-video-pending') === null && !document.querySelector('#generateButton').disabled);
    await page.locator('#closeActivity').click();
    assert.equal(captured[1].provider.options.local.parameters.num_inference_steps, 12);
    assert.deepEqual(captured[1].frame_images.map(v => v.frame_type), ['first_frame', 'last_frame']);
    assert.ok(captured[1].frame_images.every(v => /^http.*\/api\/v1\/videos\/images\/image-[a-f0-9]{64}$/.test(v.image_url.url)));
    assert.equal('input_references' in captured[1], false);
    await page.locator('[data-slot="last"] .remove-image').click();
    assert.equal(await page.locator('[data-slot="last"] img').count(), 0);
    await page.locator('input[data-slot="last"]').setInputFiles(fox);
    await page.locator('[data-slot="last"] img').waitFor();

    await page.locator('#inputMode').selectOption('references');
    assert.equal(await page.locator('[data-slot="first"]').count(), 0, 'Hidden frame images must be cleared.');
    await page.locator('input[data-slot="reference1"]').setInputFiles(fox);
    await page.locator('[data-slot="reference1"] img').waitFor();
    await page.locator('input[data-slot="reference2"]').setInputFiles(foxStart);
    await page.locator('[data-slot="reference2"] img').waitFor();
    await page.locator('#steps').fill('50');
    await shot('conditioning-references');
    await page.locator('#conditioningControls').screenshot({ path: 'outputs/studio-verification/reference-controls-desktop.png' });
    await page.locator('#generateButton').click();
    await page.waitForFunction(() => sessionStorage.getItem('reel-video-pending') === null && !document.querySelector('#generateButton').disabled);
    await page.locator('#closeActivity').click();
    assert.equal(captured[2].provider.options.local.parameters.num_inference_steps, 50);
    assert.equal('frame_images' in captured[2], false);
    assert.equal(captured[2].input_references.length, 2);
    assert.notEqual(captured[2].input_references[0].image_url.url, captured[2].input_references[1].image_url.url, 'Reference order must be preserved.');

    // Upload completion in one slot must not clear another slot's busy state.
    const uploads = [];
    let uploadsReady;
    const waitingForUploads = new Promise(resolve => { uploadsReady = resolve; });
    const holdUploads = route => { uploads.push(route); if (uploads.length === 2) uploadsReady(); };
    await page.route('**/api/v1/videos/images', holdUploads);
    await page.locator('#inputMode').selectOption('frames');
    await page.locator('input[data-slot="first"]').setInputFiles(fox);
    await page.locator('input[data-slot="last"]').setInputFiles(foxStart);
    await waitingForUploads;
    const uploadedUrl = captured[2].input_references[0].image_url.url;
    const uploadedResult = { id: uploadedUrl.split('/').pop(), url: uploadedUrl };
    await uploads[0].fulfill({ status: 201, json: uploadedResult });
    await page.locator('#imageSlots img').waitFor();
    assert.equal(await page.locator('#generateButton').isDisabled(), true);
    await page.locator('#generationForm').evaluate(form => form.dispatchEvent(new Event('submit', { cancelable: true })));
    assert.equal(captured.length, 3, 'Enter/programmatic submit cannot bypass a busy upload');
    await page.locator('#inputMode').selectOption('text');
    await uploads[1].fulfill({ status: 201, json: uploadedResult });
    await page.unroute('**/api/v1/videos/images', holdUploads);
    assert.equal(await page.locator('#imageSlots img').count(), 0, 'Late upload cannot restore hidden conditioning');

    // Invalid type and client-side oversize checks must not hit the upload endpoint.
    await page.locator('#inputMode').selectOption('frames');
    await page.locator('input[data-slot="first"]').setInputFiles({ name: 'bad.txt', mimeType: 'text/plain', buffer: Buffer.from('not an image') });
    await page.getByText(/static PNG, JPEG, or WebP/).waitFor();
    await page.locator('input[data-slot="first"]').setInputFiles({ name: 'huge.png', mimeType: 'image/png', buffer: Buffer.alloc(8388609) });
    await page.getByText(/exceeds the 8 MiB/).waitFor();
    await page.unroute('**/api/v1/videos', captureGeneration);

    const history = await (await context.request.get(`${base}/api/v1/videos`)).json();
    const realSample = history.data.find(item => item.status === 'completed' && item.unsigned_urls?.length);
    assert.ok(realSample, 'Run a real API generation before the browser media test.');

    // Test state presentation without fabricating provider execution records.
    const now = Date.now() / 1000;
    const expiredImage = `${base}/api/v1/videos/images/image-${'0'.repeat(64)}`;
    const request = { model: 'local/minimax-h3', prompt: 'A fox walks through a snowy pine forest. Gentle wind, no music.', size: '960x544', duration: 5, seed: 42, generate_audio: true, local: { export: 'original' }, provider: { options: { local: { parameters: { num_inference_steps: 50 } } } }, input_references: [{ type: 'image_url', image_url: { url: expiredImage } }, captured[2].input_references[1]] };
    const job = (id, status, overrides = {}) => ({
      id, status, polling_url: `${base}/api/v1/videos/${id}`,
      local: { request, created_at: now - 540, started_at: now - 500, phase: 'generating', ...overrides },
    });
    const completed = job('video-browser-completed', 'completed', {
      phase: 'ready', completed_at: now, metadata: {
        source_size: '960x544', delivery_size: '960x544', frames: 124, fps: 24,
        video_frame_duration: 124 / 24, container_duration: 5.175,
        seed: 42, ai_generated: true, scaling_method: 'none', fit_policy: 'original',
      },
    });
    completed.unsigned_urls = realSample.unsigned_urls;
    const failed = job('video-browser-failed', 'failed', { phase: 'failed', completed_at: now });
    failed.error = { code: 'out_of_memory', message: 'Free GPU resources and explicitly retry.' };
    const expired = job('video-browser-expired', 'completed', { phase: 'ready', completed_at: now, content_expired: true });
    const active = job('video-browser-active', 'in_progress', { phase: 'denoising', progress: { unit: 'denoising_steps', completed: 12, total: 49 } });
    const pending = job('video-browser-pending', 'pending', { phase: 'queued', started_at: null });
    let jobs = [completed, pending, active, failed, expired];
    let failHistory = false;
    await page.route('**/api/v1/videos', async route => {
      if (route.request().method() !== 'GET') return route.fallback();
      if (failHistory) return route.abort('failed');
      await route.fulfill({ json: { data: jobs } });
    });
    await page.locator('header nav a[href="#projects"]').click();
    await page.locator('#refreshButton').click();
    await page.locator('#jobs .project').first().waitFor();
    assert.equal(await page.locator('#jobs .project').count(), 5);
    assert.equal(await page.locator('#jobs .facts, #jobs .job-actions, #jobs video[controls]').count(), 0);
    const openVideo = async id => {
      await page.evaluate(id => { location.hash = `video/${id}`; }, id);
      await page.locator('#videoPage').waitFor();
      await page.locator(`#videoDetail #${id}`).waitFor();
    };
    await openVideo(pending.id);
    assert.match(await page.locator('#videoDetail .facts').innerText(), /50 sigma points/);
    await openVideo(active.id);
    assert.equal(await page.locator('#videoDetail progress').getAttribute('value'), '12');
    assert.equal(await page.locator('#videoDetail progress').getAttribute('max'), '49');
    active.local.progress.completed = 49;
    active.local.phase = 'encoding';
    await page.locator('#refreshVideo').click();
    await page.getByText('encoding · denoising complete, final video not ready yet').waitFor();
    await openVideo(failed.id);
    await page.getByText(/Free GPU resources/).waitFor();
    await openVideo(expired.id);
    assert.equal(await page.locator('#videoDetail video').count(), 0);
    await openVideo(completed.id);
    await page.waitForFunction(() => document.querySelector('#videoDetail video')?.readyState >= 1);
    await page.locator('#videoDetail video').evaluate(async video => { video.muted = true; await video.play(); video.currentTime = 2; video.pause(); video.dataset.preserved = 'yes'; });
    await page.locator('#refreshVideo').click();
    assert.equal(await page.locator('#videoDetail video').getAttribute('data-preserved'), 'yes');
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#videoDetail a[download]').click();
    assert.equal(await (await downloadEvent).failure(), null);
    await shot('detail-desktop');
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await shot('detail-mobile');

    await page.route(expiredImage, route => route.fulfill({ status: 410 }));
    await page.locator('#videoDetail .reuse').click();
    assert.equal(await page.locator('#seed').inputValue(), '42');
    assert.equal(await page.locator('#duration').inputValue(), '5');
    await page.getByText(/conditioning images expired or are unavailable/).waitFor();
    assert.equal(await page.locator('#inputMode').inputValue(), 'references');
    assert.equal(await page.locator('#imageSlots img').count(), 1, 'Surviving reference does not silently replace missing conditioning');
    assert.equal(await page.locator('#generateButton').isDisabled(), true);
    jobs = [];
    await page.locator('header nav a[href="#projects"]').click();
    await page.locator('#refreshButton').click();
    await page.getByText('Your next video starts here').waitFor();
    await shot('empty-mobile');
    failHistory = true;
    await page.locator('header nav a[href="#projects"]').click();
    await page.locator('#refreshButton').click();
    await page.waitForFunction(() => document.querySelector('#pollState').textContent.includes('Refresh failed'));
    await shot('disconnected-mobile');
    failHistory = false;

    await page.locator('#newVideoLink').click();
    // An uncertain POST is retained across reload and reconciled with the original key.
    await page.locator('input[data-slot="reference1"]').setInputFiles(fox);
    await page.locator('[data-slot="reference1"] img').waitFor();
    await page.locator('.composer').screenshot({ path: 'outputs/studio-verification/reference-controls-mobile.png' });
    const submissions = [];
    await page.route('**/api/v1/videos', async route => {
      if (route.request().method() !== 'POST') return route.fallback();
      submissions.push({ key: route.request().headers()['idempotency-key'], body: route.request().postDataJSON() });
      if (submissions.length === 1) return route.abort('failed');
      return route.fulfill({ status: 202, json: job('video-browser-reconciled', 'pending') });
    });
    await page.locator('#generateButton').click();
    await page.locator('#reconcileButton').waitFor();
    assert.equal(await page.locator('#generateButton').isDisabled(), true);

    // Transient composer notices must not strand a retained uncertain request.
    await page.locator('#inputMode').selectOption('text');
    await page.getByText(/Hidden mode images were cleared/).waitFor();
    await page.locator('#reconcileButton').waitFor();
    await page.locator('#inputMode').selectOption('frames');
    await page.locator('input[data-slot="first"]').setInputFiles({ name: 'bad.txt', mimeType: 'text/plain', buffer: Buffer.from('not an image') });
    await page.getByText(/static PNG, JPEG, or WebP/).waitFor();
    await page.locator('#reconcileButton').waitFor();
    jobs = [completed];
    await page.locator('header nav a[href="#projects"]').click();
    await page.locator('#refreshButton').click();
    await openVideo(completed.id);
    await page.locator('#videoDetail .reuse').click();
    await page.getByText(/conditioning images expired or are unavailable/).waitFor();
    await page.locator('#reconcileButton').waitFor();
    assert.equal(await page.locator('#generateButton').isDisabled(), true);
    await page.locator('.composer').screenshot({ path: 'outputs/studio-verification/reconciliation-mobile.png' });

    await page.reload();
    await page.locator('#model option[value="local/minimax-h3"]').waitFor({ state: 'attached' });
    await page.locator('#reconcileButton').waitFor();
    await page.locator('#reconcileButton').click();
    await page.waitForFunction(() => sessionStorage.getItem('reel-video-pending') === null && !document.querySelector('#generateButton').disabled);
    await page.locator('#closeActivity').click();
    assert.equal(submissions.length, 2);
    assert.deepEqual(submissions[0], submissions[1]);
    assert.match(submissions[0].body.input_references[0].image_url.url, /\/api\/v1\/videos\/images\/image-[a-f0-9]{64}$/);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('reel-video-pending')), null);
    assert.equal(await page.evaluate(() => localStorage.length), 0);

    await page.locator('#customize').evaluate(el => el.open = true);
    // Keyboard focus moves through actual form controls.
    await page.locator('#prompt').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.tagName), 'SUMMARY');
    assert.deepEqual(errors, []);
    console.log('PASS: automatic session, discovery, real image uploads, frame/reference payload order, steps, remove/reupload, invalid/oversize handling, all legacy UI states, mobile layout, playback preservation, explicit idempotent reconciliation, keyboard.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
