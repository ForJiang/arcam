"""arcam 的本机网页前端（webui）：静态三件套 + 3D 运行时。

这个包**只装数据文件**（index.html / *.js / *.css / mmd/ 整个 3D 运行时），
没有任何可导入的 Python 符号——``arcam.webapp.WEBUI_DIR`` 按 ``__file__``
定位本目录来伺服它们。加 ``__init__.py`` 是为了让 setuptools 把它当真正的
包：否则 package-data 的子目录 glob（``mmd/*``）不完全生效，wheel 会漏掉
three.js/loaders/poses.json——装态的「3D 立绘」动态 import 直接 404（源码树
跑得出来、装态跑不出来的那类坑）。
"""
