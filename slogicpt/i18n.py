"""极简中英双语支持：英文原文作 key，中文查表（Qt-free，可被任意模块导入）。

用法::

    from .i18n import t
    label = t("Test Params")                 # en -> 原样；zh -> "测试参数"
    msg = t("Single step: {}").format(name)   # 占位符在 t() 外面 .format()

约定：``t()`` 只接收**常量英文字面量**（否则查不到 key）；需要插值时用
``t("...{}...").format(...)``。缺 key 时回退英文原文（可见，便于发现漏译）。
语言的持久化不在这里（保持 Qt-free）；由 GUI 用 QSettings 负责，启动时调用
``set_language()`` 即可。
"""
from __future__ import annotations

from ._translations import ZH

LANGS: list[tuple[str, str]] = [("zh", "中文"), ("en", "English")]
_VALID = {code for code, _ in LANGS}

_LANG = "zh"   # 默认中文（团队母语）；启动时由 QSettings 覆盖


def set_language(lang: str) -> None:
    """设置当前语言；非法值忽略。"""
    global _LANG
    if lang in _VALID:
        _LANG = lang


def get_language() -> str:
    return _LANG


def t(en: str) -> str:
    """把英文原文翻成当前语言：en 原样返回，zh 查表（缺则回退英文）。"""
    if _LANG == "en":
        return en
    return ZH.get(en, en)
