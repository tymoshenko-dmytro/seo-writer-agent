// Configure's few behaviours: a destructive button asks first (data-confirm), a chosen file sends
// its form. Without this file the forms submit as they are.
(() => {
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-confirm]');
    if (b && !window.confirm(b.dataset.confirm)) e.preventDefault();
  });
  document.querySelectorAll('[data-autosubmit]').forEach(input => input.addEventListener('change', () => {
    if (input.files.length) input.form.submit();
  }));
})();
