// The shell's few behaviours: toasts that fade, dialogs, the phone
// menu, and the jobs line that keeps itself current. Every page works without this file.
(() => {
  // a toast stays a few seconds, longer while the pointer or the focus is on it
  const hide = t => { t.classList.add('is-hiding'); setTimeout(() => t.remove(), 250); };
  document.querySelectorAll('.toast').forEach(t => {
    let timer = setTimeout(() => hide(t), 6000);
    const hold = () => clearTimeout(timer);
    const later = () => { clearTimeout(timer); timer = setTimeout(() => hide(t), 3000); };
    t.addEventListener('mouseenter', hold); t.addEventListener('focusin', hold);
    t.addEventListener('mouseleave', later); t.addEventListener('focusout', later);
  });

  // the "⋯" menus (<details>): one open at a time, closed by a click elsewhere or by Escape
  const menus = () => document.querySelectorAll('details.menu[open]');
  document.addEventListener('click', e => {
    menus().forEach(m => { if (!m.contains(e.target)) m.removeAttribute('open'); });
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    menus().forEach(m => {
      const inside = m.contains(document.activeElement);
      m.removeAttribute('open');
      if (inside) m.querySelector('summary').focus();
    });
  });

  document.addEventListener('click', e => {
    const close = e.target.closest('.toast-close');
    if (close) { hide(close.closest('.toast')); return; }
    // a dialog opener is a link to a page that asks the same question: with JS, the dialog
    const opener = e.target.closest('[data-dialog]');
    if (opener) {
      const d = document.getElementById(opener.dataset.dialog);
      if (d && typeof d.showModal === 'function') { e.preventDefault(); d.showModal(); }
      return;
    }
    const closer = e.target.closest('[data-dialog-close]');
    if (closer) { const d = closer.closest('dialog'); if (d) d.close(); }
  });

  // phone width: the sidebar is a top bar, its menu opens below it
  const sidebar = document.getElementById('sidebar');
  const toggle = sidebar && sidebar.querySelector('.sidebar-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const open = sidebar.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
  }

  // the jobs line: while something runs, it follows the job list (every active job is in it,
  // R734-01), naming what shell.jobs_line names: the running job, else the oldest queued
  const line = document.querySelector('[data-jobs]');
  if (line && Number(line.dataset.jobs) > 0) {
    const text = line.querySelector('.foot-text');
    const dot = line.querySelector('.pulse');
    const tick = async () => {
      let jobs;
      try { jobs = await (await fetch('/api/jobs', {headers: {Accept: 'application/json'}})).json(); }
      catch (err) { return; }
      const active = jobs.filter(j => ['queued', 'running'].includes(j.status));
      if (!active.length) {
        text.textContent = 'No jobs running';
        dot.classList.add('is-idle');
        line.setAttribute('href', '/');
        clearInterval(timer);
        return;
      }
      const first = active.find(j => j.status === 'running') || active[active.length - 1];
      const label = first.kind === 'article' ? `Writing “${first.topic}”` : first.topic;
      text.textContent = `${active.length} job${active.length > 1 ? 's' : ''} · ${label}`;
      // the text and the link move together: a job without its folder yet links home (R734-02)
      line.setAttribute('href', first.slug ? `/a/${first.slug}` : '/');
    };
    const timer = setInterval(tick, 5000);
  }
})();
