/* Optional starting points only. Clicking an idea never submits the form. */
(() => {
  document.querySelectorAll('[data-image-ai-form]').forEach(form => {
    const brief = form.elements.namedItem('brief');
    const status = form.querySelector('[data-image-idea-status]');
    if (!brief) return;
    form.querySelectorAll('[data-image-idea]').forEach(button => button.addEventListener('click', () => {
      const idea = button.dataset.imageIdea || '';
      const proposed = brief.value.trim() ? brief.value.trimEnd() + '\n' + idea : idea;
      if (proposed.length <= 600) {
        brief.value = proposed;
        if (status) status.textContent = form.dataset.ideaAdded;
        brief.focus();
      } else if (status) status.textContent = form.dataset.ideaLong;
    }));
    brief.addEventListener('input', () => { if (status) status.textContent = ''; });
  });
})();
