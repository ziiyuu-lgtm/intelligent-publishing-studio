/* Display-only viewport, gap and alignment controls; the server validates the submitted form. */
(() => {
  document.querySelectorAll('[data-manual-layout]').forEach(editor => {
    const sketch = editor.querySelector('[data-layout-sketch]');
    const gap = editor.querySelector('[data-layout-gap]');
    const alignment = editor.querySelector('[data-layout-alignment]');
    const buttons = [...editor.querySelectorAll('[data-manual-viewport]')];
    const displayButtons = [...editor.querySelectorAll('[data-manual-display]')];
    if (!sketch || !gap || !alignment) return;
    const update = () => {
      const value = Number(gap.value);
      sketch.style.gap = Number.isInteger(value) && value >= 0 && value <= 64 ? `${value}px` : '';
      sketch.style.alignItems = ['start', 'stretch'].includes(alignment.value) ? alignment.value : '';
    };
    gap.addEventListener('input', update);
    alignment.addEventListener('change', update);
    buttons.forEach(button => button.addEventListener('click', () => {
      const mobile = button.dataset.manualViewport === 'mobile';
      sketch.classList.toggle('manual-layout-sketch--mobile', mobile);
      buttons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    }));
    displayButtons.forEach(button => button.addEventListener('click', () => {
      editor.dataset.layoutContentMode = button.dataset.manualDisplay === 'content' ? 'yes' : 'no';
      displayButtons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      editor.dispatchEvent(new Event('layout-restore', {bubbles:true}));
    }));
    update();
  });
})();
