// The editor's library for SEO Writer Agent (ui/static/vendor/tiptap/tiptap.js): TipTap with
// its Markdown support, prebuilt into one file that sets window.TT. See ui/vendor/README.md.
import { Editor, Extension, Node, Mark } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import Image from '@tiptap/extension-image';
import Placeholder from '@tiptap/extension-placeholder';
import { Markdown } from '@tiptap/markdown';
import { marked } from 'marked';
window.TT = { Editor, Extension, Node, Mark, StarterKit, Image, Placeholder, Markdown, marked };
