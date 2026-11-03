/* Approximate display only; the server validates the reviewed design. */
(() => {
  document.querySelectorAll('[data-visual-ai-editor]').forEach(form => {
    const thumb = form.querySelector('[data-visual-thumbnail]');
    const field = name => form.elements.namedItem(name);
    const names = ['primary','surface','background','hero_style','composition','alignment','section_order','show_feature'];
    if (!thumb || names.some(name => !field(name))) return;
    const contrast = colour => {
      const channels = [1,3,5].map(i => parseInt(colour.slice(i,i+2),16)/255)
        .map(v => v <= 0.04045 ? v/12.92 : ((v+0.055)/1.055)**2.4);
      const lum = 0.2126*channels[0]+0.7152*channels[1]+0.0722*channels[2];
      return (lum+0.05)/0.05 >= 1.05/(lum+0.05) ? '#000000' : '#ffffff';
    };
    const setChoice = (prefix, value, allowed) => {
      allowed.forEach(item => thumb.classList.remove(prefix+item));
      if (allowed.includes(value)) thumb.classList.add(prefix+value);
    };
    const update = () => {
      const primary=field('primary').value, surface=field('surface').value, background=field('background').value;
      if (![primary,surface,background].every(value => /^#[0-9a-fA-F]{6}$/.test(value))) return;
      const hero=field('hero_style').value === 'soft' ? surface : primary;
      const styles={'--thumb-primary':primary,'--thumb-surface':surface,'--thumb-page':background,
        '--thumb-hero':hero,'--thumb-ink':contrast(hero),'--thumb-page-ink':contrast(background),
        '--thumb-surface-ink':contrast(surface)};
      Object.entries(styles).forEach(([key,value]) => thumb.style.setProperty(key,value));
      setChoice('composition-',field('composition').value,['classic','editorial','split','immersive','minimal','grid']);
      setChoice('align-',field('alignment').value,['left','center']);
      setChoice('order-',field('section_order').value,['about_first','archive_first']);
      thumb.classList.toggle('no-feature',!field('show_feature').checked);
    };
    form.addEventListener('input',update);
    form.addEventListener('change',update);
    update();
  });
})();
