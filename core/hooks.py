"""
Hooks in agent loop
"""


class Hooks:

    async def before_interation(self):
        pass

    async def after_llm_ret(self):
        pass

    async def before_tool_call(self):
        pass

    async def before_tool_result(self):
        pass

    async def before_msg_out(self):
        pass





