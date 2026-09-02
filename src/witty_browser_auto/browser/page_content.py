"""把渲染后的页面转成 LLM 可读的 Markdown，以及列出页面链接与图片。

外部智能体最高频的需求之一是"把这页读给我的模型看"。此前唯一的出口是
`Observation.summary`——标题、地址加正文前 3000 字的裸文本，没有标题层级、列表、代码块
和表格，也不去导航与页脚。这里补上主内容提取与 Markdown 转换，让读文档、读文章这类
任务不必退化成逐个元素 `read_element`。

两条边界写进实现：

- **只读渲染后的当前页面**，不发起任何独立 HTTP 请求；页面由调用方先导航过去。
- **重复的结构化记录仍然走结构化采集**。Markdown 是给正文用的，它没有去重、分页闭合与
  完整性证据；用它抠订单表格会拿到一份没有闭合证据的片段。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MAX_CHARS = 40_000
MAX_CHARS = 200_000
DEFAULT_MAX_LINKS = 100
MAX_LINKS = 500

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_LINKS",
    "MAX_CHARS",
    "MAX_LINKS",
    "PAGE_LINKS_SCRIPT",
    "PAGE_MARKDOWN_SCRIPT",
    "markdown_options",
    "select_links",
]


def markdown_options(
    *,
    only_main_content: bool = True,
    selector: str | None = None,
    include_images: bool = False,
    include_links: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """构造 Markdown 提取参数；页面脚本只接受结构化选项，不接受表达式。"""

    if not 1000 <= max_chars <= MAX_CHARS:
        raise ValueError(f"Markdown 上限必须在 1000 到 {MAX_CHARS} 之间")
    if selector is not None and (not selector.strip() or len(selector) > 200):
        raise ValueError("内容选择器必须是不超过 200 字符的非空文本")
    return {
        "onlyMainContent": only_main_content,
        "selector": selector.strip() if selector else "",
        "includeImages": include_images,
        "includeLinks": include_links,
        "maxChars": max_chars,
    }


def select_links(
    entries: Sequence[Mapping[str, Any]],
    *,
    page_url: str,
    same_origin_only: bool = False,
    contains: str | None = None,
    limit: int = DEFAULT_MAX_LINKS,
) -> tuple[dict[str, Any], ...]:
    """按来源与子串筛选链接并去重；保持页面出现顺序，便于判断导航结构。"""

    if not 1 <= limit <= MAX_LINKS:
        raise ValueError(f"链接数量上限必须在 1 到 {MAX_LINKS} 之间")
    origin = _origin(page_url)
    needle = contains.casefold() if contains else ""
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for entry in entries:
        href = str(entry.get("href", ""))
        if not href or href in seen:
            continue
        same_origin = bool(origin) and _origin(href) == origin
        if same_origin_only and not same_origin:
            continue
        text = str(entry.get("text", ""))
        if needle and needle not in href.casefold() and needle not in text.casefold():
            continue
        seen.add(href)
        item: dict[str, Any] = {"href": href, "text": text, "same_origin": same_origin}
        for key in ("rel", "target"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                item[key] = value
        selected.append(item)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


# 固定模板：主内容选取 + HTML→Markdown。只按传入的结构化选项工作，不接受任何表达式。
# 主内容判据按"语义标签优先、退化到文本量最大且链接密度低的块"排序：链接密集的容器
# 几乎总是导航或推荐位，用文本长度减链接惩罚能把正文从侧栏里挑出来。
PAGE_MARKDOWN_SCRIPT = r"""
function(options) {
  const DROP = new Set([
    'SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS','IFRAME','OBJECT',
    'EMBED','LINK','META','BUTTON','SELECT','TEXTAREA','INPUT',
  ]);
  const BOILERPLATE = new Set(['NAV','HEADER','FOOTER','ASIDE']);
  const BOILERPLATE_ROLES = new Set([
    'navigation','banner','contentinfo','complementary','search',
  ]);

  const textOf = (node) => (node && node.innerText ? node.innerText.trim() : '');

  const pickRoot = () => {
    if (options.selector) {
      const scoped = document.querySelector(options.selector);
      if (scoped) { return scoped; }
    }
    if (!document.body) { return document.documentElement; }
    if (!options.onlyMainContent) { return document.body; }
    // 语义容器只要有实质文本就直接采信；阈值取小值只为挡掉 SPA 首屏的空壳 main。
    for (const selector of ['main', '[role="main"]', 'article']) {
      const node = document.querySelector(selector);
      if (node && textOf(node).length > 40) { return node; }
    }
    let best = document.body;
    let bestScore = textOf(document.body).length * 0.5;
    for (const node of document.body.querySelectorAll('div,section,article')) {
      const length = textOf(node).length;
      if (length < 200) { continue; }
      const score = length - node.querySelectorAll('a').length * 50;
      if (score > bestScore) { bestScore = score; best = node; }
    }
    return best;
  };

  const isHidden = (node) => {
    if (node.hidden) { return true; }
    if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') { return true; }
    const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
    if (!style) { return false; }
    return style.display === 'none' || style.visibility === 'hidden';
  };

  const skip = (node) => {
    if (DROP.has(node.tagName)) { return true; }
    if (options.onlyMainContent) {
      if (BOILERPLATE.has(node.tagName)) { return true; }
      const role = node.getAttribute ? (node.getAttribute('role') || '') : '';
      if (BOILERPLATE_ROLES.has(role.toLowerCase())) { return true; }
    }
    return isHidden(node);
  };

  const absolute = (value) => {
    try { return new URL(value, document.baseURI).href; } catch (error) { return value || ''; }
  };
  const collapse = (value) => String(value == null ? '' : value).replace(/\s+/g, ' ');
  const escapeText = (value) => collapse(value).replace(/([\\`*_[\]])/g, '\\$1');

  const inlineOf = (node) => {
    let text = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { text += escapeText(child.nodeValue); continue; }
      if (child.nodeType !== 1 || skip(child)) { continue; }
      const tag = child.tagName;
      if (tag === 'BR') { text += '\n'; continue; }
      if (tag === 'IMG') {
        if (options.includeImages) {
          const alt = collapse(child.getAttribute('alt') || '');
          text += '![' + alt + '](' + absolute(child.getAttribute('src') || '') + ')';
        }
        continue;
      }
      if (tag === 'CODE' && child.closest('pre') === null) {
        text += '`' + collapse(child.innerText) + '`';
        continue;
      }
      if (tag === 'A') {
        const label = inlineOf(child).trim() || collapse(child.getAttribute('title') || '');
        const href = child.getAttribute('href');
        if (!options.includeLinks || !href || href.startsWith('javascript:')) {
          text += label;
        } else {
          text += '[' + (label || absolute(href)) + '](' + absolute(href) + ')';
        }
        continue;
      }
      if (tag === 'STRONG' || tag === 'B') {
        const inner = inlineOf(child).trim();
        text += inner ? '**' + inner + '**' : '';
        continue;
      }
      if (tag === 'EM' || tag === 'I') {
        const inner = inlineOf(child).trim();
        text += inner ? '*' + inner + '*' : '';
        continue;
      }
      text += inlineOf(child);
    }
    return text;
  };

  const blocks = [];
  const emit = (value) => {
    const text = String(value).replace(/[ \t]+\n/g, '\n').trim();
    if (text) { blocks.push(text); }
  };

  const renderList = (node, depth) => {
    const ordered = node.tagName === 'OL';
    const start = ordered ? parseInt(node.getAttribute('start') || '1', 10) || 1 : 1;
    const lines = [];
    let index = start;
    for (const item of node.children) {
      if (item.tagName !== 'LI' || skip(item)) { continue; }
      const nested = [];
      for (const child of item.children) {
        if ((child.tagName === 'UL' || child.tagName === 'OL') && !skip(child)) {
          nested.push(renderList(child, depth + 1));
        }
      }
      const own = [];
      for (const child of item.childNodes) {
        const nestedList =
          child.nodeType === 1 && (child.tagName === 'UL' || child.tagName === 'OL');
        if (nestedList) { continue; }
        if (child.nodeType === 3) { own.push(escapeText(child.nodeValue)); continue; }
        if (child.nodeType === 1 && !skip(child)) { own.push(inlineOf(child)); }
      }
      const marker = ordered ? index + '. ' : '- ';
      const label = own.join('').replace(/\s+/g, ' ').trim();
      lines.push('  '.repeat(depth) + marker + label);
      for (const block of nested) { if (block) { lines.push(block); } }
      index += 1;
    }
    return lines.join('\n');
  };

  const renderTable = (node) => {
    const rows = [];
    for (const row of node.querySelectorAll('tr')) {
      if (skip(row)) { continue; }
      const cells = [];
      for (const cell of row.children) {
        if (cell.tagName !== 'TD' && cell.tagName !== 'TH') { continue; }
        cells.push(inlineOf(cell).replace(/\s+/g, ' ').replace(/\|/g, '\\|').trim());
      }
      if (cells.length) { rows.push(cells); }
    }
    if (!rows.length) { return ''; }
    const width = Math.max.apply(null, rows.map((row) => row.length));
    const pad = (row) => {
      const copy = row.slice();
      while (copy.length < width) { copy.push(''); }
      return '| ' + copy.join(' | ') + ' |';
    };
    const lines = [pad(rows[0]), '| ' + new Array(width).fill('---').join(' | ') + ' |'];
    for (const row of rows.slice(1)) { lines.push(pad(row)); }
    return lines.join('\n');
  };

  const walk = (node) => {
    for (const child of node.children) {
      if (skip(child)) { continue; }
      const tag = child.tagName;
      if (/^H[1-6]$/.test(tag)) {
        const level = Number(tag.charAt(1));
        emit('#'.repeat(level) + ' ' + inlineOf(child).replace(/\s+/g, ' ').trim());
        continue;
      }
      if (tag === 'P') { emit(inlineOf(child)); continue; }
      if (tag === 'UL' || tag === 'OL') { emit(renderList(child, 0)); continue; }
      if (tag === 'PRE') {
        const code = child.innerText.replace(/\s+$/, '');
        if (code) { emit('```\n' + code + '\n```'); }
        continue;
      }
      if (tag === 'BLOCKQUOTE') {
        const inner = inlineOf(child).replace(/\s+/g, ' ').trim();
        if (inner) { emit('> ' + inner); }
        continue;
      }
      if (tag === 'TABLE') { emit(renderTable(child)); continue; }
      if (tag === 'HR') { emit('---'); continue; }
      if (tag === 'IMG') {
        if (options.includeImages) {
          const alt = collapse(child.getAttribute('alt') || '');
          emit('![' + alt + '](' + absolute(child.getAttribute('src') || '') + ')');
        }
        continue;
      }
      if (tag === 'DL') { emit(inlineOf(child)); continue; }
      if (child.children.length === 0) { emit(inlineOf(child)); continue; }
      walk(child);
    }
  };

  const root = pickRoot();
  walk(root);
  let markdown = blocks.join('\n\n').replace(/\n{3,}/g, '\n\n').trim();
  const total = markdown.length;
  let truncated = false;
  if (total > options.maxChars) {
    markdown = markdown.slice(0, options.maxChars);
    truncated = true;
  }
  return JSON.stringify({
    markdown: markdown,
    truncated: truncated,
    char_count: markdown.length,
    total_char_count: total,
    title: document.title || '',
    url: location.href,
    root: root === document.body ? 'body' : (root.tagName || '').toLowerCase(),
  });
}
"""

# 链接与图片清单：链接是任何"由调用方自己编排的站内遍历"的起点。
PAGE_LINKS_SCRIPT = r"""
function(options) {
  const absolute = (value) => {
    try { return new URL(value, document.baseURI).href; } catch (error) { return ''; }
  };
  const collapse = (value) => String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
  const links = [];
  for (const node of document.querySelectorAll('a[href]')) {
    const raw = node.getAttribute('href') || '';
    if (!raw || raw.startsWith('javascript:') || raw.startsWith('#')) { continue; }
    const href = absolute(raw);
    if (!href.startsWith('http')) { continue; }
    links.push({
      href: href,
      text: collapse(node.innerText || node.getAttribute('aria-label') || ''),
      rel: collapse(node.getAttribute('rel') || ''),
      target: collapse(node.getAttribute('target') || ''),
    });
    if (links.length >= options.scanLimit) { break; }
  }
  const images = [];
  if (options.includeImages) {
    for (const node of document.querySelectorAll('img[src]')) {
      const src = absolute(node.getAttribute('src') || '');
      if (!src.startsWith('http')) { continue; }
      images.push({ src: src, alt: collapse(node.getAttribute('alt') || '') });
      if (images.length >= options.scanLimit) { break; }
    }
  }
  return JSON.stringify({
    links: links,
    images: images,
    url: location.href,
    title: document.title || '',
  });
}
"""
