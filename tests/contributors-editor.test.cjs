const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../backend/static/contributors_editor.js'), 'utf8');

function fixture(size = 2, maximum = 100) {
  const handlers = {};
  const list = {
    children: [],
    addEventListener(type, fn) { handlers[type] = fn; },
    querySelectorAll(selector) {
      if (selector === '[name$="-DELETE"]:checked') return this.children.filter(c => c.deleted.checked);
      return this.children;
    },
    insertBefore(node, target) {
      this.children.splice(this.children.indexOf(node), 1);
      const index = target === null ? this.children.length : this.children.indexOf(target);
      this.children.splice(index, 0, node);
    },
    appendChild(node) { this.children.push(node); },
  };
  function card(id, name = '') {
    const classes = new Set();
    const c = {id, offsetHeight:100, classList:{add:x=>classes.add(x), remove:x=>classes.delete(x)},
      getBoundingClientRect(){return {top:0};}, querySelectorAll(){return [];},
      closest(){return this;}, querySelector(selector){
        if (selector.includes('-name')) return this.name;
        if (selector.includes('-ORDER')) return this.order;
        if (selector.includes('-DELETE')) return this.deleted;
        return null;
      }};
    Object.defineProperties(c, {
      previousElementSibling:{get(){const i=list.children.indexOf(this);return i > 0 ? list.children[i-1] : null;}},
      nextElementSibling:{get(){const i=list.children.indexOf(this);return list.children[i+1] || null;}},
    });
    c.name = {name:`authors-${id}-name`, value:name, focus(){this.focused=true;}};
    c.order = {value:''};
    c.deleted = {name:`authors-${id}-DELETE`, checked:false};
    c.button = direction => ({dataset:{authorMove:direction}, focus(){this.focused=true;},
      closest(selector){return selector === '[data-author-move]' ? this : c;}});
    return c;
  }
  for (let i=0; i<size; i++) list.children.push(card(i, `Author ${i}`));
  const total = {value:String(size)}, max = {value:String(maximum)}, status = {textContent:''}, template = {innerHTML:'__prefix__'};
  const button = {addEventListener(type, fn){handlers.add=fn;}};
  const editor = {dataset:{movedText:'updated', maxText:'limit'}, querySelectorAll(){return [];}, querySelector(selector){
    if (selector === '[data-author-list]') return list;
    if (selector === '[data-author-empty]') return template;
    if (selector === '[name="authors-TOTAL_FORMS"]') return total;
    if (selector === '[name="authors-MAX_NUM_FORMS"]') return max;
    if (selector === '[data-author-status]') return status;
    if (selector === '[data-author-add]') return button;
    return null;
  }};
  const document = {querySelector(){return editor;}, createElement(){let created; return {
    set innerHTML(value){created=card(Number(value));}, querySelector(){return created;}
  };}};
  vm.runInNewContext(source, {document});
  return {list, handlers, total, max, status};
}

test('moving an author updates the submitted order without changing author fields', () => {
  const f=fixture();
  const first=f.list.children[0], second=f.list.children[1], up=second.button('up');
  f.handlers.click({target:up});
  assert.deepEqual(f.list.children.map(c=>c.id), [1,0]);
  assert.equal(second.order.value, '1');
  assert.equal(first.order.value, '2');
  assert.equal(first.name.value, 'Author 0');
  assert.equal(f.status.textContent, 'updated');
});

test('at the maximum, removing an author permits a replacement without exceeding the absolute limit', () => {
  const f=fixture(100);
  f.handlers.add();
  assert.equal(f.total.value, '100');
  assert.equal(f.status.textContent, 'limit');
  f.list.children[1].deleted.checked=true;
  f.handlers.add();
  assert.equal(f.total.value, '101');
  assert.equal(f.list.children[100].name.focused, true);
  f.handlers.add();
  assert.equal(f.total.value, '101');
  f.total.value='200';
  f.handlers.add();
  assert.equal(f.total.value, '200');
});
