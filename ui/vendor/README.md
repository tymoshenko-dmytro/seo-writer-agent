# Vendored design assets

Shipped as files, with their licences; nothing here is fetched or built at run time.

## Icons: Lucide (ISC)

`lucide/icons.json` holds the shapes the interface draws, taken from `@iconify-json/lucide`
1.2.130 with the stroke attributes removed (`ui/icons.py` sets them on the `<svg>`: 16 px, stroke
1.75). `lucide/LICENSE` is Lucide's. To add an icon, copy its `body` from that package's
`icons.json` (follow `aliases` to the parent), strip `fill="none"`, `stroke="currentColor"`,
`stroke-linecap`, `stroke-linejoin` and `stroke-width` and an outer `<g>`, and add it under its
Lucide name. A template that draws a name not listed here fails to render (`ui/icons.py`).

## Fonts: Inter and JetBrains Mono (SIL OFL 1.1)

In `ui/static/fonts/`, each next to its `OFL.txt`:

- `inter/InterVariable.woff`, `InterVariable-Italic.woff`: Inter 4.001 (the Google Fonts
  variable files), the weight axis cut to 400–700, the optical-size axis kept;
- `jetbrains-mono/JetBrainsMono.woff`: JetBrains Mono 2.211, weight 400–600.

Both subset to Latin, Latin Extended, Greek, Cyrillic and Vietnamese, punctuation, currency,
arrows and maths, as WOFF, with fontTools:

```python
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools import subset
import io

f = instancer.instantiateVariableFont(TTFont(src, lazy=False), {"wght": (400, 700)})
b = io.BytesIO(); f.save(b); b.seek(0); f = TTFont(b, lazy=False)
o = subset.Options(); o.layout_features = ["*"]; o.name_IDs = ["*"]; o.name_languages = ["*"]
s = subset.Subsetter(o); s.populate(unicodes=RANGES); s.subset(f)
f.flavor = "woff"; f.save(out)
```

`RANGES`: U+0020–007E, U+00A0–024F, U+0259, U+0300–052F, U+1E00–1EFF, U+2000–206F,
U+20A0–20CF, U+2100–214F, U+2190–22FF, U+25CB, U+25CF, U+FEFF, U+FFFD.

## The editor: TipTap (MIT) and marked (MIT)

`ui/static/vendor/tiptap/tiptap.js` is TipTap 3.31.2 (core, StarterKit, Image, Placeholder and
`@tiptap/markdown`) with marked 17.0.6, bundled with esbuild 0.27.7 into one minified file that
sets `window.TT`; `ui/static/js/mdblocks.js` builds the block editor on it. Every package that
went into the file, with its version and licence, is in `LICENSES.md` next to it (all MIT).

To rebuild it, in `ui/vendor/tiptap/` (`package.json` pins the versions, `entry.js` says what the
file exports):

```bash
npm install
npm run build
```

`build.mjs` writes `tiptap.js` and `LICENSES.md` into `ui/static/vendor/tiptap/`; nothing is
built when the Studio runs.

Tables are not a TipTap extension here: the editor keeps a table, an HTML block and a quote as
their Markdown (a raw block, edited as text), so saving never rewrites them.
