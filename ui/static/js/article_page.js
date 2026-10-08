// The article page's few behaviours. Every action is a
// form that works without this file; with it: a job is followed until it ends, Not important
// and Restore ask in a dialog, a paragraph is written in the block editor in its card, ticked
// proposals are counted with their cost, and the SEO form follows its fields and asks before
// unsaved changes are left.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // a link to a folded section (History's old checklist) opens it: on arriving with its
  // fragment, and when a link on this very page points at it (the same fragment again included)
  const openFold = id => {
    const target = id && document.getElementById(id);
    const fold = target && target.closest && target.closest('details');
    if (fold) fold.open = true;
  };
  openFold(location.hash && decodeURIComponent(location.hash.slice(1)));
  document.addEventListener('click', e => {
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    const href = a ? a.getAttribute('href') : '';
    const at = href.indexOf('#');
    if (at >= 0 && [location.search, ''].includes(href.slice(0, at))) openFold(decodeURIComponent(href.slice(at + 1)));
  });

  // jobs working on the article: the page reloads when one of them ends (or the next starts),
  // to show each result as it lands while the rest of the queue goes on
  const follow = $('[data-follow-job]');
  if (follow) {
    const slug = follow.dataset.followJob;
    const ids = (follow.dataset.followIds || '').split(' ').filter(Boolean);
    const running = jobs => jobs.filter(j => j.slug === slug && j.status === 'running').map(j => j.id).join(' ');
    let first = null;
    const timer = setInterval(async () => {
      let jobs;
      try { jobs = await (await fetch('/api/jobs', {headers: {Accept: 'application/json'}})).json(); }
      catch (err) { return; }
      const active = jobs.filter(j => j.slug === slug && ['queued', 'running'].includes(j.status));
      const now = running(jobs);
      if (first === null) first = now;
      if (!active.length || ids.some(id => !active.some(j => j.id === id)) || now !== first) {
        clearInterval(timer);
        location.reload();
      }
    }, 4000);
  }

  // a card's change asked for: the card says so at once, before the page comes back
  document.addEventListener('submit', e => {
    const card = e.target.closest('.ritem, .pic');
    if (card && !e.defaultPrevented) card.classList.add('is-leaving');
  });

  // Restore this version: its confirmation
  document.addEventListener('click', e => {
    const restore = e.target.closest('[data-restore]');
    const rdialog = $('#restore-dialog');
    if (restore && rdialog && typeof rdialog.showModal === 'function') {
      e.preventDefault();
      $('#restore-form').action = restore.dataset.restore;
      rdialog.showModal();
    }
  });

  // a paragraph written by the person, in the block editor (TipTap, loaded on first use); the
  // text box under it is the fallback and what the form sends
  const scripts = $('.article-layout');
  let loading = null;
  const loadEditor = () => {
    if (window.MdBlocks) return Promise.resolve();
    if (!loading) {
      const add = src => new Promise((ok, fail) => {
        const s = document.createElement('script');
        s.src = src; s.onload = ok; s.onerror = fail;
        document.head.appendChild(s);
      });
      loading = add(scripts.dataset.tiptap).then(() => add(scripts.dataset.mdblocks));
    }
    return loading;
  };
  const open = (card) => {
    const form = $('form[data-editor]', card);
    if (!form || !form.hidden) return;
    const context = $('[data-context]', card);
    const foot = $('.ritem-foot', card);
    form.hidden = false;
    if (foot) foot.hidden = true;
    const md = form.elements.after;
    loadEditor().then(() => {
      if (form.hidden) return;
      const host = document.createElement('div');
      host.className = 'ritem-tiptap prose';
      form.insertBefore(host, md);
      const editor = new window.TT.Editor({ element: host,
        extensions: window.MdBlocks.extensions({ assetBase: scripts.dataset.assets }) });
      const state = window.MdBlocks.load(editor, md.value);
      md.hidden = true;
      if (context) context.hidden = true;
      form._editor = { editor, state, host };
      editor.commands.focus('end');
    }).catch(() => { md.focus(); });
  };
  const close = (card) => {
    const form = $('form[data-editor]', card);
    if (!form) return;
    if (form._editor) { form._editor.editor.destroy(); form._editor.host.remove(); form._editor = null; }
    form.elements.after.hidden = false;
    form.elements.after.value = form.elements.before.value;
    form.hidden = true;
    const context = $('[data-context]', card);
    const foot = $('.ritem-foot', card);
    if (context) context.hidden = false;
    if (foot) foot.hidden = false;
  };
  document.addEventListener('click', e => {
    const start = e.target.closest('[data-edit]');
    if (start) {
      const card = document.getElementById(start.dataset.edit);
      if (card && $('form[data-editor]', card)) { e.preventDefault(); open(card); }
      return;
    }
    const cancel = e.target.closest('[data-edit-cancel]');
    if (cancel) {
      const card = cancel.closest('.ritem');
      const form = card && $('form[data-editor]', card);
      if (form && !new URLSearchParams(location.search).get('edit')) { e.preventDefault(); close(card); }
    }
  });
  document.addEventListener('submit', e => {
    const form = e.target;
    if (form.matches('form[data-editor]') && form._editor) {
      form.elements.after.value = window.MdBlocks.save(form._editor.editor, form._editor.state).trim();
    }
  });

  // a picture opens large in a dialog (Pictures, the review's cards); a click outside closes it
  const view = $('#pic-view');
  if (view && typeof view.showModal === 'function') {
    document.addEventListener('click', e => {
      const z = e.target.closest('[data-zoom]');
      if (!z) return;
      e.preventDefault();
      $('img', view).src = z.dataset.zoom;
      $('img', view).alt = ($('img', z) || {}).alt || '';
      view.showModal();
    });
    view.addEventListener('click', e => { if (e.target === view) view.close(); });
  }

  // Redraw… / Re-capture…: its form opens under the row of actions, which stays where it is
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-redo]');
    if (!b) return;
    const form = document.getElementById(b.dataset.redo);
    if (!form) return;
    const open = !form.classList.contains('is-open');
    form.classList.toggle('is-open', open);
    b.setAttribute('aria-expanded', String(open));
    if (open) $('input[name=feedback]', form).focus();
  });

  // your own picture: it goes in as soon as it is chosen (the form posts without JavaScript too)
  $$('form[data-upload] input[type=file]').forEach(input => input.addEventListener('change', () => {
    if (input.files.length) input.form.submit();
  }));

  // the ticked proposals: counted, with what they cost together
  const batch = $('#batch');
  if (batch) {
    const button = $('#apply-ticked');
    const each = parseFloat((button.dataset.cost || '').replace(/[^0-9.]/g, '')) || 0;
    const picks = () => $$('input[name=ids][form=batch]');
    const all = $('[data-select-all]', batch);
    // one primary per screen: while proposals are ticked, applying them is it, not Accept
    const accept = $('#accept-version');
    const acceptPrimary = accept && accept.classList.contains('btn-primary');
    const update = () => {
      const on = picks().filter(p => p.checked);
      const paid = on.filter(p => p.dataset.paid === '1').length;
      // a ticked proposal whose key the install lacks keeps the batch off, and says which key;
      // a picture's removal is code and needs none
      const nokey = on.map(p => p.dataset.nokey).find(Boolean);
      button.disabled = !on.length || Boolean(nokey);
      const why = $('#apply-nokey');
      if (why) { why.hidden = !nokey; why.textContent = nokey || ''; }
      button.classList.toggle('btn-primary', on.length > 0);
      button.classList.toggle('btn-secondary', !on.length);
      if (acceptPrimary) {
        accept.classList.toggle('btn-primary', !on.length);
        accept.classList.toggle('btn-secondary', on.length > 0);
      }
      const label = $('span', button);
      label.textContent = on.length ? `Apply the ${on.length} ticked proposal${on.length > 1 ? 's' : ''}` : 'Apply the ticked proposals';
      let cost = $('.btn-cost', button);
      if (paid && each) {
        if (!cost) { cost = document.createElement('span'); cost.className = 'btn-cost'; button.appendChild(cost); }
        cost.textContent = `· about $${(paid * each).toFixed(2)}`;
      } else if (cost) cost.remove();
      all.checked = on.length > 0 && on.length === picks().length;
    };
    all.addEventListener('change', () => { picks().forEach(p => { p.checked = all.checked; }); update(); });
    document.addEventListener('change', e => { if (e.target.matches('input[name=ids][form=batch]')) update(); });
    update();
  }

  // SEO: the preview and the counters follow the fields; leaving unsaved changes asks
  const seo = $('#seo-form');
  if (seo) {
    let dirty = seo.hasAttribute('data-dirty'), leaveTo = null;     // a refused Save: what was typed is not saved
    const state = $('#seo-state');
    const changed = () => { dirty = true; state.textContent = 'Unsaved changes'; };
    seo.addEventListener('input', e => {
      changed();
      const inp = e.target;
      const out = inp.dataset.live && $(`[data-serp="${inp.dataset.live}"]`);
      if (out) out.textContent = inp.value || (inp.dataset.live === 'slug' ? '' : '(empty)');
      const c = $(`[data-count="${inp.id}"]`);
      if (c) {
        const n = inp.value.length, lo = +c.dataset.min, hi = +c.dataset.max;
        c.textContent = `${n} of ${lo}–${hi}`;
        c.classList.toggle('is-over', n < lo || n > hi);
      }
    });
    seo.addEventListener('submit', () => { dirty = false; });
    $('#faq-add').addEventListener('click', () => {
      const node = $('#faq-new').content.cloneNode(true);
      $('#faq-list').appendChild(node);
      changed();
      const items = $$('#faq-list .faq-item');
      $('input', items[items.length - 1]).focus();
    });
    seo.addEventListener('click', e => {
      const del = e.target.closest('[data-faq-delete]');
      if (del) { del.closest('.faq-item').remove(); changed(); }
    });
    document.addEventListener('click', e => {
      const a = e.target.closest('a[href]');
      if (!dirty || !a || a.closest('dialog') || a.target === '_blank' || a.getAttribute('href').startsWith('#')) return;
      e.preventDefault(); leaveTo = a.href; $('#seo-leave').showModal();
    });
    $('#seo-leave-go').addEventListener('click', () => { dirty = false; if (leaveTo) location = leaveTo; });
    window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  }
})();
