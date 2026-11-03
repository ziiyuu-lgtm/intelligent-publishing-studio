/* A local, read-only copy of the reviewed site at real CSS viewport widths. */
document.addEventListener('DOMContentLoaded', () => {
  const panel = document.querySelector('[data-visual-device-review]');
  const site = document.querySelector('.journal-site');
  const frame = panel && panel.querySelector('[data-visual-device-frame]');
  if (!panel || !site || !frame || !('srcdoc' in frame)) return;

  // Copy only the rendered website and trusted page styles: no forms, review
  // tokens, scripts or editable controls enter the sandboxed preview.
  const page = document.implementation.createHTMLDocument('Website preview');
  page.documentElement.lang = document.documentElement.lang || 'en';
  const viewport = page.createElement('meta');
  viewport.name = 'viewport';
  viewport.content = 'width=device-width,initial-scale=1';
  page.head.appendChild(viewport);
  document.querySelectorAll('head link[rel="stylesheet"], head style').forEach((node) => {
    const copy = node.cloneNode(true);
    if (copy.tagName === 'LINK') copy.href = node.href;
    page.head.appendChild(copy);
  });
  const reset = page.createElement('style');
  reset.textContent = 'html,body{margin:0;padding:0;background:#fff}.journal-site{border:0;border-radius:0}';
  page.head.appendChild(reset);
  const snapshot = site.cloneNode(true);
  snapshot.querySelectorAll('a').forEach((link) => {
    link.removeAttribute('href');
    link.setAttribute('aria-disabled', 'true');
    link.setAttribute('tabindex', '-1');
  });
  page.body.appendChild(snapshot);
  frame.srcdoc = '<!doctype html>' + page.documentElement.outerHTML;

  const widths = {desktop: 1080, tablet: 768, mobile: 390};
  const buttons = panel.querySelectorAll('[data-visual-device]');
  const select = (name) => {
    if (!Object.prototype.hasOwnProperty.call(widths, name)) return;
    frame.style.width = widths[name] + 'px';
    buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.visualDevice === name)));
  };
  buttons.forEach((button) => button.addEventListener('click', () => select(button.dataset.visualDevice)));
  select('desktop');
  site.hidden = true;
  panel.hidden = false;
});
