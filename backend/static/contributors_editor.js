/* Optional controls. Numeric order and one blank form work without JavaScript. */
(() => {
  const editor = document.querySelector('[data-author-editor]');
  if (!editor) return;
  const list = editor.querySelector('[data-author-list]');
  const template = editor.querySelector('[data-author-empty]');
  const total = editor.querySelector('[name="authors-TOTAL_FORMS"]');
  const maximum = editor.querySelector('[name="authors-MAX_NUM_FORMS"]');
  const status = editor.querySelector('[data-author-status]');
  const showControls = root => root.querySelectorAll('[data-author-js]').forEach(el => { el.hidden = false; });
  showControls(editor);
  let dragged = null;

  const updateOrder = (announce = false) => {
    let position = 0;
    for (const card of list.querySelectorAll('[data-author-card]')) {
      const name = card.querySelector('[name$="-name"]');
      const order = card.querySelector('[name$="-ORDER"]');
      const deleted = card.querySelector('[name$="-DELETE"]');
      if (name && order) order.value = name.value.trim() && !deleted.checked ? String(++position) : '';
    }
    if (announce) status.textContent = editor.dataset.movedText;
  };
  list.addEventListener('click', event => {
    const button = event.target.closest('[data-author-move]');
    if (!button) return;
    const card = button.closest('[data-author-card]');
    if (button.dataset.authorMove === 'up' && card.previousElementSibling) {
      list.insertBefore(card, card.previousElementSibling);
      updateOrder(true);
    } else if (button.dataset.authorMove === 'down' && card.nextElementSibling) {
      list.insertBefore(card.nextElementSibling, card);
      updateOrder(true);
    }
    button.focus();
  });
  list.addEventListener('input', event => {
    if (event.target.name.endsWith('-name')) updateOrder();
  });
  list.addEventListener('change', event => {
    if (event.target.name.endsWith('-DELETE')) updateOrder();
  });
  list.addEventListener('dragstart', event => {
    const handle = event.target.closest('[data-author-drag]');
    if (!handle) return;
    dragged = handle.closest('[data-author-card]');
    dragged.classList.add('is-dragging');
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', 'author');
    }
  });
  list.addEventListener('dragover', event => {
    if (dragged && event.target.closest('[data-author-card]')) event.preventDefault();
  });
  list.addEventListener('drop', event => {
    const target = event.target.closest('[data-author-card]');
    if (!dragged || !target || dragged === target) return;
    event.preventDefault();
    const after = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
    list.insertBefore(dragged, after ? target.nextElementSibling : target);
    updateOrder(true);
  });
  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('is-dragging');
    dragged = null;
  });
  editor.querySelector('[data-author-add]').addEventListener('click', () => {
    const count = Number(total.value), limit = Number(maximum.value);
    // Deleted forms still occupy indices; allow replacements up to Django's absolute maximum.
    const deleted = list.querySelectorAll('[name$="-DELETE"]:checked').length;
    if (!Number.isInteger(count) || !Number.isInteger(limit) || count >= limit * 2 || count - deleted >= limit) {
      status.textContent = editor.dataset.maxText;
      return;
    }
    const wrapper = document.createElement('div');
    wrapper.innerHTML = template.innerHTML.replace(/__prefix__/g, String(count));
    const card = wrapper.querySelector('[data-author-card]');
    if (!card) return;
    showControls(card);
    list.appendChild(card);
    total.value = String(count + 1);
    card.querySelector('[name$="-name"]').focus();
  });
})();
