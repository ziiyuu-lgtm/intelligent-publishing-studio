/* Optional, local-only helpers for composing an editable visual design brief. */
(() => {
  document.querySelectorAll('[data-visual-brief-editor]').forEach(form => {
    const input = form.querySelector('[data-brief-input]');
    const count = form.querySelector('[data-brief-count]');
    const status = form.querySelector('[data-brief-status]');
    const examples = form.querySelector('[data-brief-examples]');
    if (!input || !count || !status || !examples) return;
    const limit = Number(input.maxLength);
    if (!Number.isInteger(limit) || limit <= 0) return;
    const updateCount = () => { count.textContent = `${Math.max(0, limit - input.value.length)} ${form.dataset.remaining}`; };
    examples.hidden = false;
    form.querySelectorAll('[data-brief-starter]').forEach(button => {
      button.addEventListener('click', () => {
        const text = button.dataset.briefText || '';
        const join = input.value && !input.value.endsWith('\n') ? '\n' : '';
        if (!text || input.value.length + join.length + text.length > limit) {
          status.textContent = form.dataset.tooLong;
          return;
        }
        input.value += join + text;
        updateCount();
        status.textContent = form.dataset.added;
        input.focus();
      });
    });
    input.addEventListener('input', () => { updateCount(); status.textContent = ''; });
    updateCount();
  });
})();
