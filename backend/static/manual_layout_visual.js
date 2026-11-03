/* Display-only viewport, gap and alignment controls; the server validates the submitted form. */
(() => {
  document.querySelectorAll('[data-manual-layout]').forEach(editor => {
    const sketch = editor.querySelector('[data-layout-sketch]');
    const gap = editor.querySelector('[name="settings-gap"]');
    const alignment = editor.querySelector('[name="settings-alignment"]');
    const buttons = [...editor.querySelectorAll('[data-manual-viewport]')];
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
    update();
  });
})();
