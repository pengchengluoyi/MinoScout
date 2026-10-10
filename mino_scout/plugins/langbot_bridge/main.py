from langbot_plugin.api.definition.plugin import BasePlugin


class MinoBridge(BasePlugin):
    """空壳：逻辑都在 components/event_listener/forward.py。"""

    async def initialize(self) -> None:
        await super().initialize()
