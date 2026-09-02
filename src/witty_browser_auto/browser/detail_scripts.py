"""详情入口和详情字段读取使用的固定 CDP 脚本。"""

CLICK_RECORD_DETAIL_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_CLICK_RECORD_DETAIL */
(() => {
  const spec = __SPEC__;
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && element.getClientRects().length > 0;
  };
  const read = (row) => {
    const target = spec.unique_selector === ':scope'
      ? row
      : row.querySelector(spec.unique_selector);
    if (!target) return '';
    if (spec.unique_source === 'text') {
      return (target.innerText || target.textContent || '').trim();
    }
    if (spec.unique_source === 'value') return String(target.value ?? '').trim();
    return String(target.getAttribute(spec.unique_source) || '').trim();
  };
  const signature = (value) => {
    let hash = 2166136261;
    for (const character of String(value || '')) {
      hash ^= character.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, '0');
  };
  try {
    const rows = Array.from(document.querySelectorAll(spec.row_selector)).filter(visible);
    for (const row of rows) {
      const key = read(row);
      const target = row.querySelector(spec.detail_trigger_selector);
      if (!key || !visible(target)) continue;
      target.scrollIntoView({block: 'center', inline: 'center'});
      const beforeUrl = location.href;
      const beforeSignature = signature(document.body ? document.body.innerText : '');
      target.click();
      return {
        clicked: true,
        unique_key: key,
        before_url: beforeUrl,
        before_signature: beforeSignature,
      };
    }
    return {clicked: false, reason: '当前列表页没有可用的详情入口'};
  } catch (error) {
    return {clicked: false, reason: String(error && error.message ? error.message : error)};
  }
})()
"""

EXTRACT_RECORD_DETAIL_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_EXTRACT_RECORD_DETAIL */
(() => {
  const expectedKey = __EXPECTED_KEY__;
  const clean = (value) => String(value || '')
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const detailRoot = document.querySelector('main,[role="main"]') || document.body;
  const rawBodyText = String(detailRoot ? detailRoot.innerText : '');
  const bodyText = clean(rawBodyText);
  const signature = (value) => {
    let hash = 2166136261;
    for (const character of String(value || '')) {
      hash ^= character.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, '0');
  };
  const errorProbe = clean(`${document.title} ${rawBodyText.slice(0, 1600)}`);
  const serverError = /(?:HTTP\s*(?:ERROR\s*)?5\d\d|ERROR\s*5\d\d)/i.test(errorProbe)
    || /(?:520\s+(?:ERROR|WEB\s+SERVER)|WEB\s+SERVER\s+RETURNED\s+AN\s+UNKNOWN)/i.test(
      errorProbe
    );
  const transientError = location.protocol === 'chrome-error:' || serverError;
  const challenge = /验证码|真人验证|安全挑战|滑块|滑动验证|人机验证/.test(
    `${document.title} ${bodyText.slice(0, 1000)}`
  );
  const pairs = new Map();
  let incompleteSemanticPairs = false;
  const semanticLabel = (value) => {
    const label = clean(value);
    return /(?:订单|编号|单号|流水|渠道|数量|金额|价格|时间|日期|状态|支付)/.test(label)
      || /(?:创建|完成|名称|规格|收货|地址|电话|备注)/.test(label)
      || /(?:order|number|id|amount|price|quantity|time|date|status|payment|channel)/i
        .test(label);
  };
  const add = (rawLabel, rawValue) => {
    const label = clean(rawLabel).replace(/[：:]$/, '').slice(0, 64);
    const value = clean(rawValue).slice(0, 4000);
    if (!label || !value || label === value || label.length > 64) return;
    if (!pairs.has(label) || value.length > pairs.get(label).length) pairs.set(label, value);
  };
  try {
    for (const row of detailRoot.querySelectorAll('table tr')) {
      const headers = Array.from(row.querySelectorAll(':scope > th'));
      const cells = Array.from(row.querySelectorAll(':scope > td'));
      if (headers.length && cells.length) {
        headers.forEach((header, index) => add(header.innerText, cells[index]?.innerText || ''));
      } else if (cells.length >= 2) {
        const semanticPairLayout = cells.every(
          (cell, index) => index % 2 === 1 || semanticLabel(cell.innerText)
        );
        if (semanticPairLayout) {
          if (cells.length % 2 !== 0) incompleteSemanticPairs = true;
          for (let index = 0; index + 1 < cells.length; index += 2) {
            add(cells[index].innerText, cells[index + 1].innerText);
          }
        } else {
          add(cells[0].innerText, cells.slice(1).map((item) => item.innerText).join(' '));
        }
      }
    }
    const terms = Array.from(detailRoot.querySelectorAll('dl dt'));
    for (const term of terms) {
      let value = term.nextElementSibling;
      if (value && value.matches('dd')) add(term.innerText, value.innerText);
    }
    for (const label of detailRoot.querySelectorAll('label')) {
      const target = label.htmlFor
        ? document.getElementById(label.htmlFor)
        : label.nextElementSibling;
      if (target) add(label.innerText, target.value ?? target.innerText);
    }
    for (const element of detailRoot.querySelectorAll(
      'li,[class*="item"],[class*="row"],[class*="detail"],[class*="info"]'
    )) {
      if (element.matches('tr') || element.closest('table')) continue;
      const children = Array.from(element.children).filter((item) => clean(item.innerText));
      if (children.length < 2 || children.length > 4) continue;
      const alternatingPairs = children.length % 2 === 0
        && children.every((child, index) => index % 2 === 1 || semanticLabel(child.innerText));
      if (alternatingPairs) {
        for (let index = 0; index < children.length; index += 2) {
          add(children[index].innerText, children[index + 1].innerText);
        }
        continue;
      }
      const label = clean(children[0].innerText);
      const value = clean(children.slice(1).map((item) => item.innerText).join(' '));
      if (label.length <= 64 && semanticLabel(label) && value && value.length <= 4000) {
        add(label, value);
      }
    }
    for (const line of rawBodyText.split(/\n+/)) {
      const match = line.match(/^([^：:]{1,64})[：:]\s*(.+)$/);
      if (match && !match[1].includes('\t') && semanticLabel(match[1])) {
        add(match[1], match[2]);
      }
    }
    const lines = rawBodyText.split(/\n+/).map(clean).filter(Boolean);
    for (let index = 0; index < lines.length - 1; index += 1) {
      if (!semanticLabel(lines[index]) || semanticLabel(lines[index + 1])) continue;
      add(lines[index], lines[index + 1]);
    }
    const details = Object.fromEntries(Array.from(pairs.entries()).slice(0, 80));
    const hasDetailBeyondIdentity = Array.from(pairs.values()).some(
      (value) => clean(value) !== clean(expectedKey)
    );
    return {
      url: location.href,
      content_signature: signature(rawBodyText),
      challenge,
      transient_error: transientError,
      error_summary: transientError ? errorProbe.slice(0, 240) : '',
      contains_expected: Boolean(expectedKey && (
        location.href.includes(expectedKey) || bodyText.includes(expectedKey)
      )),
      details,
      ready: !challenge
        && !transientError
        && !incompleteSemanticPairs
        && hasDetailBeyondIdentity,
    };
  } catch (error) {
    return {error: String(error && error.message ? error.message : error)};
  }
})()
"""
