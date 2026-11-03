const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/visual_brief.js'), 'utf8');

function makeForm(value = '', maxLength = 600) {
  const input = {value, maxLength, listeners:{}, addEventListener(name, fn){this.listeners[name]=fn;}, focus(){this.focused=true;}};
  const count = {textContent:''}, status = {textContent:''}, examples = {hidden:true};
  const buttons = ['A starter idea', 'Another idea'].map(text => ({dataset:{briefText:text},
    addEventListener(name, fn){this[name]=fn;}}));
  const form = {dataset:{remaining:'characters remaining',added:'Added',tooLong:'Too long'},
    querySelector(selector){return ({'[data-brief-input]':input,'[data-brief-count]':count,
      '[data-brief-status]':status,'[data-brief-examples]':examples})[selector];},
    querySelectorAll(){return buttons;}};
  return {form,input,count,status,examples,buttons};
}

function setup(...forms) {
  vm.runInNewContext(source, {document:{querySelectorAll(){return forms.map(f=>f.form);}}});
}

test('starter ideas append to the editable brief without replacing existing text', () => {
  const f=makeForm('My own direction');setup(f);
  assert.equal(f.examples.hidden,false);
  f.buttons[0].click();
  assert.equal(f.input.value,'My own direction\nA starter idea');
  assert.equal(f.count.textContent,'569 characters remaining');
  assert.equal(f.status.textContent,'Added');
  assert.equal(f.input.focused,true);
  f.input.value='Custom only';f.input.listeners.input();
  assert.equal(f.status.textContent,'');
  assert.equal(f.count.textContent,'589 characters remaining');
});

test('an idea that exceeds the limit is not inserted or truncated', () => {
  const f=makeForm('Keep these words',20);setup(f);f.buttons[0].click();
  assert.equal(f.input.value,'Keep these words');
  assert.equal(f.status.textContent,'Too long');
  assert.equal(f.count.textContent,'4 characters remaining');
});

test('each design form changes only its own brief', () => {
  const first=makeForm(), second=makeForm('Existing');setup(first,second);
  second.buttons[1].click();
  assert.equal(first.input.value,'');
  assert.equal(second.input.value,'Existing\nAnother idea');
});
