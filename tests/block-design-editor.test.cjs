const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../backend/static/block_design_editor.js'), 'utf8');

function fixture() {
  let handler;
  const field = {value:'#ffffff', matches(selector){return selector === '[data-block-colour-input]';},
    closest(){return {querySelector(){return picker;}};}};
  const picker = {value:'#ffffff', dataset:{target:'block-color'}, matches(selector){return selector === '[data-block-colour-picker]';}};
  const document = {addEventListener(name, fn){if(name === 'input') handler = fn;},
    getElementById(id){return id === 'block-color' ? field : null;}};
  vm.runInNewContext(source, {document});
  return {field, picker, input(target){handler({target});}};
}

test('colour picker updates the form value used for saving', () => {
  const f=fixture(); f.picker.value='#8a41c9'; f.input(f.picker);
  assert.equal(f.field.value, '#8a41c9');
});

test('valid typed colours update the picker; partial values leave it unchanged', () => {
  const f=fixture(); f.field.value='#123'; f.input(f.field); assert.equal(f.picker.value, '#ffffff');
  f.field.value='#12Ab34'; f.input(f.field); assert.equal(f.picker.value, '#12Ab34');
});
