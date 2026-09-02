"""robots.txt 解析与路径判定。

自己实现匹配而不用 `urllib.robotparser`：标准库的 `RuleLine.applies_to` 只做前缀比较，
`Disallow: /*?` 和 `Disallow: /*.pdf$` 这类带通配符的规则会被当成字面前缀，判定结果与
真实爬虫不一致。这里按 Google 的 robots.txt 规范实现三条容易做错的规则：

- **通配符**：`*` 匹配任意序列，`$` 锚定结尾。
- **最长优先**：同一分组内命中多条规则时，按规则路径长度取最具体的那条。
- **同长 Allow 胜出**：长度相同时 `Allow` 优先于 `Disallow`，否则站点无法用 Allow 开洞。

另外遵守两条取值语义：`Disallow:` 空值表示放行全部；robots.txt 404 表示全站放行，而
5xx 或取不到表示状态未知——未知不等于放行，是否因此停下由调用方按自己的合规要求决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_ROBOTS_BYTES = 512 * 1024
MAX_SITEMAPS = 50
_MAX_RULES = 2000

__all__ = [
    "MAX_ROBOTS_BYTES",
    "MAX_SITEMAPS",
    "ROBOTS_FETCH_SCRIPT",
    "RobotRule",
    "RobotsPolicy",
    "parse_robots",
    "robots_url_for",
]


def robots_url_for(url: str) -> str:
    """由任意地址推出它所属站点的 robots.txt 地址。"""

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("robots.txt 只能针对 http/https 地址查询")
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def _normalize_path(value: str) -> str:
    parts = urlsplit(value)
    path = parts.path if parts.scheme or parts.netloc else value
    if not path.startswith("/"):
        path = "/" + path
    if parts.query:
        path = f"{path}?{parts.query}"
    return unquote(path)


@dataclass(frozen=True, slots=True)
class RobotRule:
    allow: bool
    pattern: str
    _regex: re.Pattern[str] = field(repr=False, compare=False)

    @classmethod
    def build(cls, *, allow: bool, pattern: str) -> RobotRule:
        return cls(allow=allow, pattern=pattern, _regex=_compile_pattern(pattern))

    @property
    def specificity(self) -> int:
        """规范按规则路径长度衡量具体程度。"""

        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return self._regex.match(path) is not None

    def public_dict(self) -> dict[str, Any]:
        return {"directive": "allow" if self.allow else "disallow", "pattern": self.pattern}


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    compiled = "".join(".*" if character == "*" else re.escape(character) for character in body)
    return re.compile(f"^{compiled}$" if anchored else f"^{compiled}")


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """某个站点对某个 User-agent 的抓取策略。"""

    origin: str
    agent: str
    available: bool
    status: int | None = None
    rules: tuple[RobotRule, ...] = ()
    crawl_delay_seconds: float | None = None
    sitemaps: tuple[str, ...] = ()
    matched_agent: str = ""
    reason: str = ""

    @property
    def allow_all(self) -> bool:
        return self.available and not self.rules

    def decide(self, url_or_path: str) -> tuple[bool | None, RobotRule | None]:
        """判定是否可抓；`None` 表示 robots.txt 状态未知，由调用方决定如何处置。"""

        if not self.available:
            return None, None
        path = _normalize_path(url_or_path)
        best: RobotRule | None = None
        for rule in self.rules:
            if not rule.matches(path):
                continue
            if best is None or rule.specificity > best.specificity:
                best = rule
            elif rule.specificity == best.specificity and rule.allow and not best.allow:
                # 同样长度时 Allow 胜出，否则站点无法用 Allow 给 Disallow 开洞。
                best = rule
        if best is None:
            return True, None
        return best.allow, best

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "origin": self.origin,
            "agent": self.agent,
            "available": self.available,
            "status": self.status,
            "matched_agent": self.matched_agent,
            "allow_all": self.allow_all,
            "rule_count": len(self.rules),
            "rules": [rule.public_dict() for rule in self.rules],
            "crawl_delay_seconds": self.crawl_delay_seconds,
            "sitemaps": list(self.sitemaps),
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def parse_robots(
    text: str,
    *,
    origin: str,
    agent: str,
    status: int | None = 200,
) -> RobotsPolicy:
    """解析 robots.txt 正文，挑出与 `agent` 最匹配的分组。

    User-agent 取最长匹配：站点同时写 `*` 与 `MyBot` 时，`MyBot` 的分组胜出。
    """

    groups: list[tuple[list[str], list[RobotRule], float | None]] = []
    sitemaps: list[str] = []
    current_agents: list[str] = []
    current_rules: list[RobotRule] = []
    current_delay: float | None = None
    expecting_agents = False

    def flush() -> None:
        nonlocal current_agents, current_rules, current_delay, expecting_agents
        if current_agents:
            groups.append((current_agents, current_rules, current_delay))
        current_agents = []
        current_rules = []
        current_delay = None
        expecting_agents = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        key = field_name.strip().casefold()
        value = value.strip()
        if key == "user-agent":
            if not expecting_agents and (current_rules or current_delay is not None):
                flush()
            if value:
                current_agents.append(value.casefold())
                expecting_agents = True
            continue
        expecting_agents = False
        if key == "sitemap":
            if value and len(sitemaps) < MAX_SITEMAPS:
                sitemaps.append(value)
            continue
        if key in {"disallow", "allow"} and current_agents:
            if len(current_rules) >= _MAX_RULES:
                continue
            if key == "disallow" and not value:
                # 空 Disallow 等于不限制，规范上是"放行全部"的写法。
                continue
            if value:
                current_rules.append(RobotRule.build(allow=key == "allow", pattern=value))
            continue
        if key == "crawl-delay" and current_agents:
            try:
                delay = float(value)
            except ValueError:
                continue
            if 0 <= delay <= 3600:
                current_delay = delay
    flush()

    wanted = agent.casefold()
    best_group: tuple[list[str], list[RobotRule], float | None] | None = None
    best_score = -1
    for agents, rules, delay in groups:
        for candidate in agents:
            score = -1
            if candidate == "*":
                score = 0
            elif candidate in wanted or wanted in candidate:
                score = len(candidate)
            if score > best_score:
                best_score = score
                best_group = (agents, rules, delay)
    if best_group is None:
        return RobotsPolicy(
            origin=origin,
            agent=agent,
            available=True,
            status=status,
            sitemaps=tuple(sitemaps),
            reason="robots.txt 没有适用的分组，视为全站放行",
        )
    agents, rules, delay = best_group
    matched = next(
        (item for item in agents if item != "*" and (item in wanted or wanted in item)),
        "*",
    )
    return RobotsPolicy(
        origin=origin,
        agent=agent,
        available=True,
        status=status,
        rules=tuple(rules),
        crawl_delay_seconds=delay,
        sitemaps=tuple(sitemaps),
        matched_agent=matched,
    )


def missing_policy(*, origin: str, agent: str, status: int | None) -> RobotsPolicy:
    """robots.txt 返回 4xx：规范视为全站放行。"""

    return RobotsPolicy(
        origin=origin,
        agent=agent,
        available=True,
        status=status,
        reason=f"robots.txt 返回 {status}，按规范视为全站放行",
    )


def unknown_policy(*, origin: str, agent: str, status: int | None, reason: str) -> RobotsPolicy:
    """robots.txt 取不到或 5xx：状态未知，不等于放行。"""

    return RobotsPolicy(
        origin=origin,
        agent=agent,
        available=False,
        status=status,
        reason=reason,
    )


# 固定模板：只取给定地址的文本。地址由驱动按 origin 自行拼成 /robots.txt，
# 调用方无法把它指向任意 URL；robots.txt 是公开策略文件，因此不带凭据。
ROBOTS_FETCH_SCRIPT = r"""
async (spec) => {
  try {
    const response = await fetch(spec.url, {
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'follow',
    });
    const text = await response.text();
    return JSON.stringify({
      ok: true,
      status: response.status,
      text: text.slice(0, spec.maxBytes),
      truncated: text.length > spec.maxBytes,
    });
  } catch (error) {
    return JSON.stringify({
      ok: false,
      error: String((error && error.message) || error),
    });
  }
}
"""
