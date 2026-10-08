// The article list's few behaviours: a row opens its article, the search narrows the rows as it
// is typed, a jobs' progress reloads the list when it ends, an import starts once a file is
// chosen. Every part works without this file (the links, the search form, the import button).
(() => {
  document.addEventListener('click', e => {
    const row = e.target.closest('tr[data-href]');
    if (!row || e.target.closest('a, button, input, label')) return;
    if (e.metaKey || e.ctrlKey) window.open(row.dataset.href, '_blank');
    else location = row.dataset.href;
  });
  const search = document.querySelector('[data-filter-rows]');
  if (search) {
    // the page carries the filter's whole set (R6BD-02): typing narrows it, clearing restores it
    search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      let shown = 0;
      document.querySelectorAll('tr[data-text]').forEach(r => {
        r.hidden = !!q && !r.dataset.text.includes(q);
        if (!r.hidden) shown++;
      });
      const empty = document.querySelector('tr[data-empty]');
      if (empty) {
        empty.hidden = shown > 0;
        const p = empty.querySelector('p');
        if (p) p.textContent = q ? `${p.dataset.noneText.slice(0, -1)} “${search.value.trim()}”.` : p.dataset.emptyText;
      }
    });
  }
  document.querySelectorAll('[data-autosubmit]').forEach(input => input.addEventListener('change', () => {
    if (input.files.length) input.form.submit();
  }));
  // while a job runs, the page follows it. The list: each row's step as it moves, and one
  // reload when the queue is empty (review RA7C-01: the poll reads the whole job list). A run's
  // own page (`data-job-step="run:<slug>"`): a reload when its run moves to another step (the
  // timeline is drawn by the server) and when its run ends, whatever else is queued (RD41-02).
  if (document.querySelector('[data-follow-jobs]')) {
    let done = false;
    const reload = () => { done = true; location.reload(); };
    setInterval(async () => {
      if (done) return;
      let jobs;
      try { jobs = await (await fetch('/api/jobs', {headers: {Accept: 'application/json'}})).json(); }
      catch (err) { return; }
      const active = jobs.filter(j => ['queued', 'running'].includes(j.status));
      if (!active.length) { reload(); return; }
      for (const el of document.querySelectorAll('[data-job-step]')) {
        const run = el.dataset.jobStep.startsWith('run:');
        const slug = run ? el.dataset.jobStep.slice(4) : el.dataset.jobStep;
        const j = active.find(x => x.slug === slug);
        if (run) {
          if (!j || (j.step && el.textContent !== `running: ${j.step}`)) { reload(); return; }
        } else if (j && j.step) el.textContent = j.step;
      }
    }, 4000);
  }
})();
