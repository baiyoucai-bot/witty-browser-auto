"""浏览器观察使用的固定页面脚本。"""

WAIT_FOR_ACTIONABLE_DOM_SCRIPT = """
new Promise((resolve) => {
  const selector = 'a[href],button,input:not([type="hidden"]),select,textarea,summary,[role]';
  const ready = () => Boolean(document.querySelector(selector));
  if (ready()) {
    resolve(true);
    return;
  }
  const root = document.documentElement;
  if (!root) {
    resolve(false);
    return;
  }
  let timeoutId = 0;
  const observer = new MutationObserver(() => {
    if (ready()) {
      clearTimeout(timeoutId);
      observer.disconnect();
      resolve(true);
    }
  });
  observer.observe(root, {childList: true, subtree: true});
  timeoutId = setTimeout(() => {
    observer.disconnect();
    resolve(false);
  }, 1500);
})
"""

PAGE_STATE_SCRIPT = r"""
(() => {
  const hashText = (value) => {
    let result = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      result ^= value.charCodeAt(index);
      result = Math.imul(result, 16777619);
    }
    return (result >>> 0).toString(16).padStart(8, '0');
  };
  const hashPixels = (image) => {
    try {
      const canvas = document.createElement('canvas');
      canvas.width = 24;
      canvas.height = 24;
      const context = canvas.getContext('2d', {willReadFrequently: true});
      if (!context) return '';
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let result = 2166136261;
      for (let index = 0; index < pixels.length; index += 1) {
        result ^= pixels[index];
        result = Math.imul(result, 16777619);
      }
      return (result >>> 0).toString(16).padStart(8, '0');
    } catch (_error) {
      return '';
    }
  };
  const visibleImages = Array.from(document.images)
    .filter((image) => {
      const rect = image.getBoundingClientRect();
      const style = getComputedStyle(image);
      return rect.width > 0 && rect.height > 0
        && style.display !== 'none' && style.visibility !== 'hidden';
    })
    .slice(0, 50)
    .map((image) => [
      hashText(image.currentSrc || image.src || ''),
      hashPixels(image),
      image.naturalWidth || 0,
      image.naturalHeight || 0,
    ]);
  return {
    url: location.href,
    title: document.title,
    text: (document.body?.innerText || '').slice(0, 6000),
    viewport: {width: innerWidth, height: innerHeight},
    visualResources: visibleImages,
  };
})()
"""

POINTER_TARGETS_SCRIPT = r"""
(() => {
  // __wittyPointerTargets：补充无原生角色、但浏览器明确显示可点击的前端组件。
  const nativeSelector = 'a[href],button,input,select,textarea,summary,[role]';
  const isVisible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width >= 1 && rect.height >= 1;
  };
  const selectorFor = (element) => {
    if (element.id) {
      const idSelector = `#${CSS.escape(element.id)}`;
      if (document.querySelectorAll(idSelector).length === 1) return idSelector;
    }
    const parts = [];
    let current = element;
    while (current && current !== document.body) {
      const tag = current.tagName.toLowerCase();
      const siblings = current.parentElement
        ? Array.from(current.parentElement.children).filter(
            item => item.tagName === current.tagName
          )
        : [];
      const index = siblings.indexOf(current) + 1;
      parts.unshift(`${tag}:nth-of-type(${index})`);
      current = current.parentElement;
    }
    parts.unshift('body');
    return parts.join(' > ');
  };
  const stableAttributeNames = [
    'data-testid', 'id', 'name', 'aria-label', 'placeholder', 'title', 'type'
  ];
  return Array.from(document.querySelectorAll('body *'))
    .filter((element) => {
      if (element.closest(nativeSelector)) return false;
      if (!isVisible(element) || getComputedStyle(element).cursor !== 'pointer') return false;
      const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text || text.length > 200) return false;
      return !Array.from(element.children).some(
        child => isVisible(child) && getComputedStyle(child).cursor === 'pointer'
      );
    })
    .slice(0, 100)
    .map((element) => {
      const rect = element.getBoundingClientRect();
      const attrs = {};
      for (const name of stableAttributeNames) {
        const value = element.getAttribute(name);
        if (value) attrs[name] = value;
      }
      const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
      return {
        selector: selectorFor(element),
        tag: element.tagName.toLowerCase(),
        role: 'button',
        name: text,
        text,
        attrs,
        disabled: element.hasAttribute('disabled') ||
          element.getAttribute('aria-disabled') === 'true',
        box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}
      };
    });
})()
"""
