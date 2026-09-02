"""使用固定 CDP 模板执行 DOM 结构化采集，模型只能提交受控规格。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from witty_browser_auto.browser.detail_progress import DetailProgress, DetailProgressStore
from witty_browser_auto.browser.detail_scripts import (
    CLICK_RECORD_DETAIL_TEMPLATE,
    EXTRACT_RECORD_DETAIL_TEMPLATE,
)
from witty_browser_auto.domain.extraction import (
    CollectionExtractionResult,
    CollectionExtractionSpec,
)
from witty_browser_auto.domain.models import ActionCommand, ActionKind
from witty_browser_auto.domain.protocols import AutomationDriver

logger = logging.getLogger(__name__)

_NAVIGATION_DETAIL_LABELS = frozenset(
    {
        "faq",
        "help",
        "home",
        "login",
        "logout",
        "register",
        "举报违规商家",
        "客服中心",
        "常见问题",
        "开店教学",
        "注册",
        "登录",
        "禁售目录",
        "网站首页",
        "订单查询/投诉",
        "退出登录",
        "首页",
    }
)
_DETAIL_VALUE_LABEL_SUFFIXES = (
    "下单时间",
    "创建时间",
    "支付成功时间",
    "渠道流水号",
    "订单号",
    "订单实际金额",
    "购买数量",
)


@dataclass(frozen=True, slots=True)
class _PaginationAdvance:
    page_data: dict[str, Any] | None = None
    terminal_evidence: str | None = None
    failure_reason: str | None = None
    control_seen: bool = False


@dataclass(frozen=True, slots=True)
class _DetailExtraction:
    count: int = 0
    failed_keys: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    evidence: str | None = None
    failure_reason: str | None = None
    interrupted_by_security_challenge: bool = False


_EXTRACT_PAGE_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_EXTRACT_PAGE */
(() => {
  const spec = __SPEC__;
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && element.getClientRects().length > 0;
  };
  const read = (row, field) => {
    const target = field.selector === ':scope' ? row : row.querySelector(field.selector);
    if (!target) return '';
    if (field.source === 'text') return (target.innerText || target.textContent || '').trim();
    if (field.source === 'value') return String(target.value ?? '').trim();
    return String(target.getAttribute(field.source) || '').trim();
  };
  const maximumNumber = (selector) => {
    if (!selector) return null;
    const target = document.querySelector(selector);
    if (!target) return null;
    const matches = (target.innerText || target.textContent || '').match(/\d[\d,]*/g) || [];
    const numbers = matches.map((item) => Number(item.replaceAll(',', ''))).filter(Number.isFinite);
    return numbers.length ? Math.max(...numbers) : null;
  };
  const hash = (value) => {
    let result = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      result ^= value.charCodeAt(index);
      result = Math.imul(result, 16777619);
    }
    return (result >>> 0).toString(16).padStart(8, '0');
  };
  try {
    const rowNodes = Array.from(document.querySelectorAll(spec.row_selector))
      .filter(visible)
      .slice(0, Math.min(spec.max_items, 2000));
    const rows = rowNodes.map((row) => Object.fromEntries(
      spec.fields.map((field) => [field.name, read(row, field)])
    ));
    const control = spec.pagination_control_selector
      ? document.querySelector(spec.pagination_control_selector)
      : null;
    const controlDisabled = !control
      || control.matches(':disabled,[disabled],[aria-disabled="true"]')
      || Array.from(control.classList || [])
        .some((name) => name.toLowerCase().includes('disabled'));
    return {
      rows,
      fingerprint: hash(JSON.stringify(rows) + '|' + location.href),
      declared_total: maximumNumber(spec.total_count_selector),
      declared_pages: maximumNumber(spec.total_pages_selector),
      current_page: maximumNumber(spec.current_page_selector),
      pagination_exists: Boolean(control),
      pagination_disabled: Boolean(controlDisabled),
    };
  } catch (error) {
    return {error: String(error && error.message ? error.message : error)};
  }
})()
"""

_CLICK_NEXT_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_CLICK_NEXT */
(() => {
  const selector = __SELECTOR__;
  try {
    const target = document.querySelector(selector);
    if (!target) return {clicked: false, reason: '下一页目标不存在'};
    const disabled = target.matches(':disabled,[disabled],[aria-disabled="true"]')
      || Array.from(target.classList || []).some((name) => name.toLowerCase().includes('disabled'));
    if (disabled) return {clicked: false, reason: '下一页目标已禁用'};
    target.scrollIntoView({block: 'center', inline: 'center'});
    target.click();
    return {clicked: true};
  } catch (error) {
    return {clicked: false, reason: String(error && error.message ? error.message : error)};
  }
})()
"""

_CLICK_LOAD_MORE_TEMPLATE = _CLICK_NEXT_TEMPLATE.replace(
    "WITTY_BROWSER_AUTO_CLICK_NEXT",
    "WITTY_BROWSER_AUTO_CLICK_LOAD_MORE",
).replace("下一页", "加载更多")

_CLICK_PAGE_NUMBER_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_CLICK_PAGE_NUMBER */
(() => {
  const selector = __SELECTOR__;
  const targetPage = __TARGET_PAGE__;
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && element.getClientRects().length > 0;
  };
  const exactNumber = (value) => {
    const matches = String(value || '').match(/\d+/g) || [];
    return matches.length === 1 && Number(matches[0]) === targetPage;
  };
  try {
    const candidates = Array.from(document.querySelectorAll(selector)).filter(visible);
    const exact = candidates.filter((item) => (
      exactNumber(item.innerText || item.textContent)
      || exactNumber(item.getAttribute('data-page'))
      || exactNumber(item.getAttribute('data-page-number'))
      || exactNumber(item.getAttribute('aria-label'))
    ));
    if (exact.length !== 1) {
      return {
        clicked: false,
        reason: exact.length
          ? `目标页码 ${targetPage} 匹配到多个可见控件`
          : `目标页码 ${targetPage} 没有匹配到可见控件`,
      };
    }
    const target = exact[0];
    const disabled = target.matches(':disabled,[disabled],[aria-disabled="true"]')
      || Array.from(target.classList || [])
        .some((name) => name.toLowerCase().includes('disabled'));
    if (disabled) return {clicked: false, reason: `目标页码 ${targetPage} 已禁用`};
    target.scrollIntoView({block: 'center', inline: 'center'});
    target.click();
    return {clicked: true, target_page: targetPage};
  } catch (error) {
    return {clicked: false, reason: String(error && error.message ? error.message : error)};
  }
})()
"""

