// Disposable cloud-mode browser check; writes only to an isolated temporary shelf.
const fs = require('fs');
const path = require('path');
const net = require('net');
const { spawn } = require('child_process');
const { chromium } = require('playwright');

const root = __dirname;
const tempPrefix = path.join(root, '.selftest-progress-ui-');
const temp = fs.mkdtempSync(tempPrefix);
const bookName = 'test.txt';
const bookText = '第一章\n你好😀一起读书。\n第二段。';
const otherName = 'other.txt';
const otherText = '另一册\n第一段。\n第二段。';
let child;
let browser;
let childError = '';

async function freePort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function main() {
  fs.copyFileSync(path.join(root, 'reader_server.py'), path.join(temp, 'reader_server.py'));
  fs.cpSync(path.join(root, 'web'), path.join(temp, 'web'), { recursive: true });
  fs.mkdirSync(path.join(temp, 'data'));
  fs.mkdirSync(path.join(temp, '书库'));
  fs.writeFileSync(path.join(temp, 'data', 'identities.json'),
    JSON.stringify({ user: 'test-user', '004': 'test-004', op001: 'test-op001', aru001: 'test-aru001' }));
  fs.writeFileSync(path.join(temp, '书库', bookName), bookText);
  fs.writeFileSync(path.join(temp, '书库', otherName), otherText);
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  child = spawn(process.env.PYTHON_TEST || 'python3', [path.join(temp, 'reader_server.py')], {
    env: { ...process.env, READER_CLOUD: '1', READER_PORT: String(port) },
    stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
  });
  child.stderr.on('data', data => { childError += data.toString(); });
  child.on('error', error => { childError += String(error); });
  const request = async (route, who = 'user', data) => {
    const response = await fetch(base + route, {
      method: data ? 'POST' : 'GET',
      headers: { 'X-Who-Token': 'test-' + who, ...(data ? { 'Content-Type': 'application/json' } : {}) },
      body: data ? JSON.stringify(data) : undefined,
    });
    const json = await response.json();
    if (!response.ok || !json.ok) throw new Error(`${route}: ${response.status} ${json.error}`);
    return json;
  };
  let ready = false;
  let lastError = '';
  for (let i = 0; i < 80; i++) {
    try { await request('/api/books'); ready = true; break; }
    catch (error) { lastError = String(error); await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  if (!ready) throw new Error('isolated reader server did not start: ' + lastError + ' / ' + childError.slice(-1000));
  const older = await request('/api/book?name=other.txt', '004');
  await request('/api/progress', '004', {
    name: otherName, offset: otherText.indexOf('第二段'),
    book_revision: older.book_revision, expected_revision: 0,
  });
  await new Promise(resolve => setTimeout(resolve, 20));
  const book = await request('/api/book?name=test.txt', '004');
  const offset = bookText.indexOf('第二段'); // JS index is UTF-16, matching the API.
  await request('/api/progress', '004', {
    name: bookName, offset, book_revision: book.book_revision, expected_revision: 0,
  });
  const other = await request('/api/book?name=other.txt', 'user');
  await request('/api/progress', 'user', {
    name: otherName, offset: otherText.indexOf('第一段'),
    book_revision: other.book_revision, expected_revision: 0,
  });
  const latest = await request('/api/reading-progress/latest');
  const latestById = Object.fromEntries(latest.readers.map(item => [item.reader_id, item.progress]));
  if (latestById['004']?.book_id !== bookName || latestById.user?.book_id !== otherName ||
      latestById.op001 !== null || latestById.aru001 !== null)
    throw new Error('aggregate endpoint chose wrong latest records');
  if ((await fetch(base + '/api/reading-progress/latest')).status !== 401)
    throw new Error('aggregate endpoint allowed anonymous reading');

  browser = await chromium.launch({ executablePath: process.env.CHROME_TEST, headless: true });
  for (const mobile of [true, false]) {
    const context = await browser.newContext({
      viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 820 },
      isMobile: mobile, hasTouch: mobile,
    });
    try {
      const page = await context.newPage();
      await page.goto(base + '/?who_token=test-user', { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => state.book?.name === 'other.txt');
      await page.locator('#btnLib').click();
      const beforeOverview = await request('/api/reading-progress?name=other.txt');
      await page.locator('#libTabs [data-tab=overview]').click();
      await page.waitForFunction(() => document.querySelectorAll('#overviewList .progressItem').length === 4);
      const cards = await page.locator('#overviewList .progressItem').allTextContents();
      if (!cards.some(text => text.includes('002') && text.includes('《other》')) ||
          !cards.some(text => text.includes('004') && text.includes('《test》')) ||
          !cards.some(text => text.includes('op001（001）') && text.includes('暂无记录')) ||
          !cards.some(text => text.includes('aru001') && text.includes('暂无记录')))
        throw new Error('overview did not aggregate latest book per reader');
      const afterOverview = await request('/api/reading-progress?name=other.txt');
      if (afterOverview.revision !== beforeOverview.revision)
        throw new Error('viewing overview changed own progress');
      await page.evaluate(() => openBook('test.txt'));
      await page.waitForFunction(() => state.book?.name === 'test.txt');
      await page.locator('#btnLib').click();
      await page.locator('#libTabs [data-tab=progress]').click();
      await page.waitForFunction(() => document.querySelectorAll('#progressList .progressItem').length === 1);
      const panel = page.locator('#progressList');
      if (!(await panel.innerText()).includes('004')) throw new Error('004 missing from progress tab');
      if (!(await panel.innerText()).includes('临时查看位置')) throw new Error('temporary jump missing');
      const tabFits = await page.locator('#libTabs').evaluate(el => el.scrollWidth <= el.clientWidth);
      if (!tabFits) throw new Error('progress tab overflows drawer');
      await panel.locator('.progressJump').click();
      const temporary = await page.evaluate(() => state.temporaryView &&
        !document.querySelector('#btnKeepPosition').hidden);
      if (!temporary) throw new Error('jump did not stay temporary');
      const own = await request('/api/reading-progress?name=test.txt');
      if (own.progress !== null) throw new Error('viewing 004 changed user progress');
    } finally { await context.close(); }
  }
  console.log('PASS: mobile/desktop aggregate latest per reader, existing book progress, and temporary jump');
}

main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  if (child) child.kill();
  if (!temp.startsWith(tempPrefix)) throw new Error('unsafe temporary path');
  fs.rmSync(temp, { recursive: true, force: true });
});
