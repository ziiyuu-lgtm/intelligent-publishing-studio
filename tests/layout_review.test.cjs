const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/layout_review.js'), 'utf8');

test('layout sketch tracks order and valid widths while excluding hidden blocks', () => {
  const blocks = [
    {dataset:{layoutLabel:'First',layoutHidden:'no'}, order:{value:'2'}, span:{value:'8'}},
    {dataset:{layoutLabel:'Hidden',layoutHidden:'yes'}, order:{value:'3'}, span:{value:'4'}},
    {dataset:{layoutLabel:'Last',layoutHidden:'no'}, order:{value:'1'}, span:{value:'4'}},
  ];
  for (const block of blocks) block.querySelector = selector => selector === '[data-layout-order]' ? block.order : block.span;
  const sketch = {cards:[], replaceChildren(...cards){this.cards=cards;}};
  let input;
  const editor = {dataset:{emptyLabel:'No visible blocks'},
    querySelector(){return sketch;}, querySelectorAll(){return blocks;}, addEventListener(_, fn){input=fn;}};
  const document = {querySelectorAll(){return [editor];}, createElement(){return {style:{},className:'',textContent:''};}};
  vm.runInNewContext(source, {document});
  assert.deepEqual(sketch.cards.map(c=>c.textContent), ['Last','First']);
  assert.deepEqual(sketch.cards.map(c=>c.style.gridColumn), ['span 4','span 8']);
  blocks[0].order.value='1'; blocks[2].order.value='2'; blocks[0].span.value='99'; input();
  assert.deepEqual(sketch.cards.map(c=>c.textContent), ['First','Last']);
  assert.equal(sketch.cards[0].style.gridColumn, 'span 12');
  blocks[0].dataset.layoutHidden='yes'; blocks[2].dataset.layoutHidden='yes'; input();
  assert.equal(sketch.cards[0].textContent, 'No visible blocks');
});
