"""抓取策略执行层：读取 robots.txt、给出判定、按站点声明设定节奏。

工具本身只回答"这个地址按 robots.txt 能不能抓、站点要求多大间隔"，**不替调用方做决定**：
robots.txt 约束的是自动化抓取，而本库同样被用于登录自家系统这类交互场景，很多站点的
robots.txt 对全站 Disallow，默认拦下会把正当用途一起挡掉。真正的强制发生在装配时显式
打开 `respect_robots` 之后，那时才把它当硬闸门。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from witty_browser_auto.browser.pacing import HostPacer
from witty_browser_auto.network.robots import (
    RobotsPolicy,
    missing_policy,
    parse_robots,
    unknown_policy,
)

CRAWL_TOOL_NAMES = frozenset({"check_crawl_policy"})
DEFAULT_CRAWL_AGENT = "WittyBrowserAuto"

__all__ = [
    "CRAWL_TOOL_NAMES",
    "DEFAULT_CRAWL_AGENT",
    "CrawlPolicyStore",
    "CrawlToolOutcome",
    "execute_check_crawl_policy",
]


@dataclass(frozen=True, slots=True)
class CrawlToolOutcome:
    success: bool
    message: str
    data: dict[str, Any]
    model_data: dict[str, Any] | None = None


@dataclass(slots=True)
class CrawlPolicyStore:
    """按 origin 缓存 robots.txt 判定，并把站点声明的 Crawl-delay 落到节奏阀门上。"""

    agent: str = DEFAULT_CRAWL_AGENT
    pacer: HostPacer | None = None
    policies: dict[str, RobotsPolicy] = field(default_factory=dict)

    def get(self, origin: str) -> RobotsPolicy | None:
        return self.policies.get(origin)

    def remember(self, policy: RobotsPolicy) -> None:
        self.policies[policy.origin] = policy
        if self.pacer is not None and policy.crawl_delay_seconds:
            self.pacer.configure(
                urlsplit(policy.origin).netloc,
                policy.crawl_delay_seconds * 1000,
            )

    async def load(self, driver: Any, origin: str, *, agent: str | None = None) -> RobotsPolicy:
        """取一次 robots.txt 并缓存；网络失败与 5xx 都算状态未知而不是放行。"""

        effective_agent = agent or self.agent
        result = await driver.fetch_robots_txt(origin)
        if not isinstance(result, Mapping) or not result.get("ok"):
            reason = str(result.get("error", "robots.txt 读取失败")) if result else "读取失败"
            policy = unknown_policy(
                origin=origin, agent=effective_agent, status=None, reason=reason
            )
        else:
            status = result.get("status")
            status_code = int(status) if isinstance(status, (int, float)) else None
            text = str(result.get("text", ""))
            if status_code is not None and 200 <= status_code < 300:
                policy = parse_robots(
                    text, origin=origin, agent=effective_agent, status=status_code
                )
            elif status_code is not None and 400 <= status_code < 500:
                policy = missing_policy(origin=origin, agent=effective_agent, status=status_code)
            else:
                policy = unknown_policy(
                    origin=origin,
                    agent=effective_agent,
                    status=status_code,
                    reason=f"robots.txt 返回 {status_code}，状态未知，不视为放行",
                )
        self.remember(policy)
        return policy


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("抓取策略只能针对 http/https 地址查询")
    return f"{parts.scheme}://{parts.netloc}"


async def execute_check_crawl_policy(
    arguments: Mapping[str, Any],
    *,
    driver: Any,
    store: CrawlPolicyStore,
    current_url: str,
) -> CrawlToolOutcome:
    """读取 robots.txt 并判定目标地址是否允许抓取。"""

    unknown = sorted(set(arguments) - {"url", "agent", "refresh"})
    if unknown:
        raise ValueError(f"check_crawl_policy 包含未知参数：{'、'.join(unknown)}")
    target = arguments.get("url") or current_url
    if not isinstance(target, str) or not target.strip():
        raise ValueError("必须提供 url，或先打开一个页面")
    agent = arguments.get("agent")
    if agent is not None and (not isinstance(agent, str) or not agent.strip()):
        raise ValueError("agent 必须是非空文本")
    refresh = arguments.get("refresh", False)
    if not isinstance(refresh, bool):
        raise ValueError("refresh 必须是布尔值")

    origin = origin_of(target.strip())
    policy = None if refresh else store.get(origin)
    cached = policy is not None
    if policy is None or (agent and policy.agent != agent):
        policy = await store.load(driver, origin, agent=agent)
        cached = False

    allowed, rule = policy.decide(target)
    interval = store.pacer.interval_for(urlsplit(origin).netloc) if store.pacer else 0.0
    payload = {
        "url": target,
        "allowed": allowed,
        "matched_rule": rule.public_dict() if rule is not None else None,
        "cached": cached,
        "pacing_interval_ms": interval,
        "policy": policy.public_dict(),
    }
    if allowed is True:
        verdict = "允许抓取"
    elif allowed is False:
        verdict = f"robots.txt 禁止抓取，命中规则 {rule.pattern if rule else ''}"
    else:
        verdict = f"robots.txt 状态未知：{policy.reason}"
    delay_note = (
        f"；站点声明 Crawl-delay {policy.crawl_delay_seconds} 秒"
        if policy.crawl_delay_seconds
        else ""
    )
    return CrawlToolOutcome(
        # 判定本身成功完成即算成功；"不允许抓取"是结论而不是工具故障。
        success=True,
        message=f"{verdict}{delay_note}",
        data=payload,
        model_data=payload,
    )
