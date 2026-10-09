"""tests run against the source tree without installing the package."""

import atexit
import gc
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _release_onnxruntime() -> None:
    """退出前跑一次 gc，把可能被循环引用拖到关机的 ORT 会话收掉。

    onnxruntime 的 C++ 运行时有自己的工作线程；会话对象若活到解释器关机才释放
    （异常 traceback / 循环引用都会让它活下来），其 teardown 会与 threading 的
    关机竞态，表现为进程退出码 134（SIGABRT，``libc++abi: ... recursive_mutex
    lock failed``）——测试全绿，但看退出码的 CI 门禁会误报。在 atexit（Python
    仍健全时）主动回收，把窗口压到最小。未装 onnxruntime 时是 no-op。
    """
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return
    gc.collect()


atexit.register(_release_onnxruntime)
