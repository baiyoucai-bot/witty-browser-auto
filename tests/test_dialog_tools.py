"""对话框接管的单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from witty_browser_auto.agent.dialog_tools import execute_dialog_tool
from witty_browser_auto.browser.dialogs import DIALOG_KINDS, DialogSupervisor
from witty_browser_auto.domain.models import DriverCapabilities, ExecutionScope, TaskSpec


class _RecordingSession:
    """记录应答参数的假会话。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call(self, method, params=None, *, timeout_seconds=None):
        if self.fail:
            raise RuntimeError("连接已断开")
        self.calls.append((method, dict(params or {})))
        return {}


def _dialog(kind: str, *, message: str = "确定吗?", default: str = "") -> dict:
    return {
        "type": kind,
        "message": message,
        "url": "https://shop.test/orders",
        "defaultPrompt": default,
    }


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 默认策略
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "accept"),
    [
        # confirm 与 prompt 背后通常是不可逆操作，默认不替调用方点确定。
        ("confirm", False),
        ("prompt", False),
        # alert 只有一个按钮；beforeunload 拦下的是调用方自己刚要求的导航。
        ("alert", True),
        ("beforeunload", True),
    ],
)
def test_default_policy_per_dialog_kind(kind: str, accept: bool) -> None:
    supervisor = DialogSupervisor()
    session = _RecordingSession()
    _run(supervisor.handle_event(session, _dialog(kind)))
    method, params = session.calls[0]
    assert method == "Page.handleJavaScriptDialog"
    assert params["accept"] is accept


def test_every_dialog_is_answered_even_without_a_rule() -> None:
    # 只要订阅了事件就必须回答，否则渲染进程一直挂起。
    supervisor = DialogSupervisor()
    session = _RecordingSession()
    for kind in ("alert", "confirm", "prompt", "beforeunload"):
        _run(supervisor.handle_event(session, _dialog(kind)))
    assert len(session.calls) == 4


def test_failed_response_is_not_recorded_as_handled() -> None:
    supervisor = DialogSupervisor()
    _run(supervisor.handle_event(_RecordingSession(fail=True), _dialog("confirm")))
    assert supervisor.records() == ()


# ----------------------------------------------------------------------
# 覆盖规则
# ----------------------------------------------------------------------


def test_once_rule_applies_to_a_single_dialog() -> None:
    supervisor = DialogSupervisor()
    supervisor.set_rule("accept", once=True)
    session = _RecordingSession()
    _run(supervisor.handle_event(session, _dialog("confirm")))
    _run(supervisor.handle_event(session, _dialog("confirm")))
    assert [params["accept"] for _, params in session.calls] == [True, False]


def test_session_rule_persists() -> None:
    supervisor = DialogSupervisor()
    supervisor.set_rule("accept", once=False)
    session = _RecordingSession()
    _run(supervisor.handle_event(session, _dialog("confirm")))
    _run(supervisor.handle_event(session, _dialog("confirm")))
    assert [params["accept"] for _, params in session.calls] == [True, True]


def test_rule_can_target_specific_kinds_only() -> None:
    supervisor = DialogSupervisor()
    supervisor.set_rule("accept", once=False, kinds=("confirm",))
    session = _RecordingSession()
    _run(supervisor.handle_event(session, _dialog("confirm")))
    _run(supervisor.handle_event(session, _dialog("prompt")))
    assert [params["accept"] for _, params in session.calls] == [True, False]


def test_prompt_text_is_sent_only_when_accepting_a_prompt() -> None:
    supervisor = DialogSupervisor()
    supervisor.set_rule("accept", prompt_text="订单备注", once=False)
    session = _RecordingSession()
    _run(supervisor.handle_event(session, _dialog("prompt")))
    _run(supervisor.handle_event(session, _dialog("confirm")))
    assert session.calls[0][1]["promptText"] == "订单备注"
    assert "promptText" not in session.calls[1][1]


def test_prompt_falls_back_to_page_default_text() -> None:
    supervisor = DialogSupervisor()
    supervisor.set_rule("accept", once=False)
    session = _RecordingSession()
    _run(supervisor.handle_event(session, _dialog("prompt", default="默认值")))
    assert session.calls[0][1]["promptText"] == "默认值"


def test_invalid_rule_inputs_are_rejected() -> None:
    supervisor = DialogSupervisor()
    with pytest.raises(ValueError, match="accept 或 dismiss"):
        supervisor.set_rule("maybe", once=True)
    with pytest.raises(ValueError, match="未知的对话框类型"):
        supervisor.set_rule("accept", once=True, kinds=("popup",))


