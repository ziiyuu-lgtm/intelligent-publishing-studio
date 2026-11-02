const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = path.join(__dirname, '..', 'dist', 'locales');
const full = ['en', 'zh-CN'];
const partial = ['zh-TW', 'es', 'fr', 'pt', 'ar', 'ru', 'de', 'ja', 'ko', 'hi'];
const read = code => JSON.parse(fs.readFileSync(path.join(base, `${code}.json`), 'utf8'));
const critical = [
  'Language', 'Home', 'Start submission', 'Editor workspace', 'Complete demo',
  'Demo data stays in this browser session.',
  'Only the file name is read; no file is uploaded.',
  'Simulation only: no AI model, reference database or manuscript service is connected.',
  'Any AI suggestions in this prototype are illustrative; no external AI model is connected.',
  'Your example is now visible in the simulated editor workspace. No manuscript was sent and no email was generated.',
  'I understand this is a simulated submission and no real manuscript will be sent.',
  'This changes only the example record in this browser session. No message will be sent.'
];

test('English and Simplified Chinese reference catalogues have matching keys', () => {
  assert.deepEqual(Object.keys(read('zh-CN')).sort(), Object.keys(read('en')).sort());
  for (const code of full) {
    const pack = read(code);
    for (const key of critical) assert.ok(pack[key], `${code}: missing ${key}`);
  }
});

test('each partial catalogue is safe plain text and includes key simulation notices', () => {
  const source = read('en');
  for (const code of partial) {
    const pack = read(code);
    assert.ok(Object.keys(pack).length < Object.keys(source).length, `${code} is unexpectedly complete; update its status`);
    for (const [key, value] of Object.entries(pack)) {
      assert.ok(key in source, `${code}: unknown key ${key}`);
      assert.equal(typeof value, 'string');
      assert.ok(value.trim(), `${code}: blank ${key}`);
      assert.doesNotMatch(value, /[<>&"]/u, `${code}: unsafe characters in ${key}`);
    }
    for (const key of critical) assert.ok(pack[key], `${code}: missing ${key}`);
  }
});

test('the selector advertises all maintained packs and marks Arabic right-to-left', () => {
  const app = fs.readFileSync(path.join(__dirname, '..', 'dist', 'app.js'), 'utf8');
  for (const code of [...full, ...partial]) assert.ok(app.includes(`code:'${code}'`), `missing ${code}`);
  assert.match(app, /code:'ar',label:'العربية',dir:'rtl'/);
  assert.match(app, /localeMeta\(state\.lang\)\?\.notice/);
});
