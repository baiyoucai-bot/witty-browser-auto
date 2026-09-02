"""批量填写、独立等待与会话态存取的单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from witty_browser_auto.agent.form_tools import (
    execute_fill_form,
    execute_storage_state,
    execute_wait_for_condition,
)
from witty_browser_auto.browser.form_fill import FormField, apply_field
from witty_browser_auto.browser.storage_state import (
    export_state,
    import_state,
    read_state_file,
    summarize,
    write_state_file,
)
from witty_browser_auto.domain.models import DriverCapabilities, ExecutionScope, TaskSpec


def _run(coro):
    return asyncio.run(coro)


def _task(**inputs: str) -> TaskSpec:
    return TaskSpec(
        task_id="t-1",
        goal="填表",
        start_url="https://shop.test/form",
        scope=ExecutionScope(project_id="p", allowed_origins=("https://shop.test",)),
        inputs=dict(inputs),
    )


class _ScriptSession:
    """按脚本片段返回预设结果的假会话。"""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    async def call(self, method, params=None, *, timeout_seconds=None):
        params = dict(params or {})
        self.calls.append((method, params))
        declaration = params.get("functionDeclaration", "")
        for marker, value in self.responses.items():
            if marker in declaration:
                return {"result": {"value": value}}
        if "String(this.value)===String(expected)" in declaration:
            return {"result": {"value": True}}
        return {"result": {"value": None}}


# ----------------------------------------------------------------------
# 字段写入与回读
# ----------------------------------------------------------------------


def test_text_field_goes_through_real_key_events() -> None:
    session = _ScriptSession()
    page = _ScriptSession()
    result = _run(
        apply_field(session, page, "obj-1", FormField(0, "text", value="张三", target_id="t"))
    )
    assert result["filled"] is True
    # 必须走 Input.insertText，直接赋值受控组件不会更新自身状态。
    assert any(method == "Input.insertText" for method, _ in page.calls)


def test_text_field_reports_failure_when_readback_differs() -> None:
    session = _ScriptSession({"String(this.value)===String(expected)": False})
    result = _run(apply_field(session, _ScriptSession(), "obj-1", FormField(0, "text", value="x")))
    assert result["filled"] is False
    assert "回读" in result["reason"]


def test_sensitive_text_reports_source_not_value() -> None:
    result = _run(
        apply_field(
            _ScriptSession(),
            _ScriptSession(),
            "obj-1",
            FormField(0, "text", value="超级机密", sensitive=True),
        )
    )
    assert result["value_source"] == "任务输入"
    assert "超级机密" not in json.dumps(result, ensure_ascii=False)


def test_select_reports_available_options_when_nothing_matches() -> None:
    session = _ScriptSession(
        {
            "option.value === wanted": {
                "ok": False,
                "reason": "option_missing",
                "available": ["北京", "上海"],
            }
        }
    )
    result = _run(
        apply_field(session, _ScriptSession(), "obj", FormField(0, "select", value="广州"))
    )
    assert result["filled"] is False
    # 给出实际可选项，调用方才知道该改成什么。
    assert result["available_options"] == ["北京", "上海"]


def test_select_success_returns_matched_text() -> None:
    session = _ScriptSession(
        {"option.value === wanted": {"ok": True, "value": "bj", "text": "北京"}}
    )
    result = _run(
        apply_field(session, _ScriptSession(), "obj", FormField(0, "select", value="北京"))
    )
    assert result["filled"] is True
    assert result["selected_value"] == "bj"


def test_checkbox_disabled_is_reported_clearly() -> None:
    session = _ScriptSession({"this.checked !== desired": {"ok": False, "reason": "disabled"}})
    result = _run(
        apply_field(session, _ScriptSession(), "obj", FormField(0, "checkbox", checked=True))
    )
    assert result["filled"] is False
    assert result["reason"] == "勾选框处于禁用状态"


# ----------------------------------------------------------------------
# fill_form 工具层
# ----------------------------------------------------------------------


class _FormDriver:
    def __init__(self, results=None) -> None:
        self.capabilities = DriverCapabilities(forms=True, storage_state=True)
        self.received: list[FormField] = []
        self._results = results

    async def fill_fields(self, fields):
        self.received = list(fields)
        if self._results is not None:
            return self._results
        return [{**field.describe(), "filled": True} for field in fields]

    async def wait_for(self, condition):
        return {"satisfied": True, "message": "ok", "waited_seconds": 0.5}


def test_fill_form_resolves_task_inputs_without_leaking_them() -> None:
    driver = _FormDriver()
    outcome = _run(
        execute_fill_form(
            {
                "fields": [
                    {"target_id": "a", "input_key": "user"},
                    {"target_id": "b", "text": "备注"},
                    {"target_id": "c", "select_value": "北京"},
                    {"target_id": "d", "checked": True},
                ]
            },
            driver=driver,
            task=_task(user="alice"),
        )
    )
    assert outcome.success is True
    assert [field.kind for field in driver.received] == ["text", "text", "select", "checkbox"]
    assert driver.received[0].value == "alice"
    assert driver.received[0].sensitive is True
    assert "alice" not in json.dumps(outcome.data, ensure_ascii=False)


def test_fill_form_continues_past_a_failed_field() -> None:
    driver = _FormDriver(
        results=[
            {"index": 0, "filled": True},
            {"index": 1, "filled": False, "reason": "下拉框没有匹配的选项"},
            {"index": 2, "filled": True},
        ]
    )
    outcome = _run(
        execute_fill_form(
            {
                "fields": [
                    {"target_id": "a", "text": "1"},
                    {"target_id": "b", "select_value": "x"},
                    {"target_id": "c", "text": "3"},
                ]
            },
            driver=driver,
            task=_task(),
        )
    )
    # 整体失败，但成功的字段照样计入，调用方不必重填。
    assert outcome.success is False
    assert outcome.data["filled_count"] == 2
    assert "第 1 个字段" in outcome.message


def test_fill_form_rejects_ambiguous_field_definitions() -> None:
    driver = _FormDriver()
    task = _task(user="alice")
    with pytest.raises(ValueError, match="target_id 或 locator 之一"):
        _run(execute_fill_form({"fields": [{"text": "x"}]}, driver=driver, task=task))
    with pytest.raises(ValueError, match="target_id 或 locator 之一"):
        _run(
            execute_fill_form(
                {
                    "fields": [
                        {
                            "target_id": "a",
                            "locator": {"strategy": "css", "value": "#a"},
                            "text": "x",
                        }
                    ]
                },
                driver=driver,
                task=task,
            )
        )
    with pytest.raises(ValueError, match="input_key、text、select_value、checked 之一"):
        _run(execute_fill_form({"fields": [{"target_id": "a"}]}, driver=driver, task=task))
    with pytest.raises(ValueError, match="input_key、text、select_value、checked 之一"):
        _run(
            execute_fill_form(
                {"fields": [{"target_id": "a", "text": "x", "checked": True}]},
                driver=driver,
                task=task,
            )
        )
    with pytest.raises(ValueError, match="任务输入键不存在"):
        _run(
            execute_fill_form(
                {"fields": [{"target_id": "a", "input_key": "missing"}]},
                driver=driver,
                task=task,
            )
        )
    with pytest.raises(ValueError, match="非空数组"):
        _run(execute_fill_form({"fields": []}, driver=driver, task=task))


# ----------------------------------------------------------------------
# wait_for_condition
# ----------------------------------------------------------------------


def test_wait_for_condition_passes_timeout_through() -> None:
    captured = {}

    class _Driver:
        capabilities = DriverCapabilities()

        async def wait_for(self, condition):
            captured["kind"] = condition.kind
            captured["timeout"] = condition.timeout_seconds
            return {"satisfied": True, "message": "", "waited_seconds": 1.2}

    outcome = _run(
        execute_wait_for_condition(
            {"expect_kind": "text_contains", "expect_value": "已完成", "timeout_seconds": 45},
            driver=_Driver(),
        )
    )
    assert captured == {"kind": "text_contains", "timeout": 45.0}
    assert outcome.success is True


def test_wait_for_condition_unsatisfied_is_a_business_failure_not_an_error() -> None:
    class _Driver:
        capabilities = DriverCapabilities()

        async def wait_for(self, condition):
            return {"satisfied": False, "message": "页面文本未出现", "waited_seconds": 10.0}

    outcome = _run(
        execute_wait_for_condition(
            {"expect_kind": "text_contains", "expect_value": "已完成"}, driver=_Driver()
        )
    )
    assert outcome.success is False
    assert "仍未满足" in outcome.message


def test_wait_for_condition_rejects_bad_arguments() -> None:
    driver = _FormDriver()
    with pytest.raises(ValueError, match="expect_kind 只能是"):
        _run(
            execute_wait_for_condition({"expect_kind": "vibes", "expect_value": "x"}, driver=driver)
        )
    with pytest.raises(ValueError, match="expect_value 必须是非空"):
        _run(
            execute_wait_for_condition(
                {"expect_kind": "text_contains", "expect_value": " "}, driver=driver
            )
        )
    with pytest.raises(ValueError, match=r"0\.1 到 300"):
        _run(
            execute_wait_for_condition(
                {"expect_kind": "text_contains", "expect_value": "x", "timeout_seconds": 9999},
                driver=driver,
            )
        )


# ----------------------------------------------------------------------
# 会话态
# ----------------------------------------------------------------------


class _StateSession:
    def __init__(self, cookies) -> None:
        self.cookies = cookies
        self.written: list[dict] = []

    async def call(self, method, params=None, *, timeout_seconds=None):
        if method == "Network.getCookies":
            return {"cookies": self.cookies}
        if method == "Network.setCookies":
            self.written = list((params or {}).get("cookies", []))
        return {}


class _StateFrame:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.restored: list = []

    async def call_on_document(self, declaration, arguments=None):
        if "location.origin;" in declaration and "dump" not in declaration:
            return {"result": {"value": "https://shop.test"}}
        if "const dump" in declaration:
            return {"result": {"value": self.payload}}
        self.restored = list(arguments or [])
        return {"result": {"value": {"localStorage": 2, "sessionStorage": 1}}}


def test_export_uses_playwright_compatible_shape() -> None:
    session = _StateSession(
        [{"name": "sid", "value": "abc", "domain": "shop.test", "path": "/", "secure": True}]
    )
    frame = _StateFrame(
        {
            "origin": "https://shop.test",
            "localStorage": [{"name": "token", "value": "t1"}],
            "sessionStorage": [],
        }
    )
    state = _run(export_state(session, frame, urls=["https://shop.test"]))
    assert state["cookies"][0]["name"] == "sid"
    assert state["origins"][0]["origin"] == "https://shop.test"
    assert state["origins"][0]["localStorage"] == [{"name": "token", "value": "t1"}]


def test_import_skips_cookies_outside_the_authorized_origins() -> None:
    session = _StateSession([])
    frame = _StateFrame({})
    state = {
        "cookies": [
            {"name": "sid", "value": "a", "domain": "shop.test", "path": "/", "secure": True},
            {"name": "evil", "value": "b", "domain": "attacker.test", "path": "/", "secure": True},
        ],
        "origins": [],
    }
    outcome = _run(import_state(session, frame, state, allowed_origins={"https://shop.test"}))
    assert outcome["cookies_applied"] == 1
    assert outcome["cookies_skipped"] == ["evil"]
    assert [item["name"] for item in session.written] == ["sid"]


def test_import_defers_storage_for_other_origins() -> None:
    session = _StateSession([])
    frame = _StateFrame({})
    state = {
        "cookies": [],
        "origins": [
            {"origin": "https://other.test", "localStorage": [{"name": "k", "value": "v"}]}
        ],
    }
    outcome = _run(import_state(session, frame, state, allowed_origins={"https://shop.test"}))
    # Web Storage 只能写进当前页面自己的 origin。
    assert outcome["origins_skipped"] == ["https://other.test"]


def test_state_file_is_private_and_round_trips(tmp_path: Path) -> None:
    state = {"cookies": [{"name": "sid", "value": "secret"}], "origins": []}
    path = write_state_file(state, tmp_path / "states")
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert read_state_file(path) == state


def test_state_summary_gives_counts_not_values() -> None:
    summary = summarize(
        {
            "cookies": [{"name": "sid", "value": "secret"}],
            "origins": [
                {"origin": "https://shop.test", "localStorage": [{"name": "token", "value": "t"}]}
            ],
        }
    )
    assert summary["cookie_count"] == 1
    assert summary["cookie_names"] == ["sid"]
    assert "secret" not in json.dumps(summary, ensure_ascii=False)


class _StateDriver:
    def __init__(self) -> None:
        self.capabilities = DriverCapabilities(storage_state=True)
        self.imported = None

    async def export_storage_state(self, *, urls):
        return {"cookies": [{"name": "sid", "value": "secret"}], "origins": []}

    async def import_storage_state(self, state, *, allowed_origins, clear_existing=False):
        self.imported = state
        return {
            "cookies_applied": 1,
            "cookies_skipped": [],
            "storage_written": {"localStorage": 0, "sessionStorage": 0},
            "origins_skipped": [],
        }


def test_export_hides_the_snapshot_from_the_model(tmp_path: Path) -> None:
    outcome = _run(
        execute_storage_state(
            {"operation": "export"},
            driver=_StateDriver(),
            task=_task(),
            artifact_root=tmp_path,
        )
    )
    assert outcome.data["state"]["cookies"][0]["value"] == "secret"
    # 登录凭据不能进模型上下文。
    assert "secret" not in json.dumps(outcome.model_data, ensure_ascii=False)
    assert Path(outcome.data["file_path"]).is_file()


def test_import_reads_back_the_exported_file(tmp_path: Path) -> None:
    driver = _StateDriver()
    exported = _run(
        execute_storage_state(
            {"operation": "export"}, driver=driver, task=_task(), artifact_root=tmp_path
        )
    )
    _run(
        execute_storage_state(
            {"operation": "import", "file_path": exported.data["file_path"]},
            driver=driver,
            task=_task(),
            artifact_root=tmp_path,
        )
    )
    assert driver.imported["cookies"][0]["name"] == "sid"


def test_storage_state_rejects_bad_arguments(tmp_path: Path) -> None:
    driver = _StateDriver()
    task = _task()
    with pytest.raises(ValueError, match="operation 只能是"):
        _run(
            execute_storage_state(
                {"operation": "sync"}, driver=driver, task=task, artifact_root=tmp_path
            )
        )
    with pytest.raises(ValueError, match="state 或 file_path 之一"):
        _run(
            execute_storage_state(
                {"operation": "import"}, driver=driver, task=task, artifact_root=tmp_path
            )
        )
    with pytest.raises(ValueError, match="state 或 file_path 之一"):
        _run(
            execute_storage_state(
                {"operation": "import", "state": {}, "file_path": "/tmp/x.json"},
                driver=driver,
                task=task,
                artifact_root=tmp_path,
            )
        )
    with pytest.raises(ValueError, match="不接受 state 或 clear_existing"):
        _run(
            execute_storage_state(
                {"operation": "export", "clear_existing": True},
                driver=driver,
                task=task,
                artifact_root=tmp_path,
            )
        )
    with pytest.raises(ValueError, match="文件不存在"):
        _run(
            execute_storage_state(
                {"operation": "import", "file_path": str(tmp_path / "nope.json")},
                driver=driver,
                task=task,
                artifact_root=tmp_path,
            )
        )
