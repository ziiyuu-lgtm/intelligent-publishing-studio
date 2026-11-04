// The fictional test link stores its token in the URL fragment, which is not
// part of the server request. Remove it before allowing a CSRF-protected POST.
(() => {
  const form = document.querySelector('[data-test-mail-confirmation]');
  if (!form) return;
  const token = window.location.hash.slice(1);
  try {
    window.history.replaceState(null, '', window.location.pathname);
  } catch (_) {
    return;
  }
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return;
  form.elements.namedItem('token').value = token;
  form.querySelector('[type=submit]').disabled = false;
})();
