const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/visual_ai_devices.js'), 'utf8');

function fixture(supportsSrcdoc = true) {
  const frame = {style:{}};
  if (supportsSrcdoc) frame.srcdoc = '';
  const buttons = ['desktop', 'tablet', 'mobile'].map(name => ({
    dataset:{visualDevice:name}, attrs:{},
    addEventListener(type, fn){ this.onClick = fn; },
    setAttribute(name, value){ this.attrs[name] = value; },
  }));
  const links = [{attrs:{href:'/fictional'}, removeAttribute(name){delete this.attrs[name];},
    setAttribute(name,value){this.attrs[name]=value;}}];
  const snapshot = {querySelectorAll(){return links;}};
  const site = {hidden:false, cloneNode(){return snapshot;}};
  const panel = {hidden:true,querySelector(){return frame;},querySelectorAll(){return buttons;}};
  const head = {children:[],appendChild(item){this.children.push(item);}};
  const body = {children:[],appendChild(item){this.children.push(item);}};
  const page = {head,body,documentElement:{lang:'',outerHTML:'<html><body>Reviewed site</body></html>'},
    createElement(tag){return {tagName:tag.toUpperCase()};}};
  let onReady;
  const document = {documentElement:{lang:'en'},implementation:{createHTMLDocument(){return page;}},
    addEventListener(type,fn){onReady=fn;},
    querySelector(selector){return selector.includes('device')?panel:site;},querySelectorAll(){return [];}};
  vm.runInNewContext(source,{document});
  onReady();
  return {frame,buttons,site,panel,links,head,body};
}

test('device controls switch the preview without editing the original site', () => {
  const f = fixture();
  assert.equal(f.frame.style.width, '1080px');
  assert.equal(f.buttons[0].attrs['aria-pressed'], 'true');
  f.buttons[2].onClick();
  assert.equal(f.frame.style.width, '390px');
  assert.equal(f.buttons[0].attrs['aria-pressed'], 'false');
  assert.equal(f.buttons[2].attrs['aria-pressed'], 'true');
  f.buttons[1].onClick();
  assert.equal(f.frame.style.width, '768px');
  assert.equal(f.links[0].attrs.href, undefined);
  assert.equal(f.links[0].attrs['aria-disabled'], 'true');
  assert.equal(f.panel.hidden, false);
  assert.equal(f.site.hidden, true);
});

test('the full site stays visible if srcdoc is unavailable', () => {
  const f = fixture(false);
  assert.equal(f.panel.hidden, true);
  assert.equal(f.site.hidden, false);
});