_SCROLL_MORE_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_SCROLL_MORE */
(() => {
  const selector = __SELECTOR__;
  try {
    const target = selector ? document.querySelector(selector) : document.scrollingElement;
    if (!target) return {scrolled: false, reason: '滚动容器不存在'};
    const isDocument = target === document.scrollingElement;
    const before = Number(isDocument ? window.scrollY : target.scrollTop || 0);
    const scrollHeight = Number(target.scrollHeight || document.documentElement.scrollHeight || 0);
    const clientHeight = Number(isDocument ? window.innerHeight : target.clientHeight || 0);
    const maximumTop = Math.max(0, scrollHeight - clientHeight);
    // 重叠 20% 的分段滚动可覆盖虚拟列表每个窗口，避免直接跳到底部漏掉中间记录。
    const step = Math.max(160, Math.floor(clientHeight * 0.8));
    const destination = Math.min(maximumTop, before + step);
    if (isDocument) {
      window.scrollTo(0, destination);
    } else if (typeof target.scrollTo === 'function') {
      target.scrollTo({top: destination, behavior: 'auto'});
    } else {
      target.scrollTop = destination;
    }
    return {
      scrolled: true,
      before,
      after: Number(isDocument ? window.scrollY : target.scrollTop || 0),
      scroll_height: scrollHeight,
      client_height: clientHeight,
      at_bottom: destination >= maximumTop - 1,
    };
  } catch (error) {
    return {scrolled: false, reason: String(error && error.message ? error.message : error)};
  }
})()
"""

_INSPECT_COLLECTION_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_INSPECT_COLLECTION */
(() => {
  const rootSelector = __ROOT_SELECTOR__;
  const maximum = __MAXIMUM__;
  const visible = (element) => {
    const style = getComputedStyle(element);
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && element.getClientRects().length > 0;
  };
  const escaped = (value) => CSS.escape(String(value));
  const signature = (element) => {
    const classes = Array.from(element.classList || [])
      .filter((name) => /^[A-Za-z_-][A-Za-z0-9_-]{0,80}$/.test(name) && !/\d{4,}/.test(name))
      .slice(0, 3)
      .map(escaped);
    return element.localName + (classes.length ? '.' + classes.join('.') : '');
  };
  const path = (element, stop) => {
    const parts = [];
    let current = element;
    while (current && current !== stop && current instanceof Element) {
      let part = signature(current);
      if (current.parentElement) {
        const siblings = Array.from(current.parentElement.children);
        const same = siblings.filter((item) => signature(item) === part);
        if (same.length > 1) {
          const sameTag = siblings.filter((item) => item.localName === current.localName);
          part += `:nth-of-type(${sameTag.indexOf(current) + 1})`;
        }
      }
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(' > ');
  };
  const compactPath = (element, stop) => {
    if (element.id) {
      const byId = `#${escaped(element.id)}`;
      if (document.querySelectorAll(byId).length === 1) return byId;
    }
    const direct = signature(element);
    if (document.querySelectorAll(direct).length === 1) return direct;
    const parts = [];
    let current = element;
    while (current && current !== stop && current instanceof Element) {
      parts.unshift(signature(current));
      const candidate = parts.join(' > ');
      if (document.querySelectorAll(candidate).length === 1) return candidate;
      current = current.parentElement;
    }
    return path(element, stop);
  };
  const sourceOptions = (element) => {
    const options = ['text'];
    if ('value' in element) options.push('value');
    for (const name of ['href', 'src', 'title']) {
      if (element.hasAttribute(name)) options.push(name);
    }
    return options;
  };
  const cleanLabel = (value) => String(value || '')
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 64);
  const structuralLabel = (element, index) => cleanLabel(
    element.getAttribute('data-label')
    || element.getAttribute('data-field')
    || element.getAttribute('name')
    || Array.from(element.classList || []).find((name) => (
      /^[A-Za-z_][A-Za-z0-9_-]{1,63}$/.test(name)
      && !/^(cell|item|row|col|text|value|content)$/i.test(name)
    ))
    || `field_${index + 1}`
  );
  const explicitPaginationHint = () => {
    const patterns = [
      {
        items: '.arco-pagination li.arco-pagination-item',
        current: '.arco-pagination li.arco-pagination-item-active',
        total: '.arco-pagination li.arco-pagination-item:last-of-type',
      },
      {
        items: '.ant-pagination-item',
        current: '.ant-pagination-item-active',
        total: '.ant-pagination-item:last-of-type',
      },
      {
        items: '.el-pager > li.number',
        current: '.el-pager > li.number.active',
        total: '.el-pager > li.number:last-of-type',
      },
      {
        items: '.pagination .page-item:not(.prev):not(.next)',
        current: '.pagination .page-item.active',
        total: '.pagination .page-item:not(.prev):not(.next):last-of-type',
      },
    ];
    for (const pattern of patterns) {
      const items = Array.from(document.querySelectorAll(pattern.items)).filter(visible);
      const current = document.querySelector(pattern.current);
      const total = document.querySelector(pattern.total);
      if (items.length >= 2 && current && total) {
        return {
          mode: 'page_number',
          page_number_selector: pattern.items,
          current_page_selector: pattern.current,
          total_pages_selector: pattern.total,
        };
      }
    }
    const nextPatterns = [
      '.ant-pagination-next',
      '.el-pagination .btn-next',
      '.pagination .next',
      '[rel="next"]',
    ];
    for (const selector of nextPatterns) {
      if (Array.from(document.querySelectorAll(selector)).some(visible)) {
        return {mode: 'next', next_page_selector: selector};
      }
    }
    const loadMorePatterns = [
      '.ant-list-load-more button',
      '.el-button.load-more',
      '.load-more',
      '[data-testid="load-more"]',
      '[aria-label*="load more" i]',
      '[aria-label*="加载更多"]',
    ];
    for (const selector of loadMorePatterns) {
      if (Array.from(document.querySelectorAll(selector)).some(visible)) {
        return {mode: 'load_more', load_more_selector: selector};
      }
    }
    const semanticLoadMore = Array.from(document.querySelectorAll('button,a,[role="button"]'))
      .filter(visible)
      .find((item) => /^(加载更多|显示更多|更多|load more|show more)$/i.test(
        cleanLabel(item.innerText || item.textContent || item.getAttribute('aria-label'))
      ));
    if (semanticLoadMore) {
      return {mode: 'load_more', load_more_selector: compactPath(semanticLoadMore, document.body)};
    }
    return null;
  };
  const scrollHint = (parent, root) => {
    let current = parent;
    while (current && current instanceof Element) {
      const style = getComputedStyle(current);
      const overflowY = String(style.overflowY || '').toLowerCase();
      const scrollable = /^(auto|scroll|overlay)$/.test(overflowY)
        && current.scrollHeight > current.clientHeight + 2
        && current.clientHeight > 40;
      if (scrollable) {
        const identity = `${current.className || ''} ${current.getAttribute('role') || ''}`;
        const virtualized = /(virtual|virtuoso|window|viewport)/i.test(identity)
          || Boolean(current.querySelector('[data-virtualized], [data-virtuoso-scroller]'));
        return {
          mode: 'infinite_scroll',
          scroll_container_selector: compactPath(current, root.parentElement),
          scroll_kind: virtualized ? 'virtualized' : 'incremental',
        };
      }
      if (current === root) break;
      current = current.parentElement;
    }
    const identity = `${parent.className || ''} ${parent.getAttribute('role') || ''}`;
    if (/(infinite|virtual|virtuoso|react-window)/i.test(identity)) {
      return {mode: 'infinite_scroll', scroll_kind: 'virtualized'};
    }
    return null;
  };
  try {
    const root = document.querySelector(rootSelector);
    if (!root) return {error: '观察根选择器没有匹配元素'};
    const candidates = [];
    const explicitPagination = explicitPaginationHint();
    for (const parent of Array.from(root.querySelectorAll('*')).slice(0, 5000)) {
      const children = Array.from(parent.children).filter(visible);
      if (children.length < 2) continue;
      const groups = new Map();
      for (const child of children) {
        const key = signature(child);
        const group = groups.get(key) || [];
        group.push(child);
        groups.set(key, group);
      }
      for (const [rowSignature, rows] of groups.entries()) {
        if (rows.length < 2) continue;
        const first = rows[0];
        const text = (first.innerText || first.textContent || '').trim();
        if (!text) continue;
        const table = first.matches('tr') ? first.closest('table') : null;
        const parentPath = table && parent.matches('tbody')
          ? `${compactPath(table, root.parentElement)} > tbody`
          : compactPath(parent, root.parentElement);
        const headers = table
          ? Array.from(table.querySelectorAll('thead th')).map((item) => (
            cleanLabel(item.innerText || item.textContent)
          ))
          : [];
        const fieldNodes = table
          ? Array.from(first.children).filter(visible)
          : Array.from(first.querySelectorAll('*'))
            .filter((item) => visible(item) && (
              item.children.length === 0 || item.matches('a,input,img,time,button')
            ))
            .slice(0, 12);
        const childHints = fieldNodes.slice(0, 50).map((item, index) => {
          const sameTag = Array.from(first.children)
            .filter((nested) => nested.localName === item.localName);
          const directSelector = sameTag.includes(item)
            ? `:scope > ${item.localName}:nth-of-type(${sameTag.indexOf(item) + 1})`
            : path(item, first);
          return {
            selector: directSelector,
            label: headers[index] || structuralLabel(item, index),
            role: item.getAttribute('role') || '',
            source_options: sourceOptions(item),
          };
        });
        const detailHints = Array.from(first.querySelectorAll('a,button,[role="button"]'))
          .filter(visible)
          .slice(0, 20)
          .map((item, index) => ({
            selector: path(item, first),
            label: cleanLabel(
              item.innerText
              || item.textContent
              || item.getAttribute('aria-label')
              || item.getAttribute('title')
              || `detail_${index + 1}`
            ),
            role: item.getAttribute('role') || item.localName,
          }))
          .filter((item) => item.selector && item.label);
        candidates.push({
          row_selector: `${parentPath} > ${rowSignature}`,
          row_count: rows.length,
          child_hints: childHints,
          detail_hints: detailHints,
          pagination_hint: explicitPagination || scrollHint(parent, root) || {mode: 'none'},
          score: rows.length * Math.min(text.length, 200),
        });
      }
    }
    candidates.sort((left, right) => right.score - left.score);
    return {
      candidates: candidates.slice(0, maximum),
      pagination_hint: explicitPagination || {mode: 'none'},
    };
  } catch (error) {
    return {error: String(error && error.message ? error.message : error)};
  }
})()
"""


