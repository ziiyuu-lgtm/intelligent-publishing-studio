/* A text-only sketch follows edits; the server still validates every submission. */
(() => {
  document.querySelectorAll('[data-layout-editor]').forEach(editor => {
    const sketch = editor.querySelector('[data-layout-sketch]');
    const redraw = () => {
      const rows = [...editor.querySelectorAll('[data-layout-block]')].map((block, index) => {
        const order = Number(block.querySelector('[data-layout-order]').value);
        const span = Number(block.querySelector('[data-layout-span]').value);
        return {block, index, order: Number.isInteger(order) && order > 0 ? order : index + 1,
          span: Number.isInteger(span) && span >= 1 && span <= 12 ? span : 12};
      }).sort((a, b) => a.order - b.order || a.index - b.index);
      const cards = rows.filter(({block}) => block.dataset.layoutHidden !== 'yes').map(({block, span}) => {
        const card = document.createElement('div');
        card.className = 'layout-sketch__block';
        card.style.gridColumn = `span ${span}`;
        card.textContent = block.dataset.layoutLabel;
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
    editor.addEventListener('input', redraw);
    redraw();
  });
})();
