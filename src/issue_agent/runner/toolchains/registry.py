from __future__ import annotations

from typing import TYPE_CHECKING

from issue_agent.runner.toolchains.nodejs import NodejsPlugin

if TYPE_CHECKING:
    from issue_agent.runner.toolchains.base import ToolchainPlugin

BUILTIN_PLUGINS: dict[str, type[ToolchainPlugin]] = {
    NodejsPlugin.name: NodejsPlugin,
}
