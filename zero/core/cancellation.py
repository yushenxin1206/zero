"""用户手动终止当前 turn 的协作式取消。"""


class TurnCancelled(Exception):
    """用户主动终止当前 turn；由 AgentLoop 捕获并做收尾。

    经 bus.stop_queue 旁路信号触发（channel 无关）：AgentRunner 在安全点
    （迭代开头 / LLM 流结束 / 工具执行前后）检查 stop_event 后抛出；
    run_stream 放行本异常，不让它被转成 error 事件。
    """
