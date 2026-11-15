"""``python -m zero`` 的入口。

打包后的 .app 启动器不再依赖 console script 的绝对路径 shebang，而是用
内嵌解释器执行 ``-m zero``；``shell._launch_command`` 的自重启分支也认这个
形态（``sys.argv[0]`` 以 ``__main__.py`` 结尾）。
"""

from zero.main import main

raise SystemExit(main())
