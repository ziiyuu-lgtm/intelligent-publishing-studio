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
