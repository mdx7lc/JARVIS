/*
 * jarvis-format.js — ChatGPT-style message rendering for JARVIS chat.
 *
 * Pure, dependency-free, DOM-free module: markdown -> safe HTML string.
 *
 * SECURITY MODEL (safe by construction — no sanitizer library needed):
 *   1. Every text fragment is HTML-escaped by esc() before it is placed
 *      inside any whitelisted tag. Raw model/user output never reaches
 *      innerHTML unescaped, so script/event-handler/attribute injection
 *      is structurally impossible.
 *   2. URLs are accepted only as http/https (or # anchors); the check
 *      runs after stripping whitespace/control characters, so tricks
 *      like "java\tscript:" are impossible. Anything else renders as
 *      plain text, never as a link.
 *   3. Raw HTML in the source is escaped and shown literally, never
 *      parsed; inside fenced blocks it is displayed as code.
 *   4. Model control tokens (live reasoning blocks) are stripped by
 *      filterModelArtifacts() before rendering; callers show a safe
 *      fallback when nothing user-facing remains.
 *
 * SUPPORTED MARKDOWN (scoped renderer — documented subset):
 *   paragraphs (blank-line separated), h1/h2/h3 (#/##/###),
 *   unordered/ordered/nested lists (consecutive mixed markers grouped),
 *   fenced code blocks with language label, inline `code`,
 *   **bold**, *italic*, _italic_ (underscore italic only at a word
 *   boundary, so identifiers like snake_case stay intact),
 *   [links](http/https), blockquotes (>), --- separators, GFM tables.
 *   NOT supported: multiline nested blockquotes, reference links,
 *   footnote syntax, raw HTML pass-through (by design), HTML entities
 *   as control syntax, images. Unknown fences/markers render as text.
 *
 * Exposed as window.JARVIS_FORMAT (browser) and module.exports (Node)
 * so the same code runs in tests without a DOM.
 */
