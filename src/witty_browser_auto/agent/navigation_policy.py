"""智能体导航范围校验。"""

from urllib.parse import urlsplit

from witty_browser_auto.domain.errors import PolicyViolationError
from witty_browser_auto.domain.models import (
    CandidateTarget,
    ExpectedCondition,
    Observation,
    TaskSpec,
)
from witty_browser_auto.memory.url import normalize_url

_READ_ONLY_CLICK_TERMS = (
    "查询",
    "搜索",
    "检索",
    "筛选",
    "刷新",
    "加载更多",
    "下一页",
    "上一页",
    "search",
    "filter",
    "refresh",
)
_FOCUS_ONLY_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})

CLICK_VERIFICATION_TIMEOUT_SECONDS = 4.0


def assert_navigation_allowed(task: TaskSpec, url: str) -> None:
    """校验导航地址属于当前任务显式授权的来源。"""

    parts = urlsplit(url)
    if parts.username or parts.password:
        raise PolicyViolationError("导航地址不得包含明文账号或密码")
    target_origin = normalize_url(url).origin
    allowed = task.scope.allowed_origins or (normalize_url(task.start_url).origin,)
    normalized_allowed = {normalize_url(f"{origin.rstrip('/')}/").origin for origin in allowed}
    if target_origin not in normalized_allowed:
        raise PolicyViolationError(
            "导航目标不在任务授权域名范围内",
            context={"target_origin": target_origin},
        )


def condition_visible_before_action(
    condition: ExpectedCondition,
    observation: Observation,
) -> bool:
    if condition.kind == "url_contains":
        return condition.value in observation.url
    if condition.kind == "title_contains":
        return condition.value in observation.title
    if condition.kind == "text_contains":
        return condition.value in observation.summary
    return condition.kind == "target_exists" and any(
        candidate.target_id == condition.value for candidate in observation.candidates
    )


def read_only_click_fallback_condition(
    candidate: CandidateTarget,
    observation: Observation,
) -> ExpectedCondition | None:
    """Use page change when a read-only click's declared result already exists."""

    if not is_read_only_click(candidate):
        return None
    return ExpectedCondition(
        "fingerprint_changed",
        observation.fingerprint,
        timeout_seconds=CLICK_VERIFICATION_TIMEOUT_SECONDS,
    )


def is_read_only_click(candidate: CandidateTarget) -> bool:
    """识别只聚焦控件或刷新查询结果、不提交业务写操作的点击。"""

    if candidate.role.casefold() in _FOCUS_ONLY_ROLES:
        return True
    label = f"{candidate.name} {candidate.text}".casefold()
    return any(term in label for term in _READ_ONLY_CLICK_TERMS)
