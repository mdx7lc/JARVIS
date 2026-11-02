'use strict';
/*
 * Regression tests for static/jarvis-format.js (run with plain node, no DOM).
 *   /home/mdx/.nvm/versions/node/v24.21.0/bin/node tests/test_format_render.js
 */
var assert = require('assert');
var path = require('path');
var fmt = require(path.join(__dirname, '..', 'static', 'jarvis-format.js'));

var passed = 0;
function check(name, actual, expected) {
  assert.strictEqual(actual, expected, name + '\n  got:      ' + JSON.stringify(actual) + '\n  expected: ' + JSON.stringify(expected));
  passed++;
  console.log('ok - ' + name);
}

/* Control-token markers built from char codes (same as the module). */
var OPEN = String.fromCharCode(60, 116, 104, 105, 110, 107, 62);
var CLOSE = String.fromCharCode(60, 47, 116, 104, 105, 110, 107, 62);

/* A. paragraph */
check('A paragraph',
  fmt.renderMarkdown('Hello world'),
  '<p>Hello world</p>');

check('A blank lines split paragraphs',
  fmt.renderMarkdown('one\n\ntwo'),
  '<p>one</p><p>two</p>');

/* B. heading + fenced code block with python indentation */
check('B heading levels',
  fmt.renderMarkdown('## Title'),
  '<h2>Title</h2>');

check('B fenced code keeps indentation and escapes',
  fmt.renderMarkdown('```python\ndef f():\n    return "<b>" & 1\n```'),
  '<div class="code-block"><div class="code-lang">python</div><pre><code>def f():\n    return &quot;&lt;b&gt;&quot; &amp; 1</code></pre></div>');

check('B unterminated fence becomes open code block',
  fmt.renderMarkdown('```\nabc'),
  '<div class="code-block"><pre><code>abc</code></pre></div>');

/* C. ordered list + nesting */
check('C ordered nested',
  fmt.renderMarkdown('1. one\n2. two\n   1. inner'),
  '<ol><li>one</li><li>two<ol><li>inner</li></ol></li></ol>');

check('C bullet nested mixed markers grouped',
  fmt.renderMarkdown('- a\n* b\n  - c\n1. num'),
  '<ul><li>a</li><li>b<ul><li>c</li></ul></li></ul><ol><li>num</li></ol>');

check('C tab indents and wrapped lines',
  fmt.renderMarkdown('- first\n\t- deeper\n    continued'),
  '<ul><li>first<ul><li>deeper continued</li></ul></li></ul>');

/* D. inline formatting */
check('D bold italic code link',
  fmt.renderInline('a **b** c *d* e `f` [x](https://e.com)'),
  'a <strong>b</strong> c <em>d</em> e <code>f</code> <a href="https://e.com" target="_blank" rel="noopener noreferrer">x</a>');

check('D underscore in identifiers stays literal',
  fmt.renderInline('use snake_case and __init__ here'),
  'use snake_case and __init__ here');

check('D underscore italic at word boundary',
  fmt.renderInline('this is _ital_ ok'),
  'this is <em>ital</em> ok');

check('D unclosed markers stay literal',
  fmt.renderInline('a **b `c _d [x](https://e.com'),
  'a **b `c _d [x](https:&#47;&#47;e.com');

/* E. mixed content */
check('E mixed blocks',
  fmt.renderMarkdown('# T\ntext with `x`.\n\n- item **b**\n\n> quote\n\n---\n\n| h1 | h2 |\n| --- | --- |\n| a | b |'),
  '<h1>T</h1><p>text with <code>x</code>.</p><ul><li>item <strong>b</strong></li></ul>' +
  '<blockquote>quote</blockquote><hr><table><thead><tr><th>h1</th><th>h2</th></tr></thead>' +
  '<tbody><tr><td>a</td><td>b</td></tr></tbody></table>');

/* F. model control-token filtering */
check('F pair removed',
  fmt.filterModelArtifacts('secret ' + OPEN + 'hidden' + CLOSE + ' answer').text,
  'secret answer');

check('F hadArtifacts flag',
  fmt.filterModelArtifacts('a' + OPEN + 'x' + CLOSE + 'b').hadArtifacts,
  true);

check('F unterminated open truncates',
  fmt.filterModelArtifacts('good' + OPEN + 'never shown').text,
  'good');

check('F unterminated flagged',
  fmt.filterModelArtifacts('good' + OPEN + 'x').hadArtifacts,
  true);

check('F clean text untouched',
  fmt.filterModelArtifacts('plain text').text,
  'plain text');
assert.deepStrictEqual(fmt.filterModelArtifacts('plain text'), { text: 'plain text', hadArtifacts: false }, 'F clean text untouched deep');

/* G. XSS defenses */
check('G script tag escaped',
  fmt.renderMarkdown('<script>alert(1)</script>'),
  '<p>&lt;script&gt;alert(1)&lt;&#47;script&gt;</p>');

check('G inline code escapes angle brackets',
  fmt.renderInline('`<script>alert(1)</script>`'),
  '<code>&lt;script&gt;alert(1)&lt;&#47;script&gt;</code>');

check('G javascript href rejected',
  fmt.renderMarkdown('[click](javascript:alert(1))'),
  '<p>[click](javascript:alert(1))</p>');

check('G vbscript href rejected',
  fmt.renderMarkdown('[x](vbscript:msgbox)'),
  '<p>[x](vbscript:msgbox)</p>');

check('G java-tab-script href rejected',
  fmt.renderMarkdown('[x](java\tscript:alert(1))'),
  '<p>[x](java\tscript:alert(1))</p>');

check('G event handler attrs never parsed',
  fmt.renderMarkdown('[x](https://e.com" onmouseover="alert(1))'),
  '<p>[x](https:&#47;&#47;e.com&quot; onmouseover=&quot;alert(1))</p>');

check('G anchor href allowed',
  fmt.renderInline('[top](#top)'),
  '<a href="#top" target="_blank" rel="noopener noreferrer">top</a>');

check('G ampersand in text and code escaped',
  fmt.renderMarkdown('Tom & Jerry `a&b`'),
  '<p>Tom &amp; Jerry <code>a&amp;b</code></p>');

check('G quotes escaped in quotes attr',
  fmt.renderMarkdown('say "hi"'),
  '<p>say &quot;hi&quot;</p>');

check('G control char in href URL remains rejected-or-clean',
  (function () {
    var url = 'https://e.com/pa' + String.fromCharCode(3) + 'th';
    var html = fmt.renderInline('[x](' + url + ')');
    return html.indexOf(String.fromCharCode(3)) === -1 &&
           html.indexOf('<a href="https://e.com/path"') === 0;
  })(),
  true);

/* H. empty and whitespace */
check('H empty string',
  fmt.renderMarkdown(''),
  '');
check('H whitespace only',
  fmt.renderMarkdown('   \n  \n'),
  '');
check('H non-string input',
  fmt.renderMarkdown(null),
  '');

/* XSS attack combination through full renderMarkdown */
check('G full attack string stays inert',
  fmt.renderMarkdown('[a](https://e.com) <img src=x onerror=alert(1)>'),
  '<p><a href="https://e.com" target="_blank" rel="noopener noreferrer">a</a> &lt;img src=x onerror=alert(1)&gt;</p>');

console.log('\n' + passed + ' format-render tests passed');
