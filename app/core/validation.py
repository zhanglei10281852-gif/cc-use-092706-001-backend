from __future__ import annotations

from app.core.errors import ValidationError


def normalize_required_text(value: str, *, field_name: str = "内容", max_length: int | None = None) -> str:
    """对必填文本做一致的空白处理：去除首尾空白后拒绝空内容。

    接口层（请求模型）与服务层（领域逻辑入口）共用本函数，保证无论调用
    路径如何，空白字符串都不会带着空内容进入持久化或流水/审计记录。
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field_name}必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name}不能为空")
    if max_length is not None and len(normalized) > max_length:
        raise ValidationError(f"{field_name}长度不能超过 {max_length} 个字符")
    return normalized
