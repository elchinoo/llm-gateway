import { defineConfig } from '@playwright/test';

/**
 * Recorder for docs/VIDEO-SCRIPT.md. Real Chrome, 1920x1080, Playwright video on, output in docs/video/out (git-ignored).
 * PACE=1 (default) is slow human pacing; PACE=0.05 is a quick rehearsal. NOVIDEO=1 turns the video off.
 */
const pace = Number(process.env.PACE ?? '1');
export default defineConfig({
  testDir: '.',
  testMatch: /record\.spec\.ts/,
  outputDir: './out',
  workers: 1,
  retries: 0,
  timeout: 20 * 60_000,
  reporter: [['list']],
  use: {
    channel: 'chrome',
    headless: !process.env.HEADED,
    viewport: { width: 1920, height: 1080 },
    video: process.env.NOVIDEO ? 'off' : { mode: 'on', size: { width: 1920, height: 1080 } },
    launchOptions: { slowMo: Math.round(350 * pace) },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
});
