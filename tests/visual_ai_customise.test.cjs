const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/visual_ai_customise.js'), 'utf8');

function fixture() {
  const fields={primary:{value:'#17483f'},surface:{value:'#eaf2ed'},background:{value:'#fffefa'},
    hero_style:{value:'solid'},composition:{value:'classic'},alignment:{value:'left'},
    section_order:{value:'about_first'},show_feature:{checked:true}};
  const props={}, classes=new Set(), listeners={};
  const thumb={style:{setProperty(key,value){props[key]=value;}},classList:{
    add(x){classes.add(x);},remove(x){classes.delete(x);},toggle(x,on){on?classes.add(x):classes.delete(x);}}};
  const form={elements:{namedItem(name){return fields[name];}},
    querySelector(){return thumb;},addEventListener(name,fn){listeners[name]=fn;}};
  vm.runInNewContext(source,{document:{querySelectorAll(){return [form];}}});
  return {fields,props,classes,listeners};
}

test('preview updates colour, contrast, composition and feature without submitting', () => {
  const f=fixture();
  assert.equal(f.props['--thumb-hero'],'#17483f');
  assert.equal(f.props['--thumb-ink'],'#ffffff');
  f.fields.primary.value='#a13d66';f.fields.hero_style.value='soft';f.fields.composition.value='split';
  f.fields.alignment.value='center';f.fields.show_feature.checked=false;f.listeners.input();
  assert.equal(f.props['--thumb-hero'],'#eaf2ed');
  assert.equal(f.props['--thumb-ink'],'#000000');
  assert.equal(f.classes.has('composition-split'),true);
  assert.equal(f.classes.has('composition-classic'),false);
  assert.equal(f.classes.has('align-center'),true);
  assert.equal(f.classes.has('no-feature'),true);
});

test('invalid colours do not enter preview styles', () => {
  const f=fixture(), before=f.props['--thumb-primary'];
  f.fields.primary.value='url(bad)';f.listeners.change();
  assert.equal(f.props['--thumb-primary'],before);
});