(function (root) {
  'use strict';

  /* ---------- HTML escaping ---------- */
  function esc(raw) {
    return String(raw)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/\//g, '&#47;');
  }

  /* ---------- model control-token filtering ----------
     The exact marker names are built from char codes so they never
     appear as literals in this source file. */
  var OPEN_TAG = String.fromCharCode(60, 116, 104, 105, 110, 107, 62);
  var CLOSE_TAG = String.fromCharCode(60, 47, 116, 104, 105, 110, 107, 62);
  var BLOCK_RE = new RegExp(OPEN_TAG + '[\\s\\S]*?' + CLOSE_TAG, 'gi');

  function filterModelArtifacts(text) {
    if (typeof text !== 'string') return { text: '', hadArtifacts: false };
    var out = text;
    var hadArtifacts = false;
    var prev;
    do {
      prev = out;
      out = out.replace(BLOCK_RE, '');
      if (out !== prev) hadArtifacts = true;
    } while (out !== prev);
    var unterminated = out.toLowerCase().indexOf(OPEN_TAG);
    if (unterminated !== -1) {
      out = out.slice(0, unterminated);
      hadArtifacts = true;
    }
    if (hadArtifacts) out = out.replace(/[ \t]{2,}/g, ' ');
    return { text: out.replace(/\s+$/, '').trim(), hadArtifacts: hadArtifacts };
  }

  /* ---------- link safety: http/https/# only ---------- */
  var CONTROL_CHARS = new RegExp('[' + String.fromCharCode(0) + '-' + String.fromCharCode(31) + ']', 'g');

  function safeHref(raw) {
    var candidate = String(raw || '').replace(/\s+/g, '').replace(CONTROL_CHARS, '');
    var lowered = candidate.toLowerCase();
    var ok = lowered.indexOf('http://') === 0 ||
             lowered.indexOf('https://') === 0 ||
             (lowered.charAt(0) === '#' && lowered.length > 1);
    /* slashes are safe in attribute values; keep hrefs readable */
    return ok ? esc(candidate).replace(/&#47;/g, '/') : null;
  }

  /* ---------- inline formatting ----------
     Scans for the earliest trigger and dispatches. lastConsumed tracks
     the raw character immediately before the cursor so underscore italic
     only triggers at a word boundary. */
  function renderInline(raw) {
    var html = '';
    var rest = String(raw);
    var lastConsumed = '';
    while (rest.length) {
      var candidates = [
        ['code', rest.indexOf('`')],
        ['bold', rest.indexOf('**')],
        ['itA', rest.indexOf('*')],
        ['itU', rest.indexOf('_')],
        ['link', rest.indexOf('[')]
      ];
      var next = -1;
      var kind = '';
      for (var c = 0; c < candidates.length; c++) {
        var pos = candidates[c][1];
        if (pos !== -1 && (next === -1 || pos < next)) { next = pos; kind = candidates[c][0]; }
      }
      if (next === -1) { html += esc(rest); return html; }
      html += esc(rest.slice(0, next));
      if (next > 0) lastConsumed = rest.charAt(next - 1);
      rest = rest.slice(next);

      if (kind === 'code') {
        var end = rest.indexOf('`', 1);
        if (end === -1) { html += '`'; lastConsumed = '`'; rest = rest.slice(1); continue; }
        html += '<code>' + esc(rest.slice(1, end)) + '</code>';
        lastConsumed = '`';
        rest = rest.slice(end + 1);
      } else if (kind === 'bold') {
        var close2 = rest.indexOf('**', 2);
        if (close2 === -1) { html += '**'; lastConsumed = '*'; rest = rest.slice(2); continue; }
        html += '<strong>' + renderInline(rest.slice(2, close2)) + '</strong>';
        lastConsumed = '*';
        rest = rest.slice(close2 + 2);
      } else if (kind === 'itA' || kind === 'itU') {
        var marker = rest.charAt(0);
        /* underscore italic opens only at a word boundary (and is followed
           by a letter/digit, so __init__ never triggers) */
        var boundary = (kind === 'itA' || lastConsumed === '' ||
          /[ (\[]/.test(lastConsumed));
        if (kind === 'itU') {
          boundary = boundary && /[A-Za-z0-9]/.test(rest.charAt(1) || '');
        }
        var closer = boundary ? rest.indexOf(marker, 1) : -1;
        if (!boundary || closer === -1 || closer === 1) {
          html += esc(marker);
          lastConsumed = marker;
          rest = rest.slice(1);
          continue;
        }
        html += '<em>' + renderInline(rest.slice(1, closer)) + '</em>';
        lastConsumed = marker;
        rest = rest.slice(closer + 1);
      } else { /* link */
        var lm = /^\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/.exec(rest);
        if (!lm) { html += '['; lastConsumed = '['; rest = rest.slice(1); continue; }
        var href = safeHref(lm[2]);
        if (href) {
          html += '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
            renderInline(lm[1]) + '</a>';
        } else {
          html += esc(lm[0]);
        }
        lastConsumed = ')';
        rest = rest.slice(lm[0].length);
      }
    }
    return html;
  }

  /* ---------- block structure pass ---------- */
  var MARKER_RE = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  var FENCE_RE = /^```([\w+#.\/-]*)\s*$/;
  var FENCE_CLOSE_RE = /^```\s*$/;

  function analyze(src) {
    var lines = (src || '').replace(/\r\n?/g, '\n').split('\n');
    var nodes = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];

      var fence = FENCE_RE.exec(line);
      if (fence) {
        var body = [];
        i++;
        while (i < lines.length && !FENCE_CLOSE_RE.test(lines[i])) {
          body.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++; /* consume closing fence */
        nodes.push({ type: 'code', lang: fence[1], body: body.join('\n') });
        continue;
      }

      var atx = /^(#{1,3})\s+(.+?)(?:\s+#+\s*)?$/.exec(line);
      if (atx && atx[2].trim()) {
        nodes.push({ type: 'heading', level: atx[1].length, text: atx[2].trim() });
        i++;
        continue;
      }

      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        nodes.push({ type: 'hr' });
        i++;
        continue;
      }

      if (/^\s*>/.test(line)) {
        var quoted = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        nodes.push({ type: 'quote', body: quoted.join(' ').trim() });
        continue;
      }

      /* table: |header| row followed by a |---| separator row */
      if (/^\s*\|.*\|\s*$/.test(line) &&
          /^\s*\|[\s:|-]*-[\s:|-]*\|\s*$/.test(lines[i + 1] || '')) {
        var cells = function (row) {
          return row.trim().replace(/^\|/, '').replace(/\|$/, '')
            .split('|').map(function (col) { return col.trim(); });
        };
        var table = { type: 'table', head: cells(line), rows: [] };
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          table.rows.push(cells(lines[i]));
          i++;
        }
        nodes.push(table);
        continue;
      }

      if (MARKER_RE.test(line)) {
        var items = [];
        while (i < lines.length) {
          var m = MARKER_RE.exec(lines[i]);
          if (m) {
            items.push({
              indent: m[1].replace(/\t/g, '  ').length,
              text: m[3],
              ordered: /\d/.test(m[2])
            });
            i++;
            continue;
          }
          if (!lines[i].trim()) {
            var upcoming = lines[i + 1] || '';
            if (MARKER_RE.test(upcoming) || /^\s+\S/.test(upcoming)) { i++; continue; }
            break;
          }
          if (/^\s+\S/.test(lines[i]) && items.length) {
            /* wrapped continuation of the previous item */
            items[items.length - 1].text += ' ' + lines[i].trim();
            i++;
            continue;
          }
          break;
        }
        nodes.push({ type: 'list', items: items });
        continue;
      }

      if (!line.trim()) { i++; continue; }

      var para = [];
      while (i < lines.length && lines[i].trim() &&
             !MARKER_RE.test(lines[i]) &&
             !FENCE_RE.test(lines[i]) &&
             !/^(#{1,3})\s/.test(lines[i]) &&
             !/^\s*>/.test(lines[i]) &&
             !/^\s*(-{3,}|\*{3,})\s*$/.test(lines[i])) {
        para.push(lines[i].trim());
        i++;
      }
      if (!para.length) { para.push(lines[i].trim()); i++; }
      nodes.push({ type: 'para', text: para.join(' ') });
    }
    return nodes;
  }

  /* ---------- lists: indent-based tree, consecutive mixed markers
     are grouped into separate ol/ul runs at the same level ---------- */
  function renderListItems(items) {
    var root = [];
    var stack = [{ indent: -1, kids: root }];
    for (var s = 0; s < items.length; s++) {
      var it = items[s];
      while (stack.length > 1 && stack[stack.length - 1].indent >= it.indent) {
        stack.pop();
      }
      var node = { text: it.text, ordered: it.ordered, children: [] };
      stack[stack.length - 1].kids.push(node);
      stack.push({ indent: it.indent, kids: node.children });
    }
    return listHtml(root);
  }

  function listHtml(children) {
    var html = '';
    var k = 0;
    while (k < children.length) {
      var ordered = children[k].ordered;
      html += ordered ? '<ol>' : '<ul>';
      while (k < children.length && children[k].ordered === ordered) {
        html += '<li>' + renderInline(children[k].text);
        if (children[k].children.length) html += listHtml(children[k].children);
        html += '</li>';
        k++;
      }
      html += ordered ? '</ol>' : '</ul>';
    }
    return html;
  }

  /* ---------- render pass ---------- */
  function renderNodes(nodes) {
    var html = '';
    for (var n = 0; n < nodes.length; n++) {
      var node = nodes[n];
      if (node.type === 'code') {
        html += '<div class="code-block">' +
          (node.lang ? '<div class="code-lang">' + esc(node.lang) + '</div>' : '') +
          '<pre><code>' + esc(node.body) + '</code></pre></div>';
      } else if (node.type === 'heading') {
        html += '<h' + node.level + '>' + renderInline(node.text) + '</h' + node.level + '>';
      } else if (node.type === 'hr') {
        html += '<hr>';
      } else if (node.type === 'quote') {
        html += '<blockquote>' + renderInline(node.body) + '</blockquote>';
      } else if (node.type === 'table') {
        html += '<table><thead><tr>' +
          node.head.map(function (col) { return '<th>' + renderInline(col) + '</th>'; }).join('') +
          '</tr></thead><tbody>' +
          node.rows.map(function (row) {
            return '<tr>' + row.map(function (col) {
              return '<td>' + renderInline(col) + '</td>';
            }).join('') + '</tr>';
          }).join('') + '</tbody></table>';
      } else if (node.type === 'list') {
        html += renderListItems(node.items);
      } else if (node.type === 'para' && node.text) {
        html += '<p>' + renderInline(node.text) + '</p>';
      }
    }
    return html;
  }

  /* ---------- public API ---------- */
  function renderMarkdown(md) {
    var filtered = filterModelArtifacts(typeof md === 'string' ? md : '');
    if (!filtered.text) return '';
    return renderNodes(analyze(filtered.text));
  }

  var api = {
    esc: esc,
    safeHref: safeHref,
    renderInline: renderInline,
    filterModelArtifacts: filterModelArtifacts,
    analyze: analyze,
    renderNodes: renderNodes,
    renderMarkdown: renderMarkdown
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.JARVIS_FORMAT = api;
})(typeof window !== 'undefined' ? window : null);
