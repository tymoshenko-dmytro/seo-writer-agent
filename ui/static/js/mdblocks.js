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
      let nodes;
      if (RAW_TYPES.has(t.type)) {
        nodes = [{ type: 'rawBlock', attrs: { md: t.raw, srcId: id } }];
      } else {
        // the block's own text, without the blank lines after it (they are kept with the block's
        // bytes, and read alone they would be empty paragraphs)
        const own = t.raw.replace(/\n+$/, '');
        const parsed = editor.markdown.parse(defs ? `${own}\n\n${defs}\n` : `${own}\n`);
        nodes = (parsed.content || []).map((n) => ({ ...n, attrs: { ...(n.attrs || {}), srcId: id } }));
        if (!nodes.length || !valid(editor, nodes)) nodes = [{ type: 'rawBlock', attrs: { md: t.raw, srcId: id } }];
      }
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
    return editor.markdown.serialize({ type: 'doc', content: nodes.map(strip) }).replace(/\n+$/, '');
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

  // A model's Markdown as the editor's content: inside one paragraph, heading or list item it goes
  // in as that block's words (links and bold as marks, not as typed stars), so a rewrite inside a
  // block does not split it; more than one block goes in as blocks
  function parseReplacement(editor, md) {
    const doc = editor.markdown.parse(md);
    const textblock = (n) => !!editor.schema.nodes[n.type] && editor.schema.nodes[n.type].isTextblock;
    let n = doc;
    while (n.content && n.content.length === 1 && !textblock(n)) n = n.content[0];
    return textblock(n) ? (n.content || []) : (doc.content || []);
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

  window.MdBlocks = { extensions, load, save, selectionMarkdown, parseReplacement, RAW_TYPES };
})();