class CdpDomCollectionExtractor:
    """把已验证规格编译为固定脚本，并负责确定性分页与私有导出。"""

    def __init__(
        self,
        driver: AutomationDriver,
        artifact_root: Path,
        *,
        detail_progress_root: Path | None = None,
        detail_retry_delays_seconds: tuple[float, ...] = (2.0, 8.0, 30.0),
        detail_success_delay_seconds: float = 2.0,
    ) -> None:
        if any(delay < 0 for delay in detail_retry_delays_seconds):
            raise ValueError("详情重试等待时间不能为负数")
        if detail_success_delay_seconds < 0:
            raise ValueError("详情成功节流时间不能为负数")
        self.driver = driver
        self.artifact_root = artifact_root
        self.detail_progress_root = detail_progress_root or artifact_root
        self.detail_retry_delays_seconds = detail_retry_delays_seconds
        self.detail_success_delay_seconds = detail_success_delay_seconds

    async def inspect(
        self,
        *,
        root_selector: str = "body",
        max_candidates: int = 12,
    ) -> dict[str, Any]:
        if not root_selector.strip() or len(root_selector) > 512:
            raise ValueError("集合观察根选择器不能为空且不能超过 512 个字符")
        if not 1 <= max_candidates <= 30:
            raise ValueError("集合候选数量必须在 1 到 30 之间")
        script = _INSPECT_COLLECTION_TEMPLATE.replace(
            "__ROOT_SELECTOR__",
            json.dumps(root_selector, ensure_ascii=False),
        ).replace("__MAXIMUM__", str(max_candidates))
        value = await self._evaluate(script, idempotent=True)
        if not isinstance(value, dict):
            raise RuntimeError("集合结构观察返回了无效数据")
        if value.get("error"):
            raise ValueError(f"集合结构观察失败：{value['error']}")
        candidates = value.get("candidates")
        if not isinstance(candidates, list):
            raise RuntimeError("集合结构观察缺少候选数组")
        pagination_hint = value.get("pagination_hint")
        return {
            "candidates": candidates[:max_candidates],
            "pagination_hint": (
                pagination_hint if isinstance(pagination_hint, dict) else {"mode": "none"}
            ),
        }

    async def probe_entry(self, spec: CollectionExtractionSpec) -> dict[str, Any]:
        """只读探测入口页首屏的结构事实，供程序验证门与热重放前置检查使用。

        只执行一次固定采集模板，不翻页、不点击，页面状态保持不变。
        """

        page = self._validate_page(await self._extract_page(spec), 1)
        rows = page["rows"]
        field_non_empty = {
            field.name: sum(1 for row in rows if row.get(field.name, "").strip())
            for field in spec.fields
        }
        return {
            "row_count": len(rows),
            "unique_key_filled_count": sum(
                1 for row in rows if row.get(spec.unique_key, "").strip()
            ),
            "field_non_empty": field_non_empty,
            "declared_total": page["declared_total"],
            "declared_pages": page["declared_pages"],
            "current_page": page["current_page"],
            "pagination_exists": page["pagination_exists"],
            "pagination_disabled": page["pagination_disabled"],
        }

    async def extract(
        self,
        spec: CollectionExtractionSpec,
    ) -> CollectionExtractionResult:
        unique_items: dict[str, dict[str, str]] = {}
        duplicate_count = 0
        visited_pages: list[int] = []
        failed_pages: list[int] = []
        failure_reasons: list[str] = []
        declared_total: int | None = None
        declared_pages: int | None = None
        seen_fingerprints: set[str] = set()
        pagination_control_seen = False
        terminal_evidence: str | None = None
        page_data = await self._extract_page(spec)
        if spec.pagination_mode == "page_number":
            try:
                page_data = await self._prepare_first_numbered_page(spec, page_data)
            except (RuntimeError, ValueError) as exc:
                return self._failed_result(spec, str(exc), failed_page=1)

        for page_number in range(1, spec.max_pages + 1):
            try:
                page = self._validate_page(page_data, page_number)
            except (TypeError, ValueError) as exc:
                failed_pages.append(page_number)
                failure_reasons.append(str(exc))
                break
            fingerprint = page["fingerprint"]
            if fingerprint in seen_fingerprints:
                failed_pages.append(page_number)
                failure_reasons.append(f"第 {page_number} 页指纹与已访问页面重复，分页没有进展")
                break
            seen_fingerprints.add(fingerprint)
            visited_pages.append(page_number)
            declared_total = self._merge_declared_number(
                declared_total,
                page["declared_total"],
                "页面声明总数",
                failure_reasons,
            )
            declared_pages = self._merge_declared_number(
                declared_pages,
                page["declared_pages"],
                "页面声明页数",
                failure_reasons,
            )
            keys_on_snapshot: set[str] = set()
            cumulative_snapshot = spec.pagination_mode in {"load_more", "infinite_scroll"}
            for row_index, item in enumerate(page["rows"], start=1):
                key = item.get(spec.unique_key, "").strip()
                if not key:
                    failed_pages.append(page_number)
                    failure_reasons.append(
                        f"第 {page_number} 页第 {row_index} 条记录缺少唯一键 {spec.unique_key}"
                    )
                    break
                if key in unique_items:
                    if not cumulative_snapshot or key in keys_on_snapshot:
                        duplicate_count += 1
                else:
                    unique_items[key] = item
                    if len(unique_items) > spec.max_items:
                        failed_pages.append(page_number)
                        failure_reasons.append(f"去重后记录超过最大条数 {spec.max_items}")
                        break
                keys_on_snapshot.add(key)
            if failure_reasons:
                break

            advance = await self._advance_pagination(
                spec,
                page,
                page_number=page_number,
                declared_pages=declared_pages,
                previous_fingerprint=fingerprint,
                control_seen=pagination_control_seen,
            )
            pagination_control_seen = advance.control_seen
            if advance.failure_reason:
                failed_pages.append(page_number + 1)
                failure_reasons.append(advance.failure_reason)
                break
            if advance.terminal_evidence:
                terminal_evidence = advance.terminal_evidence
                break
            if advance.page_data is None:
                break
            page_data = advance.page_data
        else:
            failure_reasons.append(f"达到最大页数 {spec.max_pages}，仍未发现分页终点")

        if declared_pages is not None and len(visited_pages) != declared_pages:
            failure_reasons.append(
                f"页面声明 {declared_pages} 页，代码实际完成 {len(visited_pages)} 页"
            )
        if declared_total is not None and len(unique_items) != declared_total:
            failure_reasons.append(
                f"页面声明总数 {declared_total}，代码去重后得到 {len(unique_items)} 条"
            )

        completion_evidence: list[str] = []
        if declared_total is not None and len(unique_items) == declared_total:
            completion_evidence.append("页面声明总数与代码去重计数一致")
        if declared_pages is not None and len(visited_pages) == declared_pages:
            completion_evidence.append("页面声明页数与代码已访问页一致")
        if terminal_evidence:
            completion_evidence.append(terminal_evidence)

        detail_result = _DetailExtraction()
        if spec.detail_trigger_selector and not failure_reasons:
            detail_result = await self._extract_record_details(spec, unique_items)
            if detail_result.evidence:
                completion_evidence.append(detail_result.evidence)
            if detail_result.failure_reason:
                failure_reasons.append(detail_result.failure_reason)

        if not completion_evidence and not failure_reasons:
            failure_reasons.append("缺少页面声明总数、声明总页数或可验证分页终点等完整性证据")
        complete = not failure_reasons and bool(visited_pages)
        filtered_items = [
            item
            for item in unique_items.values()
            if all(filter_rule.matches(item) for filter_rule in spec.filters)
        ]
        json_path: Path | None = None
        csv_path: Path | None = None
        if complete:
            try:
                json_path, csv_path = await asyncio.to_thread(
                    self._export,
                    spec,
                    filtered_items,
                    len(unique_items),
                    duplicate_count,
                    tuple(visited_pages),
                    declared_total,
                    declared_pages,
                    tuple(completion_evidence),
                )
            except Exception as exc:
                complete = False
                failure_reasons.append(f"结构化结果导出失败：{exc}")

        result = CollectionExtractionResult(
            collection_name=spec.collection_name,
            complete=complete,
            unique_count=len(unique_items),
            exported_count=len(filtered_items),
            duplicate_count=duplicate_count,
            visited_pages=tuple(visited_pages),
            failed_pages=tuple(dict.fromkeys(failed_pages)),
            declared_total=declared_total,
            declared_pages=declared_pages,
            completion_evidence=tuple(completion_evidence),
            failure_reasons=tuple(failure_reasons),
            json_path=json_path,
            csv_path=csv_path,
            pagination_mode=spec.pagination_mode,
            detail_requested=spec.detail_trigger_selector is not None,
            detail_count=detail_result.count,
            detail_failed_keys=detail_result.failed_keys,
            detail_fields=detail_result.fields,
            interrupted_by_security_challenge=(detail_result.interrupted_by_security_challenge),
        )
        logger.info(
            "结构化采集代码执行结束",
            extra={
                "collection": spec.collection_name,
                "complete": result.complete,
                "visited_pages": len(result.visited_pages),
                "unique_count": result.unique_count,
                "exported_count": result.exported_count,
                "duplicate_count": result.duplicate_count,
                "failed_pages": len(result.failed_pages),
            },
        )
        return result

    async def _extract_record_details(
        self,
        spec: CollectionExtractionSpec,
        unique_items: dict[str, dict[str, str]],
    ) -> _DetailExtraction:
        if not unique_items:
            return _DetailExtraction(failure_reason="列表没有可用于详情采集的记录")
        progress_store = DetailProgressStore(self.detail_progress_root, spec, unique_items)
        try:
            progress = await asyncio.to_thread(progress_store.load)
        except OSError as exc:
            logger.warning(
                "详情采集断点读取失败，将重新建立路由",
                extra={"collection": spec.collection_name, "error_type": type(exc).__name__},
            )
            progress = None
        if progress is not None and self._origin(progress.route_prefix) == progress.source_origin:
            prefix = progress.route_prefix
            suffix = progress.route_suffix
            source_origin = progress.source_origin
            details_by_key = self._validated_progress_details(progress, unique_items)
            dropped_count = len(progress.details_by_key) - len(details_by_key)
            logger.info(
                "已恢复详情采集断点",
                extra={
                    "collection": spec.collection_name,
                    "completed_count": len(details_by_key),
                    "dropped_count": dropped_count,
                    "total_count": len(unique_items),
                },
            )
        else:
            prepared = await self._prepare_detail_progress(spec, unique_items)
            if isinstance(prepared, _DetailExtraction):
                return prepared
            progress = prepared
            prefix = progress.route_prefix
            suffix = progress.route_suffix
            source_origin = progress.source_origin
            details_by_key = dict(progress.details_by_key)
            try:
                await asyncio.to_thread(progress_store.save, progress)
            except (OSError, ValueError) as exc:
                return self._incomplete_details(
                    unique_items,
                    details_by_key,
                    f"详情采集断点写入失败：{exc}",
                )

        pending_keys = [key for key in unique_items if key not in details_by_key]
        if pending_keys and details_by_key and self.detail_success_delay_seconds:
            await asyncio.sleep(self.detail_success_delay_seconds)
        for index, key in enumerate(pending_keys):
            detail_url = f"{prefix}{quote(key, safe='')}{suffix}"
            if self._origin(detail_url) != source_origin:
                return self._incomplete_details(
                    unique_items,
                    details_by_key,
                    "详情路由越过原始站点，已熔断；可用断点已保留",
                )
            fields, challenge, failure_reason = await self._fetch_detail_fields(
                key,
                detail_url,
                baseline_item=unique_items[key],
                timeout_seconds=spec.page_wait_timeout_seconds,
            )
            if challenge:
                return self._incomplete_details(
                    unique_items,
                    details_by_key,
                    "批量详情采集过程中出现新的安全挑战，已保留断点和当前页面等待视觉链路处理",
                    interrupted_by_security_challenge=True,
                )
            if fields is None:
                logger.warning(
                    "详情采集连续失败后熔断",
                    extra={
                        "collection": spec.collection_name,
                        "completed_count": len(details_by_key),
                        "total_count": len(unique_items),
                    },
                )
                return self._incomplete_details(
                    unique_items,
                    details_by_key,
                    f"详情采集已熔断并保留断点：{failure_reason}",
                )
            details_by_key[key] = fields
            try:
                await asyncio.to_thread(
                    progress_store.save,
                    DetailProgress(prefix, suffix, source_origin, details_by_key),
                )
            except (OSError, ValueError) as exc:
                return self._incomplete_details(
                    unique_items,
                    details_by_key,
                    f"详情采集断点写入失败：{exc}",
                )
            if index < len(pending_keys) - 1 and self.detail_success_delay_seconds:
                await asyncio.sleep(self.detail_success_delay_seconds)

        detail_fields = self._merge_detail_records(unique_items, details_by_key)
        await asyncio.to_thread(progress_store.clear)
        return _DetailExtraction(
            count=len(details_by_key),
            fields=detail_fields,
            evidence=f"已按唯一键验证并合并 {len(details_by_key)}/{len(unique_items)} 条记录详情",
        )

    async def _prepare_detail_progress(
        self,
        spec: CollectionExtractionSpec,
        unique_items: Mapping[str, dict[str, str]],
    ) -> DetailProgress | _DetailExtraction:
        unique_field = next(field for field in spec.fields if field.name == spec.unique_key)
        click_script = CLICK_RECORD_DETAIL_TEMPLATE.replace(
            "__SPEC__",
            json.dumps(
                {
                    "row_selector": spec.row_selector,
                    "unique_selector": unique_field.selector,
                    "unique_source": unique_field.source,
                    "detail_trigger_selector": spec.detail_trigger_selector,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        click_result = await self._evaluate(click_script, idempotent=False)
        if not isinstance(click_result, dict) or not click_result.get("clicked"):
            reason = (
                click_result.get("reason")
                if isinstance(click_result, dict)
                else "详情入口动作返回了无效数据"
            )
            return _DetailExtraction(failure_reason=str(reason))
        first_key = str(click_result.get("unique_key", "")).strip()
        if not first_key or first_key not in unique_items:
            return _DetailExtraction(failure_reason="详情入口对应的唯一键不属于已采集列表")
        before_url = str(click_result.get("before_url", "")).strip()
        before_signature = str(click_result.get("before_signature", "")).strip()
        first_detail = await self._wait_for_record_detail(
            first_key,
            timeout_seconds=spec.page_wait_timeout_seconds,
            previous_url=before_url,
            previous_signature=before_signature,
        )
        if first_detail is None:
            return _DetailExtraction(
                failed_keys=tuple(unique_items),
                failure_reason=f"详情页未在 {spec.page_wait_timeout_seconds:g} 秒内返回可验证字段",
            )
        if first_detail.get("challenge") is True:
            return _DetailExtraction(
                failed_keys=tuple(unique_items),
                failure_reason="进入详情时出现新的安全挑战，已保留当前页面等待视觉链路处理",
                interrupted_by_security_challenge=True,
            )
        first_url = str(first_detail.get("url", ""))
        route_template = self._detail_url_template(first_url, first_key)
        if route_template is None:
            # SPA 可能先更新列表内容签名，随后才提交详情路由。继续等到带唯一键的
            # 路由稳定，避免把导航中间态误判为弹窗或固定详情页。
            routed_detail = await self._wait_for_keyed_detail_route(
                first_key,
                timeout_seconds=spec.page_wait_timeout_seconds,
            )
            if routed_detail is not None:
                first_detail = routed_detail
                if first_detail.get("challenge") is True:
                    return _DetailExtraction(
                        failed_keys=tuple(unique_items),
                        failure_reason=(
                            "进入详情时出现新的安全挑战，已保留当前页面等待视觉链路处理"
                        ),
                        interrupted_by_security_challenge=True,
                    )
                first_url = str(first_detail.get("url", ""))
                route_template = self._detail_url_template(first_url, first_key)
        if route_template is None:
            return _DetailExtraction(
                failed_keys=tuple(unique_items),
                failure_reason=(
                    "详情 URL 不包含列表唯一键，当前页面需要工程扩展处理弹窗或非参数化详情"
                ),
            )
        details_by_key: dict[str, dict[str, str]] = {}
        if first_detail.get("ready") is True and first_detail.get("transient_error") is not True:
            try:
                details_by_key[first_key] = self._validated_detail_fields(
                    first_detail,
                    first_key,
                    baseline_item=unique_items[first_key],
                )
            except ValueError:
                pass
        prefix, suffix = route_template
        source_origin = self._origin(first_url)
        if not source_origin:
            return _DetailExtraction(
                failed_keys=tuple(unique_items),
                failure_reason="详情 URL 缺少有效站点来源，已停止批量导航",
            )
        return DetailProgress(prefix, suffix, source_origin, details_by_key)

    async def _wait_for_keyed_detail_route(
        self,
        expected_key: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            detail = await self._extract_record_detail(expected_key)
            if detail.get("challenge") is True or detail.get("transient_error") is True:
                return detail
            current_url = str(detail.get("url", "")).strip()
            if (
                detail.get("ready") is True
                and detail.get("contains_expected") is True
                and self._detail_url_template(current_url, expected_key) is not None
            ):
                return detail
            await asyncio.sleep(min(0.1, timeout_seconds / 2))
        return None

    async def _fetch_detail_fields(
        self,
        key: str,
        detail_url: str,
        *,
        baseline_item: Mapping[str, str],
        timeout_seconds: float,
    ) -> tuple[dict[str, str] | None, bool, str]:
        failure_reason = "详情页没有返回可验证字段"
        attempts = len(self.detail_retry_delays_seconds) + 1
        for attempt in range(attempts):
            try:
                receipt = await self.driver.execute(
                    ActionCommand(
                        action_id=f"structured-detail-{uuid.uuid4().hex}",
                        kind=ActionKind.NAVIGATE,
                        url=detail_url,
                        idempotent=True,
                    )
                )
                if not receipt.success:
                    failure_reason = f"详情导航失败：{receipt.message}"
                else:
                    detail = await self._wait_for_record_detail(
                        key,
                        timeout_seconds=timeout_seconds,
                    )
                    if detail is not None and detail.get("challenge") is True:
                        return None, True, "详情页出现安全挑战"
                    if detail is not None and detail.get("transient_error") is True:
                        failure_reason = "详情页返回临时服务错误"
                    elif detail is None:
                        failure_reason = f"详情页未在 {timeout_seconds:g} 秒内返回可验证字段"
                    else:
                        return (
                            self._validated_detail_fields(
                                detail,
                                key,
                                baseline_item=baseline_item,
                            ),
                            False,
                            "",
                        )
            except (RuntimeError, ValueError) as exc:
                failure_reason = str(exc)
            if attempt >= len(self.detail_retry_delays_seconds):
                break
            delay = self.detail_retry_delays_seconds[attempt]
            logger.warning(
                "详情采集单项失败，等待后重试",
                extra={"attempt": attempt + 1, "max_attempts": attempts, "delay_seconds": delay},
            )
            if delay:
                await asyncio.sleep(delay)
        return None, False, failure_reason

    def _incomplete_details(
        self,
        unique_items: Mapping[str, dict[str, str]],
        details_by_key: Mapping[str, Mapping[str, str]],
        reason: str,
        *,
        interrupted_by_security_challenge: bool = False,
    ) -> _DetailExtraction:
        return _DetailExtraction(
            count=len(details_by_key),
            failed_keys=tuple(key for key in unique_items if key not in details_by_key),
            fields=self._merge_detail_records(unique_items, details_by_key),
            failure_reason=(f"{reason}；详情覆盖 {len(details_by_key)}/{len(unique_items)}"),
            interrupted_by_security_challenge=interrupted_by_security_challenge,
        )

    async def _wait_for_record_detail(
        self,
        expected_key: str,
        *,
        timeout_seconds: float,
        previous_url: str = "",
        previous_signature: str = "",
    ) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            detail = await self._extract_record_detail(expected_key)
            if detail.get("challenge") is True or detail.get("transient_error") is True:
                return detail
            current_url = str(detail.get("url", "")).strip()
            current_signature = str(detail.get("content_signature", "")).strip()
            surface_changed = bool(
                (previous_url and current_url and current_url != previous_url)
                or (
                    previous_signature
                    and current_signature
                    and current_signature != previous_signature
                )
                or (not previous_url and not previous_signature)
            )
            if (
                surface_changed
                and detail.get("ready") is True
                and detail.get("contains_expected") is True
            ):
                return detail
            await asyncio.sleep(min(0.1, timeout_seconds / 2))
        return None

    async def _extract_record_detail(self, expected_key: str) -> dict[str, Any]:
        script = EXTRACT_RECORD_DETAIL_TEMPLATE.replace(
            "__EXPECTED_KEY__",
            json.dumps(expected_key, ensure_ascii=False),
        )
        value = await self._evaluate(script, idempotent=True)
        if not isinstance(value, dict):
            raise RuntimeError("详情页结构化采集返回了无效数据")
        if value.get("error"):
            raise ValueError(f"详情页结构化采集失败：{value['error']}")
        return value

    @staticmethod
    def _detail_url_template(url: str, unique_key: str) -> tuple[str, str] | None:
        for marker in (unique_key, quote(unique_key, safe="")):
            index = url.find(marker)
            if index >= 0:
                return url[:index], url[index + len(marker) :]
        return None

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""

    @staticmethod
    def _validated_detail_fields(
        value: Mapping[str, Any],
        unique_key: str,
        *,
        baseline_item: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        if value.get("contains_expected") is not True:
            raise ValueError(f"详情页没有回显唯一键 {unique_key}")
        raw_details = value.get("details")
        if not isinstance(raw_details, Mapping) or not raw_details:
            raise ValueError(f"详情页 {unique_key} 没有可合并字段")
        fields: dict[str, str] = {}
        for raw_label, raw_value in raw_details.items():
            label = str(raw_label).strip()
            field_value = str(raw_value).strip()
            if not label or len(label) > 64 or not field_value or len(field_value) > 4000:
                continue
            if label.casefold() in _NAVIGATION_DETAIL_LABELS:
                continue
            if field_value.endswith(_DETAIL_VALUE_LABEL_SUFFIXES):
                raise ValueError(f"详情页 {unique_key} 尚未完整渲染字段值")
            fields[label] = field_value
        if not fields:
            raise ValueError(f"详情页 {unique_key} 没有通过约束的可合并字段")
        if baseline_item is not None and not any(
            baseline_item.get(label) != field_value for label, field_value in fields.items()
        ):
            raise ValueError(f"详情页 {unique_key} 尚未返回列表之外的详情字段")
        return fields

    @classmethod
    def _validated_progress_details(
        cls,
        progress: DetailProgress,
        unique_items: Mapping[str, dict[str, str]],
    ) -> dict[str, dict[str, str]]:
        validated: dict[str, dict[str, str]] = {}
        for key, fields in progress.details_by_key.items():
            baseline_item = unique_items.get(key)
            if baseline_item is None:
                continue
            try:
                validated[key] = cls._validated_detail_fields(
                    {"contains_expected": True, "details": fields},
                    key,
                    baseline_item=baseline_item,
                )
            except ValueError:
                continue
        return validated

    @staticmethod
    def _merge_detail_records(
        unique_items: Mapping[str, dict[str, str]],
        details_by_key: Mapping[str, Mapping[str, str]],
    ) -> tuple[str, ...]:
        existing_fields = {field_name for item in unique_items.values() for field_name in item}
        merged_fields: list[str] = []
        labels = dict.fromkeys(label for details in details_by_key.values() for label in details)
        for label in labels:
            contributions = [
                (item, details[label])
                for key, details in details_by_key.items()
                if label in details
                and (item := unique_items.get(key)) is not None
                and item.get(label) != details[label]
            ]
            if not contributions:
                continue
            base_name = f"详情_{label}"[:64] if label in existing_fields else label
            field_name = base_name
            suffix = 2
            while field_name in existing_fields or field_name in merged_fields:
                suffix_text = f"_{suffix}"
                field_name = f"{base_name[: 64 - len(suffix_text)]}{suffix_text}"
                suffix += 1
            for item, value in contributions:
                item[field_name] = value
            merged_fields.append(field_name)
        return tuple(merged_fields)

    async def _extract_page(self, spec: CollectionExtractionSpec) -> dict[str, Any]:
        pagination_control_selector = (
            spec.next_page_selector
            if spec.pagination_mode == "next"
            else spec.load_more_selector
            if spec.pagination_mode == "load_more"
            else None
        )
        payload = {
            "row_selector": spec.row_selector,
            "fields": [
                {"name": field.name, "selector": field.selector, "source": field.source}
                for field in spec.fields
            ],
            "pagination_control_selector": pagination_control_selector,
            "current_page_selector": spec.current_page_selector,
            "total_count_selector": spec.total_count_selector,
            "total_pages_selector": spec.total_pages_selector,
            "max_items": spec.max_items,
        }
        script = _EXTRACT_PAGE_TEMPLATE.replace(
            "__SPEC__",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        value = await self._evaluate(script, idempotent=True)
        if not isinstance(value, dict):
            raise RuntimeError("当前页结构化采集返回了无效数据")
        if value.get("error"):
            raise ValueError(f"当前页结构化采集失败：{value['error']}")
        return value

    async def _prepare_first_numbered_page(
        self,
        spec: CollectionExtractionSpec,
        page_data: dict[str, Any],
    ) -> dict[str, Any]:
        page = self._validate_page(page_data, 1)
        current_page = page["current_page"]
        if current_page is None:
            raise ValueError("页码分页无法读取当前页，已停止避免遗漏前序页面")
        if current_page == 1:
            return page_data
        click_result = await self._click_page_number(spec, 1)
        if not click_result.get("clicked"):
            raise RuntimeError(str(click_result.get("reason", "无法回到第 1 页")))
        first_page = await self._wait_for_page_change(spec, page["fingerprint"])
        if first_page is None:
            raise RuntimeError("点击第 1 页后列表指纹未变化，无法证明从首页开始采集")
        validated = self._validate_page(first_page, 1)
        if validated["current_page"] != 1:
            raise RuntimeError("页码控件动作后当前页不是第 1 页，已停止避免漏页")
        return first_page

    async def _advance_pagination(
        self,
        spec: CollectionExtractionSpec,
        page: Mapping[str, Any],
        *,
        page_number: int,
        declared_pages: int | None,
        previous_fingerprint: str,
        control_seen: bool,
    ) -> _PaginationAdvance:
        mode = spec.pagination_mode
        if mode == "none":
            return _PaginationAdvance(control_seen=control_seen)
        if mode == "infinite_scroll":
            return await self._advance_infinite_scroll(
                spec,
                previous_fingerprint,
                control_seen=control_seen,
            )
        if mode == "page_number":
            current_page = page.get("current_page")
            if current_page != page_number:
                return _PaginationAdvance(
                    failure_reason=(
                        f"代码预期第 {page_number} 页，但当前页控件显示 {current_page}"
                    ),
                    control_seen=control_seen,
                )
            if declared_pages is None:
                return _PaginationAdvance(
                    failure_reason="页码分页没有读取到声明总页数，无法证明覆盖全部页码",
                    control_seen=control_seen,
                )
            if page_number >= declared_pages:
                return _PaginationAdvance(
                    terminal_evidence="页码遍历已覆盖页面声明总页数",
                    control_seen=True,
                )
            click_result = await self._click_page_number(spec, page_number + 1)
            if not click_result.get("clicked"):
                return _PaginationAdvance(
                    failure_reason=str(click_result.get("reason", "目标页码点击失败")),
                    control_seen=True,
                )
            next_page = await self._wait_for_page_change(spec, previous_fingerprint)
            if next_page is None:
                return _PaginationAdvance(
                    failure_reason=(
                        f"点击第 {page_number + 1} 页后列表指纹未变化，已停止避免重复采集"
                    ),
                    control_seen=True,
                )
            return _PaginationAdvance(page_data=next_page, control_seen=True)

        control_exists = bool(page.get("pagination_exists"))
        control_disabled = bool(page.get("pagination_disabled"))
        control_seen = control_seen or control_exists
        label = "下一页" if mode == "next" else "加载更多"
        if not control_exists or control_disabled:
            evidence = None
            if (control_exists and control_disabled) or (not control_exists and control_seen):
                evidence = (
                    "已验证的下一页控件在终页禁用或消失"
                    if mode == "next"
                    else "已验证的加载更多控件在终点禁用或消失"
                )
            return _PaginationAdvance(
                terminal_evidence=evidence,
                control_seen=control_seen,
            )
        if declared_pages is not None and page_number >= declared_pages:
            return _PaginationAdvance(
                failure_reason=f"已达到页面声明页数，但{label}控件仍可继续",
                control_seen=control_seen,
            )
        click_result = (
            await self._click_next(spec.next_page_selector or "")
            if mode == "next"
            else await self._click_load_more(spec.load_more_selector or "")
        )
        if not click_result.get("clicked"):
            return _PaginationAdvance(
                failure_reason=str(click_result.get("reason", f"{label}点击失败")),
                control_seen=control_seen,
            )
        next_page = await self._wait_for_page_change(spec, previous_fingerprint)
        if next_page is None:
            return _PaginationAdvance(
                failure_reason=f"点击{label}后列表指纹未变化，已停止避免重复采集",
                control_seen=control_seen,
            )
        return _PaginationAdvance(page_data=next_page, control_seen=control_seen)

    async def _advance_infinite_scroll(
        self,
        spec: CollectionExtractionSpec,
        previous_fingerprint: str,
        *,
        control_seen: bool,
    ) -> _PaginationAdvance:
        stable_bottom_rounds = 0
        last_scroll_height: float | None = None
        # 一次窗口推进通常立刻改变虚拟行；静态长列表则可能需要多段滚动才能到底。
        # 上限只防止异常容器永久吞掉滚动事件，不把未到达底部误判为完成。
        for _ in range(100):
            scroll_result = await self._scroll_more(spec.scroll_container_selector)
            if not scroll_result.get("scrolled"):
                return _PaginationAdvance(
                    failure_reason=str(scroll_result.get("reason", "滚动到底动作失败")),
                    control_seen=control_seen,
                )
            next_page = await self._wait_for_page_change(
                spec,
                previous_fingerprint,
                timeout_seconds=min(2.0, spec.page_wait_timeout_seconds),
            )
            if next_page is not None:
                return _PaginationAdvance(page_data=next_page, control_seen=control_seen)
            before = float(scroll_result.get("before", 0) or 0)
            after = float(scroll_result.get("after", 0) or 0)
            scroll_height = float(scroll_result.get("scroll_height", 0) or 0)
            at_bottom = bool(scroll_result.get("at_bottom"))
            if not at_bottom:
                stable_bottom_rounds = 0
                if after <= before and scroll_height == last_scroll_height:
                    return _PaginationAdvance(
                        failure_reason="滚动容器尚未到底但位置和高度均无进展",
                        control_seen=control_seen,
                    )
                last_scroll_height = scroll_height
                continue
            stable_bottom_rounds = (
                stable_bottom_rounds + 1 if scroll_height == last_scroll_height else 1
            )
            last_scroll_height = scroll_height
            if stable_bottom_rounds >= spec.scroll_stable_rounds:
                return _PaginationAdvance(
                    terminal_evidence=(
                        f"分段滚动覆盖可见窗口，且连续 {spec.scroll_stable_rounds} 次"
                        "到达底部后列表指纹与滚动高度稳定"
                    ),
                    control_seen=control_seen,
                )
        return _PaginationAdvance(
            failure_reason="连续 100 次分段滚动仍未出现新数据或可验证终点",
            control_seen=control_seen,
        )

    async def _click_next(self, selector: str) -> dict[str, Any]:
        script = _CLICK_NEXT_TEMPLATE.replace(
            "__SELECTOR__",
            json.dumps(selector, ensure_ascii=False),
        )
        value = await self._evaluate(script, idempotent=False)
        if not isinstance(value, dict):
            raise RuntimeError("下一页动作返回了无效数据")
        return value

    async def _click_load_more(self, selector: str) -> dict[str, Any]:
        script = _CLICK_LOAD_MORE_TEMPLATE.replace(
            "__SELECTOR__",
            json.dumps(selector, ensure_ascii=False),
        )
        value = await self._evaluate(script, idempotent=False)
        if not isinstance(value, dict):
            raise RuntimeError("加载更多动作返回了无效数据")
        return value

    async def _click_page_number(
        self,
        spec: CollectionExtractionSpec,
        target_page: int,
    ) -> dict[str, Any]:
        script = _CLICK_PAGE_NUMBER_TEMPLATE.replace(
            "__SELECTOR__",
            json.dumps(spec.page_number_selector, ensure_ascii=False),
        ).replace("__TARGET_PAGE__", str(target_page))
        value = await self._evaluate(script, idempotent=False)
        if not isinstance(value, dict):
            raise RuntimeError("页码动作返回了无效数据")
        return value

    async def _scroll_more(self, selector: str | None) -> dict[str, Any]:
        script = _SCROLL_MORE_TEMPLATE.replace(
            "__SELECTOR__",
            json.dumps(selector, ensure_ascii=False),
        )
        value = await self._evaluate(script, idempotent=False)
        if not isinstance(value, dict):
            raise RuntimeError("无限滚动动作返回了无效数据")
        return value

    async def _wait_for_page_change(
        self,
        spec: CollectionExtractionSpec,
        previous_fingerprint: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        wait_seconds = (
            spec.page_wait_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            page = await self._extract_page(spec)
            if str(page.get("fingerprint", "")) != previous_fingerprint:
                return page
            await asyncio.sleep(min(0.1, wait_seconds / 2))
        return None

    async def _evaluate(self, script: str, *, idempotent: bool) -> Any:
        receipt = await self.driver.execute(
            ActionCommand(
                action_id=f"structured-{uuid.uuid4().hex}",
                kind=ActionKind.EVALUATE,
                script=script,
                idempotent=idempotent,
            )
        )
        if not receipt.success:
            if not receipt.outcome_known:
                raise RuntimeError("结构化采集内部动作结果未知，已停止避免重复翻页")
            raise RuntimeError(f"结构化采集内部动作失败：{receipt.message}")
        return receipt.data.get("value")

    @staticmethod
    def _failed_result(
        spec: CollectionExtractionSpec,
        reason: str,
        *,
        failed_page: int,
    ) -> CollectionExtractionResult:
        logger.warning(
            "结构化采集在分页准备阶段停止",
            extra={
                "collection": spec.collection_name,
                "pagination_mode": spec.pagination_mode,
                "failed_page": failed_page,
            },
        )
        return CollectionExtractionResult(
            collection_name=spec.collection_name,
            complete=False,
            unique_count=0,
            exported_count=0,
            duplicate_count=0,
            visited_pages=(),
            failed_pages=(failed_page,),
            declared_total=None,
            declared_pages=None,
            completion_evidence=(),
            failure_reasons=(reason,),
            pagination_mode=spec.pagination_mode,
            detail_requested=spec.detail_trigger_selector is not None,
        )

    @staticmethod
    def _validate_page(value: Mapping[str, Any], page_number: int) -> dict[str, Any]:
        rows = value.get("rows")
        fingerprint = value.get("fingerprint")
        if not isinstance(rows, list) or not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError(f"第 {page_number} 页缺少有效记录数组或页面指纹")
        normalized_rows: list[dict[str, str]] = []
        for item in rows:
            if not isinstance(item, Mapping):
                raise ValueError(f"第 {page_number} 页包含非对象记录")
            normalized_rows.append({str(key): str(nested) for key, nested in item.items()})
        for key in ("declared_total", "declared_pages", "current_page"):
            item = value.get(key)
            if item is not None and (
                isinstance(item, bool) or not isinstance(item, int) or item < 0
            ):
                raise ValueError(f"第 {page_number} 页的 {key} 不是有效非负整数")
        current_page = value.get("current_page")
        if current_page == 0:
            raise ValueError(f"第 {page_number} 页的 current_page 必须大于 0")
        pagination_exists = (
            value.get("pagination_exists")
            if "pagination_exists" in value
            else value.get("next_exists")
        )
        pagination_disabled = (
            value.get("pagination_disabled")
            if "pagination_disabled" in value
            else value.get("next_disabled")
        )
        return {
            "rows": normalized_rows,
            "fingerprint": fingerprint,
            "declared_total": value.get("declared_total"),
            "declared_pages": value.get("declared_pages"),
            "current_page": current_page,
            "pagination_exists": bool(pagination_exists),
            "pagination_disabled": bool(pagination_disabled),
        }

    @staticmethod
    def _merge_declared_number(
        current: int | None,
        observed: int | None,
        label: str,
        failure_reasons: list[str],
    ) -> int | None:
        if observed is None:
            return current
        if current is not None and current != observed:
            failure_reasons.append(f"{label}在分页过程中从 {current} 变化为 {observed}")
        return observed if current is None else current

    def _export(
        self,
        spec: CollectionExtractionSpec,
        items: list[dict[str, str]],
        unique_count: int,
        duplicate_count: int,
        visited_pages: tuple[int, ...],
        declared_total: int | None,
        declared_pages: int | None,
        completion_evidence: tuple[str, ...],
    ) -> tuple[Path, Path]:
        field_names = [field.name for field in spec.fields]
        for item in items:
            for field_name in item:
                if field_name not in field_names:
                    field_names.append(field_name)
        json_bytes = json.dumps(
            {
                "collection": spec.collection_name,
                "unique_count": unique_count,
                "exported_count": len(items),
                "duplicate_count": duplicate_count,
                "fields": field_names,
                "completeness": {
                    "source": "dom",
                    "complete": True,
                    "pagination_mode": spec.pagination_mode,
                    "visited_pages": list(visited_pages),
                    "failed_pages": [],
                    "declared_total": declared_total,
                    "declared_pages": declared_pages,
                    "completion_evidence": list(completion_evidence),
                    **(
                        {"detail_requested": True, "detail_count": unique_count}
                        if spec.detail_trigger_selector
                        else {}
                    ),
                },
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        csv_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(csv_buffer, fieldnames=field_names, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)
        csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")

        safe_name = (
            "".join(
                character
                for character in spec.collection_name
                if character.isalnum() or character in "-_"
            )
            or "collection"
        )
        output_root = self.artifact_root / "structured-data"
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_root, 0o700)
        stem = f"{safe_name}-{time.time_ns()}"
        json_path = output_root / f"{stem}.json"
        csv_path = output_root / f"{stem}.csv"
        temporary_paths: list[Path] = []
        try:
            for destination, content in ((json_path, json_bytes), (csv_path, csv_bytes)):
                temporary = output_root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                temporary_paths.append(temporary)
            os.replace(temporary_paths[0], json_path)
            os.replace(temporary_paths[1], csv_path)
            os.chmod(json_path, 0o600)
            os.chmod(csv_path, 0o600)
        except Exception:
            json_path.unlink(missing_ok=True)
            csv_path.unlink(missing_ok=True)
            raise
        finally:
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)
        return json_path, csv_path
