/* Update the local sketch only; the server validates and signs the selection. */
(() => {
  document.querySelectorAll('[data-visual-ai-editor]').forEach(form => {
    const thumb = form.querySelector('[data-visual-thumbnail]');
    const image = form.querySelector('[data-visual-hero-image]');
    const alt = form.elements.namedItem('hero_alt');
    const fit = form.elements.namedItem('hero_fit');
    const position = form.elements.namedItem('hero_position');
    const feature = form.elements.namedItem('show_feature');
    if (!thumb || !image || !alt || !fit || !position || !feature) return;
    const update = () => {
      const selected = form.querySelector('input[name="hero_asset_id"]:checked');
      const url = selected && selected.dataset.heroUrl;
      image.hidden = !url;
      if (url) image.src = url;
      else image.removeAttribute('src');
      image.style.objectFit = ['cover','contain'].includes(fit.value) ? fit.value : 'cover';
      image.style.objectPosition = ['center','top','bottom','left','right'].includes(position.value) ? position.value : 'center';
      thumb.classList.toggle('has-visual-hero', !!url);
      thumb.classList.toggle('no-feature', !url && !feature.checked);
    };
    form.addEventListener('input', update);
    form.addEventListener('change', event => {
      if (event.target.name === 'hero_asset_id') alt.value = event.target.dataset.heroAlt || '';
      update();
    });
    update();
  });
})();
