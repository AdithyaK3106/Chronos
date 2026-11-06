import { createRequire } from 'module';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Import playwright from the installed skill directory
const require = createRequire(import.meta.url);
const skillPlaywrightPath = 'C:/Users/Tanish/.gemini/config/plugins/agy-ultimate/skills/browser-verify/node_modules/playwright';
const { chromium } = require(skillPlaywrightPath);

async function verify() {
  console.log('Launching installed Chrome for visual verification...');
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true
  });
  const context = await browser.newContext();
  const page = await context.newPage();

  const fileUrl = 'file:///' + path.resolve(__dirname, '../index.html').replace(/\\/g, '/');
  console.log(`Loading: ${fileUrl}`);

  const viewports = [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'tablet', width: 768, height: 1024 },
    { name: 'mobile', width: 375, height: 812 }
  ];

  const consoleErrors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') {
      consoleErrors.push(msg.text());
    }
  });

  page.on('pageerror', err => {
    consoleErrors.push(err.message);
  });

  let allPassed = true;

  for (const vp of viewports) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto(fileUrl, { waitUntil: 'load' });
    await page.waitForTimeout(800);

    // Check for horizontal overflow
    const overflow = await page.evaluate(() => {
      const docWidth = document.documentElement.scrollWidth;
      const clientWidth = document.documentElement.clientWidth;
      const windowWidth = window.innerWidth;
      return {
        docWidth,
        clientWidth,
        windowWidth,
        hasOverflow: docWidth > windowWidth
      };
    });

    console.log(`Viewport [${vp.name} - ${vp.width}px]: docWidth=${overflow.docWidth}, windowWidth=${overflow.windowWidth}, hasOverflow=${overflow.hasOverflow}`);
    if (overflow.hasOverflow) {
      console.error(`ERROR: Horizontal overflow detected in ${vp.name}! docWidth=${overflow.docWidth} > windowWidth=${overflow.windowWidth}`);
      allPassed = false;
    }

    // Capture full screenshot
    const screenshotPath = path.resolve(__dirname, `../artifacts/landing_${vp.name}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: true });
    console.log(`Captured full page screenshot: ${screenshotPath}`);
  }

  console.log(`Total console errors: ${consoleErrors.length}`);
  if (consoleErrors.length > 0) {
    console.error('Console errors:', consoleErrors);
    allPassed = false;
  }

  // Also test interactive tabs in simulator
  console.log('Testing interactive terminal simulator toggle...');
  await page.click('#tab-unprotected');
  await page.waitForTimeout(400);
  const unprotectedText = await page.textContent('#sim-body');
  if (!unprotectedText.includes('EXFILTRATED')) {
    console.error('ERROR: Terminal simulator unprotected state failed to render!');
    allPassed = false;
  } else {
    console.log('Terminal simulator toggle verified.');
  }

  // Test ROI calculator slider update
  console.log('Testing ROI calculator reactivity...');
  await page.evaluate(() => {
    const slider = document.getElementById('slider-agents');
    slider.value = '50';
    slider.dispatchEvent(new Event('input'));
  });
  await page.waitForTimeout(200);
  const updatedVal = await page.textContent('#calc-agents-val');
  if (!updatedVal.includes('50 agents')) {
    console.error('ERROR: ROI calculator failed to update reactively!');
    allPassed = false;
  } else {
    console.log(`ROI calculator reactive update verified: ${updatedVal}`);
  }

  // Test FAQ accordion
  console.log('Testing FAQ accordion interaction...');
  const firstFaq = page.locator('.faq-question').first();
  await firstFaq.click();
  await page.waitForTimeout(400);
  const faqOpen = await page.evaluate(() => {
    return document.querySelector('.faq-item').classList.contains('open');
  });
  if (!faqOpen) {
    console.error('ERROR: FAQ accordion failed to open!');
    allPassed = false;
  } else {
    console.log('FAQ accordion open state verified.');
  }

  await browser.close();

  if (allPassed) {
    console.log('ALL VERIFICATION CHECKS PASSED: 0 horizontal overflow, 0 console errors, interactive states validated.');
    process.exit(0);
  } else {
    console.error('VERIFICATION CHECKS FAILED.');
    process.exit(1);
  }
}

verify().catch(err => {
  console.error('Exception during verification:', err);
  process.exit(1);
});
