const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/manual_layout_visual.js'), 'utf8');
function makeEditor() {
  const handlers = {};
  const gap = {value:'18', addEventListener(name,fn){handlers['gap-'+name]=fn;}};
  const alignment = {value:'stretch', addEventListener(name,fn){handlers['alignment-'+name]=fn;}};
  const sketch = {style:{}, classes:{}, classList:{toggle(name,val){sketch.classes[name]=val;}}};
  const buttons = ['desktop','mobile'].map(view => ({dataset:{manualViewport:view},attrs:{},
    addEventListener(name,fn){this[name]=fn;},setAttribute(name,val){this.attrs[name]=val;}}));
  const displays = ['content','outline'].map(view => ({dataset:{manualDisplay:view},attrs:{},
    addEventListener(name,fn){this[name]=fn;},setAttribute(name,val){this.attrs[name]=val;}}));
  const editor = {dataset:{layoutContentMode:'yes'},events:[],dispatchEvent(event){this.events.push(event.type);},querySelector(selector){return ({'[data-layout-sketch]':sketch,
    '[data-layout-gap]':gap,'[data-layout-alignment]':alignment})[selector]},
    querySelectorAll(selector){return selector === '[data-manual-viewport]' ? buttons : displays}};
  return {sketch,gap,alignment,buttons,displays,handlers,editor};
}
function fixture(count=1) {
  const fixtures = Array.from({length:count}, () => makeEditor());
  class Event { constructor(type) { this.type=type; } }
  vm.runInNewContext(source,{document:{querySelectorAll(){return fixtures.map(f => f.editor)}},Event});
  return count === 1 ? fixtures[0] : fixtures;
}

test('viewport switch only changes canvas presentation and keeps form values', () => {
  const f=fixture();
  assert.equal(f.sketch.style.gap,'18px');
  assert.equal(f.sketch.style.alignItems,'stretch');
  f.buttons[1].click();
  assert.equal(f.sketch.classes['manual-layout-sketch--mobile'],true);
  assert.equal(f.buttons[1].attrs['aria-pressed'],'true');
  assert.equal(f.buttons[0].attrs['aria-pressed'],'false');
  assert.equal(f.gap.value,'18');
  f.buttons[0].click();
  assert.equal(f.sketch.classes['manual-layout-sketch--mobile'],false);
});

test('canvas previews valid gap and alignment, and clears invalid values', () => {
  const f=fixture();
  f.gap.value='42';f.handlers['gap-input']();assert.equal(f.sketch.style.gap,'42px');
  f.gap.value='65';f.handlers['gap-input']();assert.equal(f.sketch.style.gap,'');
  f.alignment.value='start';f.handlers['alignment-change']();assert.equal(f.sketch.style.alignItems,'start');
  f.alignment.value='invalid';f.handlers['alignment-change']();assert.equal(f.sketch.style.alignItems,'');
});

test('content and outline switch only the preview mode', () => {
  const f=fixture();
  f.displays[1].click();
  assert.equal(f.editor.dataset.layoutContentMode,'no');
  assert.equal(f.displays[1].attrs['aria-pressed'],'true');
  assert.deepEqual(f.editor.events,['layout-restore']);
  assert.equal(f.gap.value,'18');
  f.displays[0].click();
  assert.equal(f.editor.dataset.layoutContentMode,'yes');
});

test('multiple suggestion canvases maintain independent display and form settings', () => {
  const [first, second] = fixture(2);
  second.gap.value='36';second.handlers['gap-input']();
  second.buttons[1].click();second.displays[1].click();
  assert.equal(first.sketch.style.gap,'18px');
  assert.equal(second.sketch.style.gap,'36px');
  assert.equal(first.sketch.classes['manual-layout-sketch--mobile'],undefined);
  assert.equal(second.sketch.classes['manual-layout-sketch--mobile'],true);
  assert.equal(first.editor.dataset.layoutContentMode,'yes');
  assert.equal(second.editor.dataset.layoutContentMode,'no');
  assert.deepEqual(first.editor.events,[]);
  assert.deepEqual(second.editor.events,['layout-restore']);
});
