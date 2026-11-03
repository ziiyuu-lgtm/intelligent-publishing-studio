const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../backend/static/image_ai_ideas.js'),'utf8');

function setup(value='') {
  const brief={value,focus(){this.focused=true;},addEventListener(name,fn){this[name]=fn;}};
  const status={textContent:''};
  const button={dataset:{imageIdea:'A calm research illustration'},addEventListener(name,fn){this[name]=fn;}};
  const form={dataset:{ideaAdded:'Added',ideaLong:'Too long'},elements:{namedItem(){return brief;}},
    querySelector(){return status;},querySelectorAll(){return [button];}};
  vm.runInNewContext(source,{document:{querySelectorAll(){return [form];}}});
  return {brief,status,button};
}

test('a starting idea appends without replacing the existing creative brief',()=>{
  const f=setup('My idea');f.button.click();
  assert.equal(f.brief.value,'My idea\nA calm research illustration');
  assert.equal(f.status.textContent,'Added');
  assert.equal(f.brief.focused,true);
});

test('an idea that exceeds the limit is not inserted',()=>{
  const f=setup('x'.repeat(590));f.button.click();
  assert.equal(f.brief.value,'x'.repeat(590));assert.equal(f.status.textContent,'Too long');
});
