const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/manual_layout_visual.js'), 'utf8');
function fixture() {
  const handlers = {};
  const gap = {value:'18', addEventListener(name,fn){handlers['gap-'+name]=fn;}};
  const alignment = {value:'stretch', addEventListener(name,fn){handlers['alignment-'+name]=fn;}};
  const sketch = {style:{}, classes:{}, classList:{toggle(name,val){sketch.classes[name]=val;}}};
  const buttons = ['desktop','mobile'].map(view => ({dataset:{manualViewport:view},attrs:{},
    addEventListener(name,fn){this[name]=fn;},setAttribute(name,val){this.attrs[name]=val;}}));
  const editor = {querySelector(selector){return ({'[data-layout-sketch]':sketch,
    '[name="settings-gap"]':gap,'[name="settings-alignment"]':alignment})[selector]},
    querySelectorAll(){return buttons}};
  vm.runInNewContext(source,{document:{querySelectorAll(){return [editor]}}});
  return {sketch,gap,alignment,buttons,handlers};
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