def test_records_capture_what_happened() -> None:
    supervisor = DialogSupervisor()
    _run(supervisor.handle_event(_RecordingSession(), _dialog("confirm", message="确定删除吗?")))
    record = supervisor.records()[0]
    assert record.kind == "confirm"
    assert record.message == "确定删除吗?"
    assert record.action == "dismiss"
    assert record.handled_by == "默认策略"


# ----------------------------------------------------------------------
# 工具层
# ----------------------------------------------------------------------


class _FakeDriver:
    def __init__(self) -> None:
        self.capabilities = DriverCapabilities(dialogs=True)
        self.supervisor = DialogSupervisor()

    def dialog_policy(self):
        return self.supervisor.effective_policy()

    def dialog_records(self):
        return [record.public_dict() for record in self.supervisor.records()]

    def set_dialog_rule(self, action, *, prompt_text="", once, kinds=None):
        self.supervisor.set_rule(
            action,
            prompt_text=prompt_text,
            once=once,
            kinds=kinds or DIALOG_KINDS,
        )


def _task(**inputs: str) -> TaskSpec:
    return TaskSpec(
        task_id="t-1",
        goal="删除订单",
        start_url="https://shop.test/",
        scope=ExecutionScope(project_id="p"),
        inputs=dict(inputs),
    )


def test_tool_sets_a_next_only_rule() -> None:
    driver = _FakeDriver()
    outcome = execute_dialog_tool({"action": "accept"}, driver=driver, task=_task())
    assert outcome.data["policy"]["confirm"]["action"] == "accept"
    assert outcome.data["policy"]["confirm"]["source"] == "一次性覆盖"
    assert "下一次" in outcome.message


def test_tool_inspect_does_not_change_policy() -> None:
    driver = _FakeDriver()
    outcome = execute_dialog_tool({"action": "inspect"}, driver=driver, task=_task())
    assert outcome.data["policy"]["confirm"]["source"] == "默认"
    assert outcome.data["dialog_count"] == 0


def test_tool_resolves_prompt_text_from_task_inputs() -> None:
    driver = _FakeDriver()
    execute_dialog_tool(
        {"action": "accept", "prompt_text_input_key": "reason", "dialog_kinds": ["prompt"]},
        driver=driver,
        task=_task(reason="库存不足"),
    )
    session = _RecordingSession()
    _run(driver.supervisor.handle_event(session, _dialog("prompt")))
    assert session.calls[0][1]["promptText"] == "库存不足"


def test_tool_hides_prompt_value_from_the_model() -> None:
    driver = _FakeDriver()
    driver.supervisor.set_rule("accept", prompt_text="机密", once=False, kinds=("prompt",))
    _run(driver.supervisor.handle_event(_RecordingSession(), _dialog("prompt")))
    outcome = execute_dialog_tool({"action": "inspect"}, driver=driver, task=_task())
    assert outcome.data["dialogs"][0]["prompt_text"] == "机密"
    assert outcome.model_data["dialogs"][0]["prompt_text"] == "[REDACTED]"


def test_tool_rejects_bad_arguments() -> None:
    driver = _FakeDriver()
    with pytest.raises(ValueError, match="未知参数"):
        execute_dialog_tool({"action": "accept", "force": True}, driver=driver, task=_task())
    with pytest.raises(ValueError, match="action 只能是"):
        execute_dialog_tool({"action": "maybe"}, driver=driver, task=_task())
    with pytest.raises(ValueError, match="scope 只能是"):
        execute_dialog_tool({"action": "accept", "scope": "forever"}, driver=driver, task=_task())
    with pytest.raises(ValueError, match="只能给一个"):
        execute_dialog_tool(
            {"action": "accept", "prompt_text": "a", "prompt_text_input_key": "b"},
            driver=driver,
            task=_task(b="x"),
        )
    with pytest.raises(ValueError, match="不存在键"):
        execute_dialog_tool(
            {"action": "accept", "prompt_text_input_key": "missing"},
            driver=driver,
            task=_task(),
        )
    with pytest.raises(ValueError, match="只在 action 为 accept"):
        execute_dialog_tool({"action": "dismiss", "prompt_text": "a"}, driver=driver, task=_task())


def test_tool_requires_driver_capability() -> None:
    class _NoDialogs:
        capabilities = DriverCapabilities()

    with pytest.raises(ValueError, match="不支持对话框接管"):
        execute_dialog_tool({"action": "inspect"}, driver=_NoDialogs(), task=_task())
