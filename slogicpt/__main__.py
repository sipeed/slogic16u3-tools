import sys

# 版本护栏：本项目依赖标准库 tomllib（Python 3.11 起）。低版本会在下面的
# `from slogicpt.gui import main` 处以晦涩的 ModuleNotFoundError: tomllib 崩溃，
# 对产线人员不友好，故在导入前给出明确提示。
if sys.version_info < (3, 11):
    sys.stderr.write(
        "SLogicPT requires Python 3.11 or newer (depends on the standard "
        "library tomllib).\n"
        "SLogicPT 需要 Python 3.11 或更高版本（依赖标准库 tomllib）。\n"
        f"Current version is Python {sys.version.split()[0]}; please upgrade "
        "and retry.\n"
        f"当前为 Python {sys.version.split()[0]}，请升级后重试。\n")
    raise SystemExit(1)

from slogicpt.gui import main

if __name__ == "__main__":
    main()
