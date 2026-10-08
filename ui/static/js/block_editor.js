// The block editor. The page is a form with the article's
// Markdown in a text box: without this file it is a Markdown editor with Save. With it, the text
// box becomes the article as blocks (TipTap over its Markdown, ui/static/js/mdblocks.js): a
// gutter beside the block under the pointer adds a block below ("+", the same menu as "/") or
// moves and deletes it; a selection gets a bar (bold, italic, link, H2, H3, Edit with AI); an AI
// edit waits as before → after for Apply; a picture gets its own bar (alt text, Redraw, Remove).
// Save is explicit (the button, or ⌘S) and is a version of its own; the text box carries what
// is saved, written by MdBlocks.save so unchanged blocks keep their bytes. Leaving with unsaved
// changes asks first.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const form = $('#editor-form');
  const md = $('#editor-md');
  const ui = $('#editor-ui');
  if (!form || !md || !window.TT || !window.MdBlocks) return;

  const slug = ui.dataset.slug;
  // what is saved: the text the page was opened with, or, after a refused save, the text on the
  // disk (the person's own text in the box is then still unsaved; review RF9C-01)
  const savedEl = document.querySelector('#editor-saved');
  const saved = savedEl ? savedEl.value : md.value;
  const host = document.createElement('div');
  host.className = 'editor-host';
  md.parentNode.insertBefore(host, md);
  md.hidden = true;
  document.body.appendChild(ui);
  ui.hidden = false;

  const editor = new window.TT.Editor({
    element: host,
    extensions: window.MdBlocks.extensions({ assetBase: ui.dataset.assets }),
    editorProps: { attributes: { class: 'prose editor-prose', 'aria-label': 'The article', spellcheck: 'true' } },
  });
  let state = window.MdBlocks.load(editor, md.value);
  let view = 'blocks';
  const current = () => (view === 'blocks' ? window.MdBlocks.save(editor, state) : md.value);

  // ── the saving state ────────────────────────────────────────────────────────────────────────
  const stateEl = $('#editor-state');
  const saveBtn = $('#editor-save');
  const first = stateEl.innerHTML;
  let dirty = false;
  const refresh = () => {
    const now = current();
    dirty = now !== saved || !!$('input[name=overwrite]:checked');
    saveBtn.disabled = !dirty;
    stateEl.innerHTML = dirty ? '<span class="status is-yellow">Unsaved changes</span>' : first;
  };
  let pending = null;
  const later = () => { clearTimeout(pending); pending = setTimeout(refresh, 250); };
  editor.on('update', later);
  md.addEventListener('input', later);
  form.addEventListener('change', later);
  refresh();

  form.addEventListener('submit', e => {
    clearTimeout(pending);
    refresh();
    if (!dirty) { e.preventDefault(); return; }       // nothing to save: no version for nothing
    md.value = current();
    dirty = false;
  });
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      clearTimeout(pending);
      refresh();
      if (dirty) form.requestSubmit();
    }
  });

  // ── leaving with unsaved changes asks ───────────────────────────────────────────────────────
  let leaveTo = null;
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href]');
    if (!dirty || !a || a.closest('dialog') || a.target === '_blank' || a.getAttribute('href').startsWith('#')) return;
    e.preventDefault(); leaveTo = a.href; $('#leave-dialog').showModal();
  });
  $('#leave-go').addEventListener('click', () => { dirty = false; if (leaveTo) location = leaveTo; });
  window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  // ── Blocks / Markdown ───────────────────────────────────────────────────────────────────────
  $$('[data-view]').forEach(b => b.addEventListener('click', () => {
    const want = b.dataset.view;
    if (want === view) return;
    if (want === 'markdown') {
      md.value = window.MdBlocks.save(editor, state);
      host.hidden = true; md.hidden = false;
      md.style.height = Math.max(400, md.scrollHeight) + 'px';
    } else {
      state = window.MdBlocks.load(editor, md.value);
      // blocks the person did not touch in Markdown still save as the text they came from
      host.hidden = false; md.hidden = true;
    }
    view = want;
    $$('[data-view]').forEach(x => x.setAttribute('aria-current', String(x.dataset.view === view)));
    hideAll();
    refresh();
  }));

  // ── floating pieces ─────────────────────────────────────────────────────────────────────────
  const gutter = $('#gutter'), blockMenu = $('#block-menu'), slash = $('#slash-menu');
  const selBar = $('#sel-bar'), picBar = $('#pic-bar'), rawBar = $('#raw-bar'), ask = $('#ask');
  // `above`: where to go when there is no room below (the top of the line it belongs to)
  const place = (el, x, y, above = null) => {
    el.hidden = false;
    const w = el.offsetWidth, h = el.offsetHeight;
    const top = above !== null && y + h > window.innerHeight - 8 && above - h - 6 > 8 ? above - h - 6 : y;
    el.style.left = `${Math.max(8, Math.min(x, window.innerWidth - w - 8)) + window.scrollX}px`;
    el.style.top = `${top + window.scrollY}px`;
  };
  const hideAll = (except) => [gutter, blockMenu, slash, selBar, picBar, rawBar].forEach(el => { if (el !== except) el.hidden = true; });

  // the top-level block at a document position: its start and its node
  const blockAt = (pos) => {
    const $pos = editor.state.doc.resolve(Math.min(pos, editor.state.doc.content.size));
    if ($pos.depth === 0) {
      const node = editor.state.doc.nodeAt(pos);
      return node ? { from: pos, node } : null;
    }
    const from = $pos.before(1);
    return { from, node: editor.state.doc.nodeAt(from) };
  };
  let gutterBlock = null;
  host.addEventListener('mousemove', e => {
    if (view !== 'blocks' || !blockMenu.hidden || !slash.hidden) return;
    const at = editor.view.posAtCoords({ left: e.clientX, top: e.clientY });
    if (!at) return;
    const b = blockAt(at.inside >= 0 ? at.inside : at.pos);
    if (!b || !b.node) return;
    const dom = editor.view.nodeDOM(b.from);
    if (!(dom instanceof HTMLElement)) return;
    const r = dom.getBoundingClientRect();
    gutterBlock = b;
    place(gutter, host.getBoundingClientRect().left - 56, r.top + 2);
  });
  host.addEventListener('mouseleave', e => {
    if (!gutter.contains(e.relatedTarget) && blockMenu.hidden) gutter.hidden = true;
  });

  // ── moving and deleting blocks ──────────────────────────────────────────────────────────────
  const blocks = () => {
    const out = [];
    editor.state.doc.forEach((node, offset) => out.push({ from: offset, to: offset + node.nodeSize, node }));
    return out;
  };
  const sectionOf = (i, list) => {
    // a heading with everything up to the next heading of its level or higher
    const level = list[i].node.attrs.level;
    let j = i + 1;
    while (j < list.length && !(list[j].node.type.name === 'heading' && list[j].node.attrs.level <= level)) j++;
    return [i, j];
  };
  const move = (range, dir) => {
    const list = blocks();
    const [i, j] = range;
    const { tr } = editor.state;
    const slice = editor.state.doc.slice(list[i].from, list[j - 1].to);
    if (dir < 0) {
      if (i === 0) return;
      let k = i - 1;
      if (range.section) { while (k > 0 && !(list[k].node.type.name === 'heading' && list[k].node.attrs.level <= list[i].node.attrs.level)) k--; }
      tr.delete(list[i].from, list[j - 1].to).insert(list[k].from, slice.content);
    } else {
      if (j >= list.length) return;
      let k = j + 1;
      if (range.section) { [, k] = sectionOf(j, list); if (list[j].node.type.name !== 'heading') k = j + 1; }
      const at = list[k - 1].to;
      tr.insert(at, slice.content).delete(list[i].from, list[j - 1].to);
    }
    editor.view.dispatch(tr.scrollIntoView());
  };
  gutter.addEventListener('click', e => {
    const b = e.target.closest('[data-gutter]');
    if (!b || !gutterBlock) return;
    const r = gutter.getBoundingClientRect();
    if (b.dataset.gutter === 'add') {
      const at = gutterBlock.from + gutterBlock.node.nodeSize;
      editor.chain().insertContentAt(at, { type: 'paragraph' }).setTextSelection(at + 1).focus().run();
      hideAll();
      openSlash();
    } else {
      $$('[data-heading-only]', blockMenu).forEach(x => { x.hidden = gutterBlock.node.type.name !== 'heading'; });
      place(blockMenu, r.left, r.bottom + 4, r.top);
      $('button:not([hidden])', blockMenu).focus();
    }
  });
  blockMenu.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b || !gutterBlock) return;
    const list = blocks();
    const i = list.findIndex(x => x.from === gutterBlock.from);
    if (i < 0) return;
    const act = b.dataset.act;
    if (act === 'delete') {
      editor.chain().deleteRange({ from: list[i].from, to: list[i].to }).focus().run();
    } else if (act === 'up' || act === 'down') {
      move([i, i + 1], act === 'up' ? -1 : 1);
    } else {
      const range = sectionOf(i, list);
      range.section = true;
      move(range, act === 'section-up' ? -1 : 1);
    }
    hideAll();
  });

  // ── "/" and "+": the menu of blocks ─────────────────────────────────────────────────────────
  let slashFrom = null, active = 0;
  const items = () => $$('.slash-item', slash);
  const mark = () => items().forEach((x, i) => x.classList.toggle('is-active', i === active));
  const openSlash = () => {
    const { from } = editor.state.selection;
    const c = editor.view.coordsAtPos(from);
    slashFrom = from;
    active = 0; mark();
    place(slash, c.left, c.bottom + 6, c.top);
  };
  editor.on('update', ({ transaction }) => {
    if (view !== 'blocks' || !transaction.docChanged) return;
    const { $from, empty } = editor.state.selection;
    const text = $from.parent.textContent;
    if (empty && $from.parent.type.name === 'paragraph' && text === '/' ) openSlash();
    else if (!slash.hidden && !text.startsWith('/')) slash.hidden = true;
  });
  const choose = (key) => {
    // the "/…" typed is taken out first; the block's bounds are read again after that, so a
    // replacement never reaches into the next block (review RF9C-02)
    const start = editor.state.selection.$from.before();
    const typed = editor.state.doc.nodeAt(start);
    if (typed && typed.textContent.startsWith('/')) {
      editor.view.dispatch(editor.state.tr.delete(start + 1, start + 1 + typed.textContent.length));
    }
    const node = editor.state.doc.nodeAt(start);
    const end = start + (node ? node.nodeSize : 0);
    const empty = node && node.type.name === 'paragraph' && !node.textContent;
    const chain = editor.chain().focus().setTextSelection(start + 1);
    if (key === 'text') chain.setParagraph().run();
    else if (key === 'h2') chain.setHeading({ level: 2 }).run();
    else if (key === 'h3') chain.setHeading({ level: 3 }).run();
    else if (key === 'ul') chain.toggleBulletList().run();
    else if (key === 'ol') chain.toggleOrderedList().run();
    else if (key === 'quote') chain.toggleBlockquote().run();
    else if (key === 'table') {
      const md = '| Column | Column |\n| --- | --- |\n| A cell | A cell |\n';
      const raw = { type: 'rawBlock', attrs: { md } };
      // an empty block becomes the table; one with words keeps them and the table goes after it
      editor.chain().insertContentAt(empty ? { from: start, to: end } : end, raw).run();
    } else if (key === 'picture') {
      askPicture(empty ? start : end);
    }
    slash.hidden = true;
  };
  slash.addEventListener('click', e => { const b = e.target.closest('[data-block]'); if (b) choose(b.dataset.block); });
  document.addEventListener('keydown', e => {
    if (slash.hidden) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); active = (active + 1) % items().length; mark(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = (active - 1 + items().length) % items().length; mark(); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(items()[active].dataset.block); }
    else if (e.key === 'Escape') { slash.hidden = true; }
  }, true);

  // ── the selection bar ───────────────────────────────────────────────────────────────────────
  editor.on('selectionUpdate', () => {
    const sel = editor.state.selection;
    rawBar.hidden = true;
    if (sel.node && sel.node.type.name === 'rawBlock') {
      selBar.hidden = true; picBar.hidden = true;
      const dom = editor.view.nodeDOM(sel.from);
      if (dom instanceof HTMLElement) { const r = dom.getBoundingClientRect(); place(rawBar, r.left, r.top - 44); }
      return;
    }
    if (sel.node && sel.node.type.name === 'image') {
      selBar.hidden = true;
      const dom = editor.view.nodeDOM(sel.from);
      if (dom instanceof HTMLElement) { const r = dom.getBoundingClientRect(); place(picBar, r.left, r.bottom + 6); }
      return;
    }
    picBar.hidden = true;
    if (sel.empty || sel.node) { selBar.hidden = true; return; }
    const c = editor.view.coordsAtPos(sel.from);
    place(selBar, c.left, c.top - 44);
  });
  selBar.addEventListener('mousedown', e => e.preventDefault());       // the selection stays
  selBar.addEventListener('click', e => {
    const b = e.target.closest('[data-mark]');
    if (!b) return;
    const c = editor.chain().focus();
    if (b.dataset.mark === 'bold') c.toggleBold().run();
    else if (b.dataset.mark === 'italic') c.toggleItalic().run();
    else if (b.dataset.mark === 'h2') c.toggleHeading({ level: 2 }).run();
    else if (b.dataset.mark === 'h3') c.toggleHeading({ level: 3 }).run();
    else if (b.dataset.mark === 'link') askLink();
    else if (b.dataset.mark === 'ai') askAI();
  });

  // ── a small request box: a link, an AI edit, a picture ──────────────────────────────────────
  const askInput = $('#ask-input'), askText = $('#ask-text'), askResult = $('#ask-result'), askActions = $('#ask-actions');
  const button = (label, kind, fn) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = kind === 'link' ? 'text-action' : `btn btn-${kind} btn-sm`;
    b.innerHTML = `<span>${label}</span>`;
    b.addEventListener('click', fn);
    return b;
  };
  const openAsk = (title, cost, placeholder, value, actions, multiline = false) => {
    $('#ask-title').textContent = title;
    $('#ask-cost').textContent = cost || '';
    askInput.hidden = multiline; askText.hidden = !multiline;
    askInput.placeholder = placeholder; askInput.value = multiline ? '' : (value || '');
    askText.value = multiline ? (value || '') : '';
    askResult.hidden = true; askResult.innerHTML = '';
    askActions.replaceChildren(...actions);
    const c = editor.view.coordsAtPos(editor.state.selection.to);
    selBar.hidden = true;
    ask.hidden = false;
    place(ask, Math.max(16, c.left - 40), c.bottom + 8, c.top);
    (multiline ? askText : askInput).focus();
  };
  const closeAsk = () => { ask.hidden = true; editor.commands.focus(); };
  askText.addEventListener('keydown', e => { if (e.key === 'Escape') closeAsk(); });
  askInput.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeAsk();
    if (e.key === 'Enter') { e.preventDefault(); const go = $('.btn-primary, .btn-secondary', askActions); if (go) go.click(); }
  });

  const askLink = () => {
    const was = editor.getAttributes('link').href || '';
    openAsk('Link', '', 'https://…', was, [
      button('Apply', 'secondary', () => {
        const href = askInput.value.trim();
        const c = editor.chain().focus().extendMarkRange('link');
        (href ? c.setLink({ href }) : c.unsetLink()).run();
        closeAsk();
      }),
      button('Remove the link', 'link', () => { editor.chain().focus().extendMarkRange('link').unsetLink().run(); closeAsk(); }),
      button('Cancel', 'link', closeAsk),
    ]);
  };

  // a job: started, then followed until it ends (the same routes the Studio's jobs use)
  const job = async (path, payload, note) => {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                                  body: JSON.stringify(payload) });
    if (r.redirected || r.status === 401) throw new Error('The session ended: reload the page.');
    const start = await r.json().catch(() => ({}));
    if (!r.ok || start.error) throw new Error(start.error || `The request failed (${r.status}).`);
    if (!start.job) return start;
    for (let i = 0; i < 240; i++) {
      await new Promise(ok => setTimeout(ok, 2000));
      const j = await (await fetch('/api/job/' + start.job)).json();
      if (note && j.step) note(j.step);
      if (j.status === 'done') return j.result || {};
      if (j.status === 'failed') throw new Error(j.error || 'The job failed.');
      if (j.status === 'interrupted') throw new Error('The server restarted while it ran.');
    }
    throw new Error('Still running after eight minutes: look at the article later.');
  };
  const working = (text) => { askResult.hidden = false; askResult.innerHTML = `<p class="hint">${text}</p>`; askActions.replaceChildren(); };
  const failed = (err, retry) => {
    askResult.hidden = false;
    askResult.innerHTML = '<p class="hint is-error"></p>';
    $('p', askResult).textContent = err.message || String(err);
    askActions.replaceChildren(button('Try again', 'secondary', retry), button('Close', 'link', closeAsk));
  };

  const askAI = () => {
    const { from, to } = editor.state.selection;
    const doc = editor.state.doc;
    const selection = doc.textBetween(from, to, '\n\n');
    const before = doc.textBetween(Math.max(0, from - 1500), from, '\n\n');
    const after = doc.textBetween(to, Math.min(doc.content.size, to + 1500), '\n\n');
    const run = async () => {
      const instruction = askInput.value.trim();
      if (!instruction) { askInput.focus(); return; }
      working('Rewriting…');
      try {
        const res = await job('/api/ai-edit', { slug, instruction, selection, before, after }, step => working(`${step}…`));
        const text = String(res.replacement || '').trim();
        askResult.hidden = false;
        askResult.innerHTML = '<div class="change-text"><del class="diff-del"></del> <ins class="diff-ins"></ins></div>';
        $('del', askResult).textContent = selection;
        $('ins', askResult).textContent = text;
        askActions.replaceChildren(
          button('Apply', 'secondary', () => {
            // where the words are now: the person may have typed elsewhere while it ran
            const now = editor.state.doc;
            const same = now.textBetween(from, Math.min(to, now.content.size), '\n\n') === selection;
            let at = same ? { from, to } : null;
            if (!at) now.descendants((node, pos) => {
              if (at || !node.isText) return;
              const i = node.text.indexOf(selection);
              if (i >= 0) at = { from: pos + i, to: pos + i + selection.length };
            });
            if (!at) { failed(new Error('The words it rewrote are not in the article any more.'), run); return; }
            editor.chain().focus().insertContentAt(at, text).run();
            closeAsk();
          }),
          button('Discard', 'link', closeAsk),
          button('Try again', 'link', run));
      } catch (err) { failed(err, run); }
    };
    openAsk('Edit with AI', ui.dataset.aiCost, 'shorter · plainer · more concrete', '', [
      button('Rewrite', 'secondary', run), button('Cancel', 'link', closeAsk)]);
  };

  const askPicture = (at) => {
    const doc = editor.state.doc;
    const before = doc.textBetween(Math.max(0, at - 1500), at, '\n\n');
    const after = doc.textBetween(at, Math.min(doc.content.size, at + 1500), '\n\n');
    const run = async () => {
      working('Choosing a format…');
      try {
        const res = await job('/api/visual-insert', { slug, instruction: askInput.value.trim(), before, after },
                              step => working(`${step}…`));
        if (!res.markdown) throw new Error('The picture came back empty.');
        const where = Math.min(at, editor.state.doc.content.size);
        const node = editor.state.doc.nodeAt(where);
        const parsed = editor.markdown.parse(res.markdown);
        editor.chain().focus().insertContentAt(
          node && node.type.name === 'paragraph' && !node.textContent ? { from: where, to: where + node.nodeSize } : where,
          parsed.content || []).run();
        closeAsk();
      } catch (err) { failed(err, run); }
    };
    openAsk('A picture here', ui.dataset.insertCost, 'a chart of the prices · a screenshot of the pricing page · or leave it to the machine', '', [
      button('Make it', 'secondary', run), button('Cancel', 'link', closeAsk)]);
  };

  // ── a block kept as its Markdown (a table, a quote, HTML): edited as text ───────────────────
  rawBar.addEventListener('mousedown', e => e.preventDefault());
  const editRaw = () => {
    const sel = editor.state.selection;
    if (!sel.node || sel.node.type.name !== 'rawBlock') return;
    const pos = sel.from, node = sel.node;
    openAsk('Edit as Markdown', '', '', node.attrs.md.replace(/\n+$/, ''), [
      button('Apply', 'secondary', () => {
        const text = askText.value.replace(/\s+$/, '');
        if (!text) editor.chain().focus().deleteRange({ from: pos, to: pos + node.nodeSize }).run();
        else editor.view.dispatch(editor.state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, md: `${text}\n` }));
        closeAsk();
      }),
      button('Cancel', 'link', closeAsk)], true);
  };
  rawBar.addEventListener('click', e => {
    const b = e.target.closest('[data-raw]');
    if (!b) return;
    if (b.dataset.raw === 'remove') { editor.chain().focus().deleteSelection().run(); rawBar.hidden = true; }
    else editRaw();
  });
  host.addEventListener('dblclick', e => { if (e.target.closest('.raw-block')) editRaw(); });

  // ── a picture: alt text, Redraw, Remove ─────────────────────────────────────────────────────
  picBar.addEventListener('mousedown', e => e.preventDefault());
  picBar.addEventListener('click', e => {
    const b = e.target.closest('[data-pic]');
    const sel = editor.state.selection;
    if (!b || !sel.node || sel.node.type.name !== 'image') return;
    const pos = sel.from, node = sel.node;
    if (b.dataset.pic === 'remove') { editor.chain().focus().deleteSelection().run(); picBar.hidden = true; return; }
    if (b.dataset.pic === 'alt') {
      openAsk('Alt text', '', 'what the picture shows, for someone who cannot see it', node.attrs.alt || '', [
        button('Apply', 'secondary', () => {
          editor.view.dispatch(editor.state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, alt: askInput.value.trim() }));
          closeAsk();
        }),
        button('Cancel', 'link', closeAsk)]);
      return;
    }
    const path = node.attrs.src;
    if (!/^assets\//.test(path)) { openAsk('Redraw', '', '', '', [button('Close', 'link', closeAsk)]); failed(new Error('Only the article\'s own pictures are made again.'), closeAsk); return; }
    const run = async () => {
      working('Making it again…');
      try {
        await job('/api/visual-edit', { slug, path, feedback: askInput.value.trim(), alt: node.attrs.alt || '',
                                        version: ui.dataset.version }, step => working(`${step}…`));
        $$('img', host).forEach(img => { if (img.getAttribute('src').endsWith(path)) img.src = img.src.split('?')[0] + '?t=' + Date.now(); });
        closeAsk();
      } catch (err) { failed(err, run); }
    };
    openAsk(`Make ${path} again`, ui.dataset.redrawCost, 'what to change: darker background · drop the third bar', '', [
      button('Make it again', 'secondary', run), button('Cancel', 'link', closeAsk)]);
  });

  // ── Versions ────────────────────────────────────────────────────────────────────────────────
  const drawer = $('#versions-drawer'), opener = $('#versions-open');
  const toggleDrawer = (open) => { drawer.hidden = !open; opener.setAttribute('aria-expanded', String(open)); };
  opener.addEventListener('click', () => toggleDrawer(drawer.hidden));
  $('#versions-close').addEventListener('click', () => toggleDrawer(false));
  drawer.addEventListener('submit', e => {
    if (dirty && !window.confirm('Restoring drops your unsaved changes. Restore it?')) e.preventDefault();
    else dirty = false;
  });

  document.addEventListener('mousedown', e => {
    if (!e.target.closest('.is-floating') && !e.target.closest('#gutter')) { blockMenu.hidden = true; if (!e.target.closest('.ProseMirror')) slash.hidden = true; }
  });
})();
