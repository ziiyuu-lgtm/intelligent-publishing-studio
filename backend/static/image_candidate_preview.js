/* A changed control must be previewed before it can be applied. */
document.addEventListener('DOMContentLoaded', () => {
  const controls = document.querySelector('[data-image-preview-controls]');
  const apply = document.querySelector('[data-image-preview-apply]');
  const button = apply && apply.querySelector('[data-image-apply-button]');
  const hint = apply && apply.querySelector('[data-image-update-hint]');
  if (!controls || !apply || !button || !hint) return;
  const fit = controls.elements.namedItem('fit');
  const position = controls.elements.namedItem('position');
  if (!fit || !position) return;
  const page = controls.elements.namedItem('page'), target = controls.elements.namedItem('target');
  let displayedFit = fit.value, displayedPosition = position.value, displayedPage = page && page.value,
      displayedTarget = target && target.value;
  const update = () => {
    const changed = fit.value !== displayedFit || position.value !== displayedPosition ||
      !!(page && page.value !== displayedPage) || !!(target && target.value !== displayedTarget);
    button.disabled = changed;
    hint.hidden = !changed;
  };
  fit.addEventListener('change', update);
  position.addEventListener('change', update);
  if (page) page.addEventListener('change', update);
  if (target) target.addEventListener('change', update);
  // Used by the separate static demonstration after its local-only preview update.
  controls.addEventListener('ips-preview-updated', () => {
    displayedFit = fit.value; displayedPosition = position.value; displayedPage = page && page.value;
    displayedTarget = target && target.value; update();
  });
});
