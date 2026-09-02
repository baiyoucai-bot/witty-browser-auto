"""受控文件上传：校验本地路径后通过 DOM.setFileInputFiles 注入 file input。"""

from __future__ import annotations

from pathlib import Path

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.domain.errors import PolicyViolationError, TargetNotFoundError

# Chrome 对相对路径和目录都不会严格拒绝，会造出空文件或把目录当成文件，
# 所以路径合法性必须在调用 CDP 之前由我们钉死。
_MAX_UPLOAD_FILES = 10
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

_ATTACHED_FILES_SCRIPT = """
function(expected) {
  if (!(this instanceof HTMLInputElement) || this.type !== 'file') {
    return {ok: false, reason: 'not_file_input'};
  }
  const actual = Array.from(this.files || []).map((file) => ({
    name: file.name,
    size: file.size,
  }));
  if (actual.length !== expected.length) {
    return {ok: false, reason: 'count', actual: actual.length, expected: expected.length};
  }
  for (let index = 0; index < expected.length; index += 1) {
    if (actual[index].name !== expected[index].name
        || actual[index].size !== expected[index].size) {
      return {ok: false, reason: 'mismatch', actual, expected};
    }
  }
  return {ok: true, files: actual};
}
"""


def resolve_upload_paths(paths: list[str]) -> list[Path]:
    """把调用方给出的路径收敛成可上传的绝对文件路径。

    探测证明：相对路径会被 Chrome 静默接受、不存在的路径会变成 0 字节 File、
    目录也会被当成文件。这些都会让页面以为上传成功，所以这里一律拒绝。
    """

    if not paths:
        raise PolicyViolationError("上传至少需要一个本地文件路径")
    if len(paths) > _MAX_UPLOAD_FILES:
        raise PolicyViolationError(f"单次最多上传 {_MAX_UPLOAD_FILES} 个文件")

    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise PolicyViolationError("上传路径不能为空")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise PolicyViolationError(f"上传路径必须是绝对路径：{raw}")
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PolicyViolationError(f"上传文件不存在：{raw}") from exc
        if not path.is_file():
            raise PolicyViolationError(f"上传路径不是普通文件：{raw}")
        size = path.stat().st_size
        if size > _MAX_UPLOAD_BYTES:
            raise PolicyViolationError(f"上传文件超过 {_MAX_UPLOAD_BYTES} 字节上限：{path.name}")
        if path in seen:
            continue
        seen.add(path)
        resolved.append(path)
    return resolved


async def set_file_input_files(
    session: CdpTargetSession,
    object_id: str,
    paths: list[Path],
) -> list[dict[str, int | str]]:
    """把本地文件注入到 file input，并回读确认名称与大小一致。"""

    await session.call(
        "DOM.setFileInputFiles",
        {"objectId": object_id, "files": [str(path) for path in paths]},
    )
    expected = [{"name": path.name, "size": path.stat().st_size} for path in paths]
    checked = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _ATTACHED_FILES_SCRIPT,
            "arguments": [{"value": expected}],
            "returnByValue": True,
        },
    )
    result = checked.get("result", {}).get("value")
    if not isinstance(result, dict):
        raise RuntimeError("浏览器没有返回文件上传回读结果")
    if result.get("reason") == "not_file_input":
        raise TargetNotFoundError("目标不是 file 类型的 input 元素")
    if result.get("ok") is not True:
        raise RuntimeError("文件已发送，但 input 回读校验失败")
    files = result.get("files")
    if not isinstance(files, list):
        raise RuntimeError("文件上传回读结果缺少文件列表")
    return [
        {"name": str(item.get("name", "")), "size": int(item.get("size", 0))}
        for item in files
        if isinstance(item, dict)
    ]
