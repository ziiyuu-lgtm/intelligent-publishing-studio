const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/visual_ai_hero.js'), 'utf8');

function fixture() {
  const fields={hero_alt:{value:'Original'}, hero_fit:{value:'cover'},
    hero_position:{value:'center'},show_feature:{checked:true}};
  const classes=new Set(), handlers={};
  const thumb={classList:{toggle(name,on){on?classes.add(name):classes.delete(name);}}};
  const image={hidden:true,style:{},removeAttribute(name){delete this[name];}};
  let selected={name:'hero_asset_id',dataset:{heroUrl:'/private/a',heroAlt:'First image'}};
  const form={elements:{namedItem(name){return fields[name];}},
    querySelector(selector){if(selector.includes('thumbnail'))return thumb;
      if(selector.includes('hero-image'))return image;return selected;},
    addEventListener(type,fn){handlers[type]=fn;}};
  vm.runInNewContext(source,{document:{querySelectorAll(){return [form];}}});
  return {fields,classes,handlers,image,select(value){selected=value;handlers.change({target:value});}};
}

test('changing the chosen image updates the local sketch and description', () => {
  const f=fixture();
  assert.equal(f.image.src,'/private/a');
  f.fields.hero_fit.value='contain';f.fields.hero_position.value='top';
  f.select({name:'hero_asset_id',dataset:{heroUrl:'/private/b',heroAlt:'Second image'}});
  assert.equal(f.fields.hero_alt.value,'Second image');
  assert.equal(f.image.src,'/private/b');
  assert.equal(f.image.style.objectFit,'contain');
  assert.equal(f.image.style.objectPosition,'top');
  assert.equal(f.classes.has('has-visual-hero'),true);
});

test('removing the image restores the optional feature panel', () => {
  const f=fixture();f.fields.show_feature.checked=false;
  f.select({name:'hero_asset_id',dataset:{heroUrl:'',heroAlt:''}});
  assert.equal(f.image.hidden,true);
  assert.equal(f.image.src,undefined);
  assert.equal(f.classes.has('no-feature'),true);
  assert.equal(f.fields.hero_alt.value,'');
});
