const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../backend/static/section_editor.js'), 'utf8');

// Minimal DOM stand-in exercises the real event handlers; browser layout is not simulated.
function fixture() {
  const handlers = {};
  const list = {
    children: [],
    addEventListener(name, fn) { handlers[name] = fn; },
    querySelectorAll() { return this.children; },
    insertBefore(node, target) {
      this.children.splice(this.children.indexOf(node), 1);
      const index = target === null ? this.children.length : this.children.indexOf(target);
      this.children.splice(index, 0, node);
    },
    appendChild(node) { this.children.push(node); },
  };
  function card(id, value, newSection = false) {
    const classes = new Set();
    const result = { id, dataset: newSection ? {newSection:'true'} : {}, offsetHeight: 100,
      classList:{add(x){classes.add(x);}, remove(x){classes.delete(x);}, contains(x){return classes.has(x);}},
      getBoundingClientRect(){return {top:0};},
      closest(selector){return selector === '[data-section-card]' ? this : null;},
      querySelector(selector) {
        if (selector.includes('-kind')) return this.kind;
        if (selector.includes('-ORDER')) return this.order;
        if (selector.includes('-visible')) return this.visible;
        return null;
      },
    };
    Object.defineProperties(result, {
      previousElementSibling:{get(){const i=list.children.indexOf(this);return i > 0 ? list.children[i-1] : null;}},
      nextElementSibling:{get(){const i=list.children.indexOf(this);return i >= 0 ? list.children[i+1] || null : null;}},
    });
    result.kind = {name:`sections-${id}-kind`, value, focus(){this.focused=true;}, closest(){return result;}};
    result.order = {name:`sections-${id}-ORDER`, value:''};
    result.visible = {name:`sections-${id}-visible`, checked:false, closest(){return result;}};
    result.handle = {closest(selector){return selector === '[data-drag-handle]' ? this : result;}};
    result.button = direction => ({dataset:{move:direction}, focus(){this.focused=true;},
      closest(selector){return selector === '[data-move]' ? this : result;}});
    return result;
  }
  const about = card(0, 'about'), archive = card(1, 'archive'), blank = card(2, '', true);
  list.children.push(about, archive, blank);
  const total = {value:'3'}, max = {value:'12'}, status = {textContent:''}, template = {innerHTML:'__prefix__'};
  const addButton = {addEventListener(name, fn){handlers.add=fn;}};
  const editor = {dataset:{movedText:'order updated', maxText:'limit reached'}, querySelector(selector){
    if (selector === '[data-section-list]') return list;
    if (selector === '[data-empty-section]') return template;
    if (selector === '[name="sections-TOTAL_FORMS"]') return total;
    if (selector === '[name="sections-MAX_NUM_FORMS"]') return max;
    if (selector === '[data-section-status]') return status;
    if (selector === '[data-add-section]') return addButton;
    return null;
  }};
  const document = {querySelector(){return editor;}, createElement(){
    let created;
    return {set innerHTML(value){created=card(value, '', true);}, querySelector(){return created;}};
  }};
  vm.runInNewContext(source, {document});
  return {list, handlers, about, archive, blank, total, max, status};
}

test('move buttons reorder real form cards and maintain their submitted order', () => {
  const f=fixture(), up=f.archive.button('up');
  f.handlers.click({target:up});
  assert.deepEqual(f.list.children.map(c=>c.id), [1,0,2]);
  assert.equal(f.archive.order.value,'1');
  assert.equal(f.about.order.value,'2');
  assert.equal(f.blank.order.value,'');
  assert.equal(f.status.textContent,'order updated');
  assert.equal(up.focused,true);
});

test('dragging updates the order and ends the dragging state', () => {
  const f=fixture();
  let transferred='';
  f.handlers.dragstart({target:f.archive.handle,dataTransfer:{setData(type,value){transferred=value;}}});
  assert.equal(transferred,'journal-module');
  assert.equal(f.archive.classList.contains('is-dragging'),true);
  let prevented=false;
  f.handlers.drop({target:f.about,clientY:0,preventDefault(){prevented=true;}});
  f.handlers.dragend();
  assert.equal(prevented,true);
  assert.deepEqual(f.list.children.map(c=>c.id), [1,0,2]);
  assert.equal(f.archive.order.value,'1');
  assert.equal(f.about.order.value,'2');
  assert.equal(f.archive.classList.contains('is-dragging'),false);
});

test('adding a module updates management count and respects the maximum', () => {
  const f=fixture();
  f.handlers.add();
  assert.equal(f.total.value,'4');
  assert.equal(f.list.children.length,4);
  assert.equal(f.list.children[3].visible.checked,true);
  assert.equal(f.list.children[3].kind.focused,true);
  f.max.value='4';
  f.handlers.add();
  assert.equal(f.total.value,'4');
  assert.equal(f.status.textContent,'limit reached');
});

test('selecting a type on an empty module enables it until the user changes visibility', () => {
  const f=fixture();
  f.blank.kind.value='scope';
  f.handlers.change({target:f.blank.kind});
  assert.equal(f.blank.visible.checked,true);
  f.blank.visible.checked=false;
  f.handlers.change({target:f.blank.visible});
  f.blank.kind.value='policies';
  f.handlers.change({target:f.blank.kind});
  assert.equal(f.blank.visible.checked,false);
});
