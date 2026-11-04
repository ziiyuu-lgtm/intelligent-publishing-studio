const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../backend/static/local_mail_confirmation.js'), 'utf8');

function fixture(fragment, historyThrows = false) {
  const token = {value:''};
  const button = {disabled:true};
  const form = {elements:{namedItem:name=>name === 'token' ? token : null}, querySelector:()=>button};
  const document = {querySelector:()=>form};
  const history = {url:null, replaceState(_a, _b, url) {
    if (historyThrows) throw new Error('unavailable');
    this.url = url;
  }};
  const window = {location:{hash:fragment, pathname:'/test-mail-registration/attempt/'}, history};
  vm.runInNewContext(source, {document, window});
  return {token, button, history};
}

test('a valid fictional link clears its fragment before enabling confirmation', () => {
  const token = 'a'.repeat(43);
  const f = fixture('#' + token);
  assert.equal(f.history.url, '/test-mail-registration/attempt/');
  assert.equal(f.token.value, token);
  assert.equal(f.button.disabled, false);
});

test('invalid fragments or unavailable history cannot enable confirmation', () => {
  for (const fragment of ['', '#short', '#' + 'a'.repeat(42) + '!']) {
    const f = fixture(fragment);
    assert.equal(f.button.disabled, true);
    assert.equal(f.token.value, '');
  }
  const f = fixture('#' + 'b'.repeat(43), true);
  assert.equal(f.button.disabled, true);
  assert.equal(f.token.value, '');
});
