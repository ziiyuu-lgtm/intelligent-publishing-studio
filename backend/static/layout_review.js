/* The sketch and controls edit the submitted fields; the server validates all changes. */
(() => {
  document.querySelectorAll('[data-layout-editor]').forEach(editor => {
    const sketch = editor.querySelector('[data-layout-sketch]');
    const status = editor.querySelector('[data-layout-status]');
    const list = editor.querySelector('[data-layout-block-list]');
    let draggedId = null;
    const cardCache = new Map();
    editor.querySelectorAll('[data-layout-controls]').forEach(control => { control.hidden = false; });

    const getRows = () => [...editor.querySelectorAll('[data-layout-block]')].map((block, index) => {
      const orderField = block.querySelector('[data-layout-order]');
      const spanField = block.querySelector('[data-layout-span]');
      const order = Number(orderField.value), span = Number(spanField.value);
      return {block, index, orderField, spanField,
        order:Number.isInteger(order) && order >= 1 && order <= 20 ? order : index + 1,
        span:Number.isInteger(span) && span >= 1 && span <= 12 ? span : 12};
    }).sort((a, b) => a.order - b.order || a.index - b.index);

    const redraw = () => {
      const rows = getRows();
      rows.forEach(({block, spanField}, position) => {
        const up = block.querySelector('[data-layout-move="up"]');
        const down = block.querySelector('[data-layout-move="down"]');
        if (up) up.disabled = position === 0;
        if (down) down.disabled = position === rows.length - 1;
        block.querySelectorAll('[data-layout-width]').forEach(button => {
          button.setAttribute('aria-pressed', String(button.dataset.layoutWidth === spanField.value));
        });
      });
      const cards = rows.filter(({block}) => block.dataset.layoutHidden !== 'yes').map(({block, span}) => {
        const content = block.querySelector('[data-layout-content]');
        const useContent = editor.dataset.layoutContentMode === 'yes' && content && content.content;
        const key = `${block.dataset.layoutId}:${useContent ? 'content' : 'outline'}`;
        let card = cardCache.get(key);
        if (!card) {
          card = document.createElement('div');
          card.className = 'layout-sketch__block';
          if (useContent) {
            card.classList.add('layout-sketch__block--content');
            card.append(content.content.cloneNode(true));
          } else {
            card.textContent = block.dataset.layoutLabel;
          }
          card.draggable = true;
          card.dataset.layoutSketchItem = block.dataset.layoutId;
          cardCache.set(key, card);
        }
        card.style.gridColumn = `span ${span}`;
        return card;
      });
      if (!cards.length) {
        const empty = document.createElement('div');
        empty.className = 'layout-sketch__block';
        empty.style.gridColumn = 'span 12';
        empty.textContent = editor.dataset.emptyLabel;
        cards.push(empty);
      }
      sketch.replaceChildren(...cards);
    };

    const move = (sourceId, targetId, focusButton = null) => {
      const rows = getRows();
      const from = rows.findIndex(row => row.block.dataset.layoutId === sourceId);
      const to = rows.findIndex(row => row.block.dataset.layoutId === targetId);
      if (from < 0 || to < 0 || from === to) return;
      const [row] = rows.splice(from, 1);
      rows.splice(to, 0, row);
      rows.forEach((entry, index) => { entry.orderField.value = String(index + 1); });
      rows.forEach(entry => list.append(entry.block));
      if (status) status.textContent = editor.dataset.movedText;
      redraw();
      if (focusButton) {
        const fallback = focusButton.dataset.layoutMove === 'up' ? 'down' : 'up';
        const focusTarget = focusButton.disabled ? row.block.querySelector(`[data-layout-move="${fallback}"]`) : focusButton;
        if (focusTarget && !focusTarget.disabled) focusTarget.focus();
      }
    };

    editor.addEventListener('input', redraw);
    editor.addEventListener('layout-restore', () => {
      getRows().forEach(entry => list.append(entry.block));
      redraw();
    });
    editor.addEventListener('click', event => {
      const button = event.target.closest('[data-layout-width], [data-layout-move]');
      if (!button || !editor.contains(button)) return;
      const block = button.closest('[data-layout-block]');
      if (!block) return;
      if (button.dataset.layoutWidth) {
        const width = Number(button.dataset.layoutWidth);
        if (Number.isInteger(width) && width >= 1 && width <= 12) {
          block.querySelector('[data-layout-span]').value = String(width);
          redraw();
        }
      } else {
        const rows = getRows();
        const index = rows.findIndex(row => row.block === block);
        const direction = button.dataset.layoutMove === 'up' ? -1 : button.dataset.layoutMove === 'down' ? 1 : 0;
        const target = rows[index + direction];
        if (direction && target) move(block.dataset.layoutId, target.block.dataset.layoutId, button);
      }
    });
    sketch.addEventListener('dragstart', event => {
      const card = event.target.closest('[data-layout-sketch-item]');
      if (!card || !sketch.contains(card)) return;
      draggedId = card.dataset.layoutSketchItem;
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', draggedId);
      }
    });
    sketch.addEventListener('dragover', event => {
      const card = event.target.closest('[data-layout-sketch-item]');
      if (draggedId && card && sketch.contains(card)) event.preventDefault();
    });
    sketch.addEventListener('drop', event => {
      const card = event.target.closest('[data-layout-sketch-item]');
      if (draggedId && card && sketch.contains(card)) {
        event.preventDefault();
        move(draggedId, card.dataset.layoutSketchItem);
      }
      draggedId = null;
    });
    sketch.addEventListener('dragend', () => { draggedId = null; });
    getRows().forEach(entry => list.append(entry.block));
    redraw();
  });
})();
