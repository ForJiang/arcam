"""``python -m arcam`` 入口（等价于装好包后的 ``arcam`` 命令）。"""

from __future__ import annotations

import sys

from arcam.cli import main

if __name__ == "__main__":
    sys.exit(main())
