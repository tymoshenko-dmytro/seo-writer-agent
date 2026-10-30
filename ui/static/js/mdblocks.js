// The article as blocks that keep their own Markdown.
//
// Markdown stays the storage: the version, the review, the publisher and the guards read it, so
// the editor must give back what it was given. The text is cut into blocks with marked's lexer
// (whose pieces join back into the text exactly); each block becomes the editor's nodes, marked
// with the block it came from. On save, a block whose nodes are as they were loaded is written
// back byte for byte with the space that followed it; only a block the person changed is written
// again by the editor. So saving without a change writes the same bytes, and a change to one block
// changes only that block's lines.
//
// What the editor cannot hold without changing it (a table, an HTML comment or block, a quote) is
// a raw block: shown as it renders, edited as its Markdown.
//
// Needs window.TT (ui/static/vendor/tiptap/tiptap.js): Editor, Node, Extension, StarterKit,
// Image, Markdown, marked.
(function () {
  const RAW_TYPES = new Set(['table', 'html', 'blockquote', 'def']);
  const RAW_IN_LIST = 'The rewrite has a table, a quote or HTML, which cannot go inside a list: select whole blocks, or ask for text only.';
  const BLOCK_TYPES = ['paragraph', 'heading', 'bulletList', 'orderedList', 'listItem', 'codeBlock',
                       'blockquote', 'horizontalRule', 'image', 'rawBlock'];

  // which block of the source a node came from; a node split off by Enter is new
  const SourceId = () => window.TT.Extension.create({
    name: 'sourceId',
    addGlobalAttributes() {
      return [{ types: BLOCK_TYPES, attributes: {
        srcId: { default: null, keepOnSplit: false, rendered: false },
      } }];
    },
  });

  // a block kept as its Markdown: a table, HTML, a quote
  const RawBlock = () => window.TT.Node.create({
    name: 'rawBlock',
    group: 'block',
    atom: true,
    selectable: true,
    draggable: true,
    addAttributes() {
      return { md: { default: '' }, srcId: { default: null, rendered: false } };
    },
    parseHTML() { return [{ tag: 'div[data-raw-block]' }]; },
    renderHTML({ node }) {
      return ['div', { 'data-raw-block': '', class: 'raw-block' }, ['pre', {}, node.attrs.md]];
    },
  });

  // `assetBase` ("/a/<slug>/"): where the article's own pictures (assets/…) are shown from; the
  // Markdown keeps the address as written
  function extensions(options = {}) {
    const { StarterKit, Image, Markdown } = window.TT;
    const base = options.assetBase || '';
    const shown = (src) => (base && src && !/^([a-z][a-z0-9+.-]*:|\/)/i.test(src) ? base + src : src);
    // a picture sits in its paragraph, with its caption line, as the Markdown writes it
    const Picture = Image.extend({
      renderHTML({ HTMLAttributes }) { return ['img', { ...HTMLAttributes, src: shown(HTMLAttributes.src) }]; },
    });
    return [StarterKit, Picture.configure({ inline: true }), Markdown, SourceId(), RawBlock()];
  }

  function valid(editor, nodes) {
    // nodes the editor's schema accepts as they are; anything else is kept as raw Markdown
    try {
      for (const n of nodes) editor.schema.nodeFromJSON(n).check();
      return true;
    } catch (e) {
      return false;
    }
  }

  const strip = (n) => {
    // a node without its source mark, for comparing what it holds
    const out = { ...n };
    if (out.attrs) { out.attrs = { ...out.attrs }; delete out.attrs.srcId; }
    if (out.content) out.content = out.content.map(strip);
    return out;
  };

  function groups(doc) {
    // consecutive top-level nodes from the same source block
    const out = [];
    for (const n of doc.content || []) {
      const id = (n.attrs && n.attrs.srcId) || null;
      const last = out[out.length - 1];
      if (last && id && last.id === id) last.nodes.push(n);
      else out.push({ id, nodes: [n] });
    }
    return out;
  }

  // One block of Markdown (a token of marked's lexer) as the editor's nodes: what the editor cannot
  // hold as it is (a table, HTML, a quote, a link definition) is a raw block of its Markdown
  function blockNodes(editor, t, defs) {
    if (!RAW_TYPES.has(t.type) && !nestedRaw(t)) {
      // the block's own text, without the blank lines after it (they are kept with the block's
      // bytes, and read alone they would be empty paragraphs)
      const own = t.raw.replace(/\n+$/, '');
      const nodes = editor.markdown.parse(defs ? `${own}\n\n${defs}\n` : `${own}\n`).content || [];
      if (nodes.length && valid(editor, nodes)) return nodes;
    }
    return [{ type: 'rawBlock', attrs: { md: t.raw } }];
  }

  // a table or HTML inside a list: the editor's parser drops it and keeps the rest valid, so the
  // whole block stays raw instead (review R4EC-04)
  function nestedRaw(t) {
    const inner = (t.tokens || []).concat(...(t.items || []).map((i) => i.tokens || []));
    return inner.some((c) => c.type === 'table' || (c.type === 'html' && c.block) || nestedRaw(c));
  }

  // Load `md` into `editor`; returns the state `save` needs.
  function load(editor, md) {
    const { marked } = window.TT;
    const tokens = marked.lexer(md);
    // a reference-style link or picture ([words][label]) is defined elsewhere in the text: each
    // block is read with the text's definitions, so it is a link in the editor, not its words
    // (review R5CC-04); a changed block is written with the address inline
    const defs = tokens.filter((t) => t.type === 'def').map((t) => t.raw.trim()).join('\n');
    const blocks = [];
    let lead = '';
    const content = [];
    for (const t of tokens) {
      if (t.type === 'space') {
        if (blocks.length) blocks[blocks.length - 1].after += t.raw; else lead += t.raw;
        continue;
      }
      const id = `b${blocks.length}`;
      const nodes = blockNodes(editor, t, defs).map((n) => ({ ...n, attrs: { ...(n.attrs || {}), srcId: id } }));
      blocks.push({ id, raw: t.raw, after: '' });
      content.push(...nodes);
    }
    editor.commands.setContent({ type: 'doc', content }, { emitUpdate: false });
    // what each block holds as the editor keeps it (defaults filled in): the "unchanged" mark
    const byId = {};
    blocks.forEach((b, i) => { byId[b.id] = { ...b, index: i }; });
    for (const g of groups(editor.getJSON())) {
      if (g.id && byId[g.id] && byId[g.id].snapshot === undefined) byId[g.id].snapshot = JSON.stringify(g.nodes.map(strip));
    }
    return { lead, byId, order: blocks.map((b) => b.id) };
  }

  function serializeNodes(editor, nodes) {
    // a raw block is its Markdown; anything else is written by the editor
    if (nodes.length === 1 && nodes[0].type === 'rawBlock') return nodes[0].attrs.md.replace(/\n+$/, '');
    return separateLists(editor.markdown.serialize({ type: 'doc', content: nodes.map(strip) }).replace(/\n+$/, ''));
  }

  // The editor writes a nested list right under its item's first line. A numbered list that does
  // not start at 1 cannot interrupt a paragraph (CommonMark), so the server read "3. One" there as
  // text: such a list gets a blank line before it (review RE22-04). Code fences are left alone
  function separateLists(md) {
    const out = [];
    let fence = null;            // the open fence's character and length, as CommonMark closes it (R1D6-01)
    for (const line of md.split('\n')) {
      // Markdown's own whitespace, not JavaScript's \s: an indent is spaces (a tab is four columns,
      // NBSP is text), and only spaces or tabs may follow a closing fence (RA4E-01)
      const f = line.match(/^( *)(`{3,}|~{3,})(.*)$/);
      if (!fence && f && !(f[2][0] === '`' && f[3].includes('`'))) fence = { ind: f[1].length, ch: f[2][0], n: f[2].length };
      else if (fence && f && f[1].length - fence.ind <= 3 && f[2][0] === fence.ch && f[2].length >= fence.n && /^[ \t]*$/.test(f[3])) {
        fence = null; out.push(line); continue;
      }
      const prev = out[out.length - 1];
      const m = !fence && line.match(/^( *)(\d+)[.)] /);
      // a sibling item (a list marker at the same indentation) is not a paragraph to interrupt
      const sibling = (p) => { const k = p.match(/^( *)([-*+]|\d+[.)]) /); return k && k[1].length === m[1].length; };
      if (m && Number(m[2]) !== 1 && prev !== undefined && prev.trim() && !sibling(prev)) out.push('');
      out.push(line);
    }
    return out.join('\n');
  }

  // A selection as Markdown, its links' addresses and its emphasis written out: what a model
  // rewrites, so a rewrite keeps the sources (the selection read as plain text lost every link's
  // address; audit 2026-10-30)
  function selectionMarkdown(editor, from, to) {
    const doc = editor.state.doc;
    const $from = doc.resolve(from);
    const parent = $from.node($from.sharedDepth(to));
    let nodes = doc.slice(from, to).content.toJSON() || [];
    // words inside one paragraph or heading come as bare inline nodes: one block of that kind
    if (parent.isTextblock) nodes = [{ ...parent.toJSON(), content: nodes }];
    return nodes.map((n) => serializeNodes(editor, [n])).join('\n\n');
  }

  // A model's Markdown for the range it rewrote: its blocks read as the article's are (a table or a
  // link definition stays raw Markdown, a reference link finds the article's definitions), placed
  // by what the range was, not by what the reply looks like (review RDD8-01..03):
  // - inside one paragraph, heading or list item: one block of reply goes in as that block's
  //   words, so the block is not split;
  // - whole blocks (or whole list items): replaced whole, the reply's blocks (or items) in their
  //   place, so a list is not nested in an emptied item and a heading stays a heading;
  // - anything else: the reply's blocks where the words were.
  // The reply's blocks, read with the link definitions that hold after it goes in: its own first
  // (the first definition of a label wins), then the article's (review R4EC-02). Positions are only compared, never resolved, so a reply that came back
  // after the article changed is still read (review R4EC-05)
  function replyNodes(editor, md, from, to) {
    const { marked } = window.TT;
    const tokens = marked.lexer(md);
    // all the reply's definitions, one inside a list or a quote too: marked gathers them, and a
    // link in another block of the reply uses them (review RE22-02)
    // marked has already read their escapes: written back escaped, so the second reading gives
    // the same address and title, a backslash in them too (review R1D6-02)
    const esc = (v, chars) => v.replace(chars, '\\$&');
    const defs = Object.entries(tokens.links || {}).map(([label, l]) =>
      `[${label}]: <${esc(l.href, /[\\<>]/g)}>${l.title ? ` "${esc(l.title, /[\\"]/g)}"` : ''}`);
    // then the article's: outside the replaced range first, then inside it, which the reply may
    // still lean on without repeating it (review RE22-02 controls)
    const outside = [], inside = [];
    editor.state.doc.descendants((n, pos) => {
      if (n.type.name === 'rawBlock' && /^ {0,3}\[[^\]]+\]:/.test(n.attrs.md)) {
        (pos + n.nodeSize <= from || pos >= to ? outside : inside).push(n.attrs.md.trim());
      }
    });
    defs.push(...outside, ...inside);
    return tokens.filter((t) => t.type !== 'space').flatMap((t) => blockNodes(editor, t, defs.join('\n')));
  }

  function parseReplacement(editor, md, from, to) {
    const doc = editor.state.doc;
    const nodes = replyNodes(editor, md, from, to);
    const $from = doc.resolve(from), $to = doc.resolve(to);
    let d = $from.sharedDepth(to);
    const parent = $from.node(d);
    const textblock = (n) => !!editor.schema.nodes[n.type] && editor.schema.nodes[n.type].isTextblock;
    // a table, a quote or HTML is a raw block, which the Markdown is written with only at the top
    // of the article: one that would land inside a list is refused before anything changes
    // (review R4EC-03)
    const place = (range, content, container) => {
      if (container.type.name !== 'doc' && content.some((n) => n.type === 'rawBlock')) {
        throw new Error(RAW_IN_LIST);
      }
      return { range, content };
    };
    if (parent.isTextblock) {
      let n = nodes.length === 1 ? nodes[0] : null;
      while (n && !textblock(n) && n.content && n.content.length === 1) n = n.content[0];
      if (n && textblock(n)) return { range: { from, to }, content: n.content || [] };
      return place({ from, to }, nodes, $from.node(d - 1));
    }
    const atStart = (l) => l > $from.depth ? true : (l === $from.depth ? $from.parentOffset === 0 : $from.index(l) === 0 && atStart(l + 1));
    const atEnd = (l) => l > $to.depth ? true : (l === $to.depth ? $to.parentOffset === $to.parent.content.size
                                                                  : $to.indexAfter(l) === $to.node(l).childCount && atEnd(l + 1));
    if (!atStart(d + 1) || !atEnd(d + 1)) return place({ from, to }, nodes, parent);
    // all of a list selected: the list itself is replaced, so the reply's own list type and start
    // number hold and no emptied item is left (review R4EC-06). Nothing else is climbed: all of
    // one item is that item's blocks replaced, a quote around a list is kept
    const covered = (l) => $from.index(l) === 0 && $to.indexAfter(l) === $from.node(l).childCount;
    const isList = (n) => n.type.name === 'bulletList' || n.type.name === 'orderedList';
    // only a list is climbed out of: a quote or an item around it stays (review RE22-03)
    while (d > 0 && isList($from.node(d)) && covered(d)) d -= 1;
    const outer = $from.node(d);
    const range = { from: $from.before(d + 1), to: $to.after(d + 1) };
    const list = nodes.length === 1 && nodes[0].type === outer.type.name && outer.type.name !== 'doc';
    return place(range, list ? nodes[0].content || [] : nodes, outer);
  }

  // The reply in its place. The blocks it touched are written again from what the editor holds,
  // not given back as their old bytes: those may name a link definition the reply dropped, or
  // read brackets the reply escaped as a link (review RDD8-04, RDD8-05)
  function applyReplacement(editor, { range, content }) {
    // where the reply's nodes really land is known only after the editor fits them in: a range
    // that starts inside a list item can put a raw block in it however the range looked. Such a
    // change is taken back whole (the state before it, history too) and refused (review RE22-01)
    const before = editor.state;
    editor.chain().focus().insertContentAt(range, content).run();
    let nested = false;
    editor.state.doc.descendants((node, pos, parent) => {
      if (node.type.name === 'rawBlock' && parent.type.name !== 'doc') nested = true;
      return !nested;
    });
    if (nested) {
      editor.view.updateState(before);
      throw new Error(RAW_IN_LIST);
    }
    const doc = editor.state.doc;
    const end = range.to + (doc.content.size - before.doc.content.size);
    const tr = editor.state.tr;
    doc.forEach((node, pos) => {
      if (pos < end && pos + node.nodeSize > range.from && node.attrs.srcId) {    // touching is not touched (R4EC-01)
        tr.setNodeMarkup(pos, undefined, { ...node.attrs, srcId: null });
      }
    });
    if (tr.docChanged) editor.view.dispatch(tr);
  }

  // The document as Markdown: unchanged blocks byte for byte, changed ones written again.
  function save(editor, state) {
    const gs = groups(editor.getJSON());
    const seen = new Set();
    const parts = [];
    gs.forEach((g, i) => {
      const b = g.id && state.byId[g.id];
      const same = b && !seen.has(g.id) && JSON.stringify(g.nodes.map(strip)) === b.snapshot;
      if (g.id) seen.add(g.id);
      const next = gs[i + 1];
      // the space that followed this block in the source, when what follows it is what did
      const nextSame = b && next && next.id && state.order[b.index + 1] === next.id;
      if (same) {
        if (nextSame || (!next && b.index === state.order.length - 1)) parts.push(b.raw + b.after);
        else parts.push(b.raw.replace(/\n*$/, '') + (next ? '\n\n' : (b.raw + b.after).match(/\n*$/)[0] || '\n'));
      } else {
        let text = serializeNodes(editor, g.nodes);
        // the editor writes a bracket as \[ so it never becomes a link by accident; in a block
        // whose source wrote its brackets plain, a reference-style link or picture ([words][r],
        // defined elsewhere in the text) is one, and stays one (review RB32-03)
        if (b && !/\\[\[\]]/.test(b.raw)) text = text.replace(/\\([\[\]])/g, '$1');
        if (!text.trim()) return;                       // an emptied block leaves nothing
        // in its place in the source: the source's own spacing after it
        const inPlace = b && (nextSame || (!next && b.index === state.order.length - 1));
        parts.push(text + (inPlace ? ((b.raw + b.after).match(/\n*$/)[0] || '\n') : (next ? '\n\n' : '\n')));
      }
    });
    return state.lead + parts.join('');
  }

  window.MdBlocks = { extensions, load, save, selectionMarkdown, replyNodes, parseReplacement, applyReplacement, RAW_TYPES };
})();
