const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../backend/static/image_candidate_preview.js'), 'utf8');
function setup(hasPage = false, hasTarget = false) {
  const make = value => ({value, handlers:{}, addEventListener(name, fn){this.handlers[name]=fn;}});
  const fit = make('cover'), position = make('center'), page = hasPage ? make('about') : null,
        target = hasTarget ? make('main') : null;
  const controls = {handlers:{}, elements:{namedItem(name){return {fit,position,page,target}[name] || null;}},
    addEventListener(name, fn){this.handlers[name]=fn;}};
  const button = {disabled:false}, hint = {hidden:true};
  const apply = {querySelector(selector){return selector==='[data-image-apply-button]'?button:hint;}};
  const document = {addEventListener(name,fn){if(name==='DOMContentLoaded')fn();},
    querySelector(selector){return selector==='[data-image-preview-controls]'?controls:apply;}};
  vm.runInNewContext(source,{document});
  return {fit,position,page,target,controls,button,hint};
}
test('changed choices disable applying until the displayed choices are restored', () => {
  const v=setup();v.fit.value='contain';v.fit.handlers.change();
  assert.equal(v.button.disabled,true);assert.equal(v.hint.hidden,false);
  v.fit.value='cover';v.fit.handlers.change();
  assert.equal(v.button.disabled,false);assert.equal(v.hint.hidden,true);
  v.position.value='top';v.position.handlers.change();assert.equal(v.button.disabled,true);
});
test('a changed placement requires a refreshed preview before application', () => {
  const v=setup(true,true);v.target.value='block-2';v.target.handlers.change();
  assert.equal(v.button.disabled,true);assert.equal(v.hint.hidden,false);
  v.controls.handlers['ips-preview-updated']();
  assert.equal(v.button.disabled,false);assert.equal(v.hint.hidden,true);
  v.target.value='main';v.target.handlers.change();assert.equal(v.button.disabled,true);
});
test('a changed target page also requires an updated preview', () => {
  const v=setup(true);v.page.value='editorial-board';v.page.handlers.change();
  assert.equal(v.button.disabled,true);assert.equal(v.hint.hidden,false);
  v.controls.handlers['ips-preview-updated']();
  assert.equal(v.button.disabled,false);assert.equal(v.hint.hidden,true);
  v.page.value='about';v.page.handlers.change();assert.equal(v.button.disabled,true);
});
test('the static preview can mark locally updated displayed choices', () => {
  const v=setup();v.fit.value='contain';v.position.value='bottom';v.fit.handlers.change();
  assert.equal(v.button.disabled,true);v.controls.handlers['ips-preview-updated']();
  assert.equal(v.button.disabled,false);assert.equal(v.hint.hidden,true);
  v.position.value='top';v.position.handlers.change();assert.equal(v.button.disabled,true);
});
