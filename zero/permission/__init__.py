"""权限/能力包络子系统。

- ``envelope``：跨平台的包络、判定（P/G/D）、扩权表、策略文件、CLI
- ``seatbelt``：macOS 内核沙箱后端（SBPL 渲染 / sandbox-exec 包裹 / 自检）
- ``guard``：``before_tool_call`` 守卫，把能力申请裁定并分发审批

依赖方向是单向的：``guard → envelope → seatbelt``。``seatbelt`` 不 import
``envelope``（参数以 duck typing 传入），避免成环。
"""
