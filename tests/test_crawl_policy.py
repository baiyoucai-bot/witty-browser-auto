"""robots.txt 判定、按主机限速与导航闸门的回归。

三条最容易做错的规则单独立测：通配符要真的当通配符、命中多条时按路径长度取最具体、
同长时 Allow 胜出。另外两条取值语义必须分清：4xx 是放行，5xx 与取不到是"未知"而不是放行。
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import pytest

from witty_browser_auto.agent.crawl_tools import (
    CrawlPolicyStore,
    execute_check_crawl_policy,
    origin_of,
)
from witty_browser_auto.agent.tools import ToolExecutor
from witty_browser_auto.browser.pacing import MAX_INTERVAL_MS, HostPacer
from witty_browser_auto.domain.models import (
    ActionCommand,
    ActionReceipt,
    DriverCapabilities,
    ExecutionScope,
    ExpectedCondition,
    ModelToolCall,
    Observation,
    TaskSpec,
    VerificationResult,
)
from witty_browser_auto.network.robots import parse_robots, robots_url_for

ORIGIN = "https://shop.test"

_ROBOTS = """
# 通用分组
User-agent: *
Disallow: /private/
Disallow: /*?
Allow: /private/public.html
Crawl-delay: 2
Sitemap: https://shop.test/sitemap.xml

User-agent: WittyBrowserAuto
Disallow: /
Allow: /api/
Disallow: /api/*.pdf$
"""


# ----------------------------------------------------------------------
# 解析与判定
# ----------------------------------------------------------------------


def test_wildcards_and_end_anchor_are_honoured() -> None:
    policy = parse_robots(_ROBOTS, origin=ORIGIN, agent="WittyBrowserAuto")
    assert policy.matched_agent == "wittybrowserauto"

    assert policy.decide("/")[0] is False
    assert policy.decide("/api/orders")[0] is True
    # `$` 锚定结尾：带查询串就不再命中该规则，与真实爬虫一致。
    assert policy.decide("/api/doc.pdf")[0] is False
    assert policy.decide("/api/doc.pdf?v=1")[0] is True


def test_longest_rule_wins_and_allow_breaks_ties() -> None:
    policy = parse_robots(_ROBOTS, origin=ORIGIN, agent="OtherBot")
    assert policy.matched_agent == "*"

    # /private/public.html 比 /private/ 更长，因此放行。
    assert policy.decide("/private/x")[0] is False
    allowed, rule = policy.decide("/private/public.html")
    assert allowed is True and rule is not None and rule.pattern == "/private/public.html"
    # 查询串被 /*? 拦住。
    assert policy.decide("/list?page=2")[0] is False
    assert policy.decide("/list")[0] is True

    tie = parse_robots("User-agent: *\nDisallow: /x\nAllow: /x\n", origin=ORIGIN, agent="OtherBot")
    assert tie.decide("/x")[0] is True


def test_agent_specificity_beats_the_wildcard_group() -> None:
    policy = parse_robots(_ROBOTS, origin=ORIGIN, agent="WittyBrowserAuto/2.0")
    # 专用分组胜出，因此根路径被禁而不是只禁 /private/。
    assert policy.decide("/")[0] is False
    assert policy.crawl_delay_seconds is None


def test_crawl_delay_and_sitemaps_are_extracted() -> None:
    policy = parse_robots(_ROBOTS, origin=ORIGIN, agent="OtherBot")
    assert policy.crawl_delay_seconds == 2.0
    assert policy.sitemaps == ("https://shop.test/sitemap.xml",)


def test_empty_disallow_means_allow_all() -> None:
    policy = parse_robots("User-agent: *\nDisallow:\n", origin=ORIGIN, agent="Bot")
    assert policy.allow_all is True
    assert policy.decide("/anything")[0] is True


def test_comments_and_unknown_fields_are_ignored() -> None:
    policy = parse_robots(
        "User-agent: *  # 注释\nDisallow: /a  # 也是注释\nUnknown-field: 1\n",
        origin=ORIGIN,
        agent="Bot",
    )
    assert policy.decide("/a")[0] is False
    assert policy.decide("/b")[0] is True


def test_robots_url_is_derived_from_any_page_url() -> None:
    assert robots_url_for("https://shop.test/deep/path?q=1") == "https://shop.test/robots.txt"
    with pytest.raises(ValueError, match="http/https"):
        robots_url_for("file:///etc/passwd")
    with pytest.raises(ValueError, match="http/https"):
        origin_of("about:blank")


# ----------------------------------------------------------------------
# 取不到与 5xx
# ----------------------------------------------------------------------


class _RobotsDriver:
    capabilities = DriverCapabilities(dom=True, accessibility=True, javascript=True)

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0
        self.commands: list[ActionCommand] = []

    async def fetch_robots_txt(self, origin: str) -> dict[str, Any]:
        self.calls += 1
        return self.response

    async def observe(self, *, force: bool = False) -> Observation:
        return Observation(
            surface_id="s",
            url=f"{ORIGIN}/start",
            title="起点",
            version=1,
            fingerprint="fp",
            summary="起点",
            candidates=(),
        )

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        return ActionReceipt(command.action_id, True, True, "ok", 1.0)

    async def verify(self, condition: ExpectedCondition) -> VerificationResult:
        return VerificationResult(True, "ok")


def _store(pacer: HostPacer | None = None) -> CrawlPolicyStore:
    return CrawlPolicyStore(agent="WittyBrowserAuto", pacer=pacer)


def test_missing_robots_is_allow_all() -> None:
    driver = _RobotsDriver({"ok": True, "status": 404, "text": ""})
    policy = asyncio.run(_store().load(driver, ORIGIN))
    assert policy.available is True
    assert policy.allow_all is True
    assert policy.decide("/anything")[0] is True


def test_server_error_is_unknown_not_allowed() -> None:
    driver = _RobotsDriver({"ok": True, "status": 503, "text": ""})
    policy = asyncio.run(_store().load(driver, ORIGIN))
    assert policy.available is False
    # 未知不等于放行：判定返回 None，由调用方决定怎么处置。
    assert policy.decide("/anything")[0] is None


def test_network_failure_is_unknown() -> None:
    driver = _RobotsDriver({"ok": False, "error": "Failed to fetch"})
    policy = asyncio.run(_store().load(driver, ORIGIN))
    assert policy.available is False
    assert "Failed to fetch" in policy.reason


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _check(driver: Any, store: CrawlPolicyStore, arguments: dict[str, Any] | None = None) -> Any:
    return asyncio.run(
        execute_check_crawl_policy(
            arguments or {},
            driver=driver,
            store=store,
            current_url=f"{ORIGIN}/start",
        )
    )


def test_tool_reports_verdict_and_caches_per_origin() -> None:
    driver = _RobotsDriver({"ok": True, "status": 200, "text": _ROBOTS})
    store = _store(HostPacer())

    first = _check(driver, store, {"url": f"{ORIGIN}/api/orders"})
    assert first.success is True
    assert first.data["allowed"] is True
    assert first.data["cached"] is False
    # 站点声明的 Crawl-delay 落到节奏阀门上。
    assert first.data["pacing_interval_ms"] == 0.0  # WittyBrowserAuto 分组没有声明 delay

    second = _check(driver, store, {"url": f"{ORIGIN}/api/doc.pdf"})
    assert second.data["allowed"] is False
    assert second.data["matched_rule"]["pattern"] == "/api/*.pdf$"
    assert second.data["cached"] is True
    # 同一 origin 只读一次 robots.txt。
    assert driver.calls == 1

    refreshed = _check(driver, store, {"url": f"{ORIGIN}/api/orders", "refresh": True})
    assert refreshed.data["cached"] is False
    assert driver.calls == 2


def test_wildcard_group_delay_reaches_the_pacer() -> None:
    driver = _RobotsDriver({"ok": True, "status": 200, "text": _ROBOTS})
    pacer = HostPacer()
    store = _store(pacer)
    result = _check(driver, store, {"url": f"{ORIGIN}/list", "agent": "OtherBot"})

    assert result.data["policy"]["crawl_delay_seconds"] == 2.0
    # 2 秒 Crawl-delay 变成 2000 毫秒的主机间隔。
    assert pacer.interval_for("shop.test") == 2000.0
    assert result.data["pacing_interval_ms"] == 2000.0


def test_tool_rejects_bad_arguments() -> None:
    driver = _RobotsDriver({"ok": True, "status": 200, "text": _ROBOTS})
    store = _store()
    for arguments, expected in (
        ({"bogus": 1}, "未知参数"),
        ({"agent": " "}, "agent 必须"),
        ({"refresh": "yes"}, "refresh 必须"),
        ({"url": "about:blank"}, "http/https"),
    ):
        with pytest.raises(ValueError, match=expected):
            _check(driver, store, arguments)


# ----------------------------------------------------------------------
# 限速
# ----------------------------------------------------------------------


def test_pacer_waits_between_same_host_requests() -> None:
    async def scenario() -> None:
        pacer = HostPacer(default_interval_ms=60)
        assert await pacer.acquire("https://a.test/1") == 0.0
        started = monotonic()
        waited = await pacer.acquire("https://a.test/2")
        elapsed = (monotonic() - started) * 1000
        assert waited > 0
        assert elapsed >= 40  # 允许调度抖动
        # 不同主机互不影响。
        assert await pacer.acquire("https://b.test/1") == 0.0

    asyncio.run(scenario())


def test_pacer_takes_the_larger_of_declared_and_configured() -> None:
    pacer = HostPacer(default_interval_ms=100)
    pacer.configure("a.test", 500)
    assert pacer.interval_for("a.test") == 500
    pacer.configure("b.test", 10)
    # 站点声明更小时不能把调用方设定的礼貌下限调低。
    assert pacer.interval_for("b.test") == 100
    assert pacer.snapshot() == {"a.test": 500.0, "b.test": 100.0}


def test_pacer_rejects_out_of_range_intervals() -> None:
    with pytest.raises(ValueError, match="请求间隔"):
        HostPacer(-1)
    with pytest.raises(ValueError, match="请求间隔"):
        HostPacer(MAX_INTERVAL_MS + 1)


# ----------------------------------------------------------------------
# 导航闸门
# ----------------------------------------------------------------------


def _executor(driver: Any, **kwargs: Any) -> ToolExecutor:
    return ToolExecutor(
        driver,
        TaskSpec("crawl", "抓取", f"{ORIGIN}/start", ExecutionScope("project")),
        **kwargs,
    )


def _navigate(executor: ToolExecutor, driver: Any, url: str) -> Any:
    async def scenario() -> Any:
        observation = await driver.observe()
        return await executor.execute(ModelToolCall("c1", "navigate", {"url": url}), observation)

    return asyncio.run(scenario())


def test_navigation_is_open_by_default_even_when_robots_disallows() -> None:
    driver = _RobotsDriver({"ok": True, "status": 200, "text": _ROBOTS})
    result = _navigate(_executor(driver), driver, f"{ORIGIN}/private/x")

    # 默认不遵守：robots.txt 约束的是抓取，交互场景不该被它挡住。
    assert result.success is True
    assert driver.calls == 0
    assert driver.commands and driver.commands[0].url == f"{ORIGIN}/private/x"


def test_respect_robots_blocks_disallowed_navigation() -> None:
    driver = _RobotsDriver({"ok": True, "status": 200, "text": _ROBOTS})
    executor = _executor(driver, respect_robots=True)
    result = _navigate(executor, driver, f"{ORIGIN}/api/doc.pdf")

    assert result.success is False
    assert "robots.txt 禁止抓取" in result.message
    assert result.data["blocked_by"] == "robots.txt"
    # 被拦下时不能真的发出导航。
    assert driver.commands == []
    # 首次访问该站点自动读一次 robots.txt，调用方不必记着先查。
    assert driver.calls == 1

    allowed = _navigate(executor, driver, f"{ORIGIN}/api/orders")
    assert allowed.success is True
    assert driver.calls == 1  # 复用缓存


def test_respect_robots_refuses_when_status_is_unknown() -> None:
    driver = _RobotsDriver({"ok": True, "status": 500, "text": ""})
    result = _navigate(_executor(driver, respect_robots=True), driver, f"{ORIGIN}/x")

    assert result.success is False
    assert "状态未知" in result.message
    assert driver.commands == []
