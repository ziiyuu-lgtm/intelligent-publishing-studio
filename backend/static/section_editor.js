/* Progressive enhancement: the form remains usable with numeric order fields. */
(() => {
  const editor = document.querySelector('[data-section-editor]');
  if (!editor) return;
  const list = editor.querySelector('[data-section-list]');
  const template = editor.querySelector('[data-empty-section]');
  const total = editor.querySelector('[name="sections-TOTAL_FORMS"]');
  const maximum = editor.querySelector('[name="sections-MAX_NUM_FORMS"]');
  const status = editor.querySelector('[data-section-status]');
  let dragged = null;

  const updateOrder = () => {
    let position = 0;
    for (const card of list.querySelectorAll('[data-section-card]')) {
      const kind = card.querySelector('[name$="-kind"]');
      const order = card.querySelector('[name$="-ORDER"]');
      if (kind && order) order.value = kind.value ? String(++position) : '';
    }
    status.textContent = editor.dataset.movedText;
  };

  list.addEventListener('click', event => {
    const button = event.target.closest('[data-move]');
    if (!button) return;
    const card = button.closest('[data-section-card]');
    if (button.dataset.move === 'up' && card.previousElementSibling) {
      list.insertBefore(card, card.previousElementSibling);
      updateOrder();
    } else if (button.dataset.move === 'down' && card.nextElementSibling) {
      list.insertBefore(card.nextElementSibling, card);
      updateOrder();
    }
    button.focus();
  });

  list.addEventListener('change', event => {
    const card = event.target.closest('[data-section-card]');
    if (!card || card.dataset.newSection !== 'true') return;
    if (event.target.name.endsWith('-visible')) card.dataset.visibilityTouched = 'true';
    if (event.target.name.endsWith('-kind') && card.dataset.visibilityTouched !== 'true') {
      const visible = card.querySelector('[name$="-visible"]');
      if (visible) visible.checked = Boolean(event.target.value);
    }
  });

  list.addEventListener('dragstart', event => {
    const handle = event.target.closest('[data-drag-handle]');
    if (!handle) return;
    dragged = handle.closest('[data-section-card]');
    dragged.classList.add('is-dragging');
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', 'journal-module');
    }
  });
  list.addEventListener('dragover', event => {
    if (dragged && event.target.closest('[data-section-card]')) event.preventDefault();
  });
  list.addEventListener('drop', event => {
    const target = event.target.closest('[data-section-card]');
    if (!dragged || !target || dragged === target) return;
    event.preventDefault();
    const after = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
    list.insertBefore(dragged, after ? target.nextElementSibling : target);
    updateOrder();
  });
  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('is-dragging');
    dragged = null;
  });

  editor.querySelector('[data-add-section]').addEventListener('click', () => {
    const count = Number(total.value), limit = Number(maximum.value);
    if (!Number.isInteger(count) || !Number.isInteger(limit) || count >= limit) {
      status.textContent = editor.dataset.maxText;
      return;
    }
    const wrapper = document.createElement('div');
    wrapper.innerHTML = template.innerHTML.replace(/__prefix__/g, String(count));
    const card = wrapper.querySelector('[data-section-card]');
    if (!card) return;
    list.appendChild(card);
    total.value = String(count + 1);
    const visible = card.querySelector('[name$="-visible"]');
    if (visible) visible.checked = true;
    const kind = card.querySelector('[name$="-kind"]');
    if (kind) kind.focus();
  });
})();
