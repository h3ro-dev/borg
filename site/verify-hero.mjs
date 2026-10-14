// Hero turn acceptance on collective.html; invoked by verify.mjs with its /borg/ test server.
// The Blender turn must take over from an identical poster, play, pause with the shared motion
// switch, use the smaller file on phones, and never download for reduced-motion visitors.
import assert from 'node:assert/strict';

const ready = (page) =>
  page.waitForFunction(() => document.querySelector('.ship-stage').dataset.state === 'ready', null, { timeout: 20000 });

export async function verifyHero({ browser, origin, results }) {
  const url = origin + '/borg/collective.html';

  const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await desktop.newPage();
  await page.goto(url);
  await ready(page);
  assert((await page.locator('.ship-video').evaluate((v) => v.currentSrc)).endsWith('/assets/borg-ship-turn-960.webm'));
  // Compare the poster with the video's first frame, drawn the same size.
  const difference = await page.evaluate(async () => {
    const video = document.querySelector('.ship-video');
    const poster = document.querySelector('.ship-poster');
    const resume = !video.paused;
    video.pause();
    if (video.currentTime !== 0) {
      video.currentTime = 0;
      await new Promise((resolve) => video.addEventListener('seeked', resolve, { once: true }));
    }
    const read = (element) => {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 160;
      const context = canvas.getContext('2d');
      context.drawImage(element, 0, 0, 160, 160);
      return context.getImageData(0, 0, 160, 160).data;
    };
    const a = read(poster);
    const b = read(video);
    let total = 0;
    for (let i = 0; i < a.length; i++) total += Math.abs(a[i] - b[i]);
    if (resume) await video.play();
    return total / a.length;
  });
  assert(difference < 6, `The first video frame differs from the poster by ${difference.toFixed(2)} per channel`);
  await page.waitForTimeout(1600);
  const state = await page.locator('.ship-video').evaluate((v) => ({ paused: v.paused, time: v.currentTime }));
  assert(!state.paused && state.time > 0.3, `The turn should be playing: ${JSON.stringify(state)}`);
  assert.equal(await page.locator('.ship-poster').evaluate((el) => getComputedStyle(el).opacity), '0');
  await page.locator('#motion-toggle').click();
  assert(await page.locator('.ship-video').evaluate((v) => v.paused), 'The shared motion switch pauses the turn');
  await desktop.close();

  // The turn is always the size of the poster the browser chose for the screen's pixels.
  for (const [label, viewport, deviceScaleFactor, expected] of [
    ['1x laptop', { width: 1440, height: 900 }, 1, 960],
    ['2x laptop', { width: 1440, height: 900 }, 2, 1200],
    ['1x phone', { width: 390, height: 844 }, 1, 720],
    ['3x phone', { width: 390, height: 844 }, 3, 1200],
  ]) {
    const context = await browser.newContext({ viewport, deviceScaleFactor });
    const view = await context.newPage();
    await view.goto(url);
    await ready(view);
    const pair = await view.evaluate(() => ({
      poster: document.querySelector('.ship-poster').currentSrc,
      video: document.querySelector('.ship-video').currentSrc,
    }));
    const posterSize = pair.poster.includes('poster-720') ? 720 : pair.poster.includes('poster-960') ? 960 : 1200;
    assert.equal(posterSize, expected, `${label}: poster ${pair.poster}`);
    assert(pair.video.endsWith(`/assets/borg-ship-turn-${expected}.webm`), `${label}: video ${pair.video}`);
    await context.close();
  }

  const calm = await browser.newContext({ reducedMotion: 'reduce' });
  const still = await calm.newPage();
  const requested = [];
  still.on('request', (request) => { if (request.url().endsWith('.webm')) requested.push(request.url()); });
  await still.goto(url, { waitUntil: 'load' });
  await still.waitForTimeout(800);
  assert.equal(await still.locator('.ship-video').count(), 0);
  assert.deepEqual(requested, [], 'Reduced motion never downloads the turn');
  await calm.close();

  results.checks.push(
    `Hero turn: the Blender video takes over from an identical poster (mean difference ${difference.toFixed(2)}/255), plays, pauses with the shared motion switch, matches the poster size the browser chose (720/960/1200 for 1x and 3x screens) and is never downloaded with reduced motion`,
  );
}
