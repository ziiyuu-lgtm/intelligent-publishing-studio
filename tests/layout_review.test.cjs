const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/layout_review.js'), 'utf8');

function fixture(options = {}) {
  const buttons = (block, key, value) => ({dataset:{[key]:value}, block, disabled:false,
    attributes:{}, setAttribute(name, val){this.attributes[name]=val;}, focus(){this.focused=true;},
    closest(selector){return selector === '[data-layout-block]' ? this.block : this;}});
  const blocks = [
    {dataset:{layoutId:'b0',layoutLabel:'First',layoutHidden:'no'}, order:{value:'2'}, span:{value:'8'}},
    {dataset:{layoutId:'b1',layoutLabel:'Hidden',layoutHidden:'yes'}, order:{value:'3'}, span:{value:'4'}},
    {dataset:{layoutId:'b2',layoutLabel:'Last',layoutHidden:'no'}, order:{value:'1'}, span:{value:'4'}},
  ];
  for (const block of blocks) {
    block.up=buttons(block,'layoutMove','up'); block.down=buttons(block,'layoutMove','down');
    block.widths=['3','4','6','8','12'].map(width=>buttons(block,'layoutWidth',width));
    block.preview = {content:{cloneNode(){return {textContent:'Safe saved content: '+block.dataset.layoutLabel};}}};
    block.querySelector = selector => ({'[data-layout-content]':block.preview, '[data-layout-order]':block.order, '[data-layout-span]':block.span,
      '[data-layout-move="up"]':block.up, '[data-layout-move="down"]':block.down})[selector];
    block.querySelectorAll = () => block.widths;
  }
  const sketch = {cards:[], handlers:{}, replaceChildren(...cards){this.cards=cards;},
    addEventListener(name, fn){this.handlers[name]=fn;}, contains(card){return this.cards.includes(card);}};
  const list = {append(block){const i=blocks.indexOf(block); if(i>=0) blocks.splice(i,1); blocks.push(block);}};
  const status = {textContent:''}, handlers = {}, controls = [{hidden:true}];
  const editor = {dataset:{emptyLabel:'No visible blocks',movedText:'Order updated',layoutContentMode:options.content?'yes':'no'},
    querySelector(selector){return ({'[data-layout-sketch]':sketch,'[data-layout-status]':status,
      '[data-layout-block-list]':list})[selector];},
    querySelectorAll(selector){return selector === '[data-layout-controls]' ? controls : blocks;},
    addEventListener(name, fn){handlers[name]=fn;}, contains(button){return !!button.block;}};
  const document = {querySelectorAll(){return [editor];}, createElement(){return {style:{},dataset:{},className:'',textContent:'',children:[],
    classList:{add(){}},append(child){this.children.push(child);},closest(){return this;}};}};
  vm.runInNewContext(source, {document});
  return {blocks,sketch,status,handlers,controls};
}

test('layout sketch tracks order and valid widths while excluding hidden blocks', () => {
  const f=fixture();
  const first=f.blocks.find(b=>b.dataset.layoutId==='b0'), last=f.blocks.find(b=>b.dataset.layoutId==='b2');
  assert.equal(f.controls[0].hidden, false);
  assert.deepEqual(f.sketch.cards.map(c=>c.textContent), ['Last','First']);
  assert.deepEqual(f.sketch.cards.map(c=>c.style.gridColumn), ['span 4','span 8']);
  assert.deepEqual(f.blocks.map(b=>b.dataset.layoutId), ['b2','b0','b1']);
  first.order.value='1'; last.order.value='2'; first.span.value='99'; f.handlers.input();
  assert.deepEqual(f.sketch.cards.map(c=>c.textContent), ['First','Last']);
  assert.equal(f.sketch.cards[0].style.gridColumn, 'span 12');
  first.dataset.layoutHidden='yes'; last.dataset.layoutHidden='yes'; f.handlers.input();
  assert.equal(f.sketch.cards[0].textContent, 'No visible blocks');
});

test('width shortcuts and move buttons update the selected block while keeping hidden blocks', () => {
  const f=fixture(), first=f.blocks.find(b=>b.dataset.layoutId==='b0'),
    hidden=f.blocks.find(b=>b.dataset.layoutId==='b1'), last=f.blocks.find(b=>b.dataset.layoutId==='b2');
  const half=first.widths.find(button=>button.dataset.layoutWidth==='6');
  f.handlers.click({target:half});
  assert.equal(first.span.value,'6');
  assert.equal(half.attributes['aria-pressed'],'true');
  assert.equal(last.span.value,'4');
  f.handlers.click({target:first.up});
  assert.deepEqual(f.blocks.map(b=>b.dataset.layoutId), ['b0','b2','b1']);
  assert.deepEqual(f.blocks.map(b=>b.order.value), ['1','2','3']);
  assert.equal(f.status.textContent,'Order updated');
  assert.equal(first.up.disabled,true);
  assert.equal(first.down.focused,true);
  assert.equal(hidden.dataset.layoutHidden,'yes');
});

test('dragging visible sketch cards reorders fields and keeps hidden blocks once', () => {
  const f=fixture();
  const last=f.sketch.cards[0], first=f.sketch.cards[1];
  let transferred='';
  f.sketch.handlers.dragstart({target:last,dataTransfer:{setData(_,x){transferred=x;}}});
  let prevented=false;
  f.sketch.handlers.drop({target:first,preventDefault(){prevented=true;}});
  assert.equal(transferred,'b2');
  assert.equal(prevented,true);
  assert.deepEqual(f.blocks.map(b=>b.dataset.layoutId), ['b0','b2','b1']);
  assert.deepEqual(f.blocks.map(b=>b.order.value), ['1','2','3']);
  assert.deepEqual(f.sketch.cards.map(c=>c.textContent), ['First','Last']);
  assert.equal(f.blocks[2].dataset.layoutHidden,'yes');
});

test('content mode clones the inert content template for visible blocks only', () => {
  const f=fixture({content:true});
  assert.equal(f.sketch.cards.length,2);
  assert.equal(f.sketch.cards[0].children[0].textContent,'Safe saved content: Last');
  assert.equal(f.sketch.cards[1].children[0].textContent,'Safe saved content: First');
  assert.deepEqual(f.sketch.cards.map(c=>c.dataset.layoutSketchItem),['b2','b0']);
});

test('redrawing content preserves existing preview elements', () => {
  const f=fixture({content:true}), before=f.sketch.cards[0];
  f.handlers.input();
  assert.equal(f.sketch.cards[0],before);
});
