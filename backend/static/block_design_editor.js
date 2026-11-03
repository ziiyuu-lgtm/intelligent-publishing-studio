/* Keep the accessible hex input and native colour picker in sync, including new form cards. */
(() => {
  document.addEventListener('input', event => {
    const target = event.target;
    if (target.matches('[data-block-colour-picker]')) {
      const field = document.getElementById(target.dataset.target);
      if (field) field.value = target.value;
    } else if (target.matches('[data-block-colour-input]') && /^#[0-9a-f]{6}$/i.test(target.value)) {
      const wrapper = target.closest('[data-block-colour-field]');
      const picker = wrapper && wrapper.querySelector('[data-block-colour-picker]');
      if (picker) picker.value = target.value;
    }
  });
})();
/* Optional width shortcuts leave the numeric field available without JavaScript. */
(() => {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-grid-span-value]');
    if (!button) return;
    const field = document.getElementById(button.dataset.target);
    const value = Number(button.dataset.gridSpanValue);
    if (field && Number.isInteger(value) && value >= 1 && value <= 12) field.value = String(value);
  });
})();
