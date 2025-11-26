"""
Agent Core 模块

参考 gemini-cli 的分层设计，将核心 AI 逻辑与应用层分离。
此模块封装了所有与 AI Agent 相关的原子能力和核心逻辑：
- 工具定义与注册机制 (ToolRegistry)
- 工具调度执行器 (ToolScheduler)
- 工具基类 (DeclarativeTool, ToolInvocation)

架构设计理念（参考 gemini-cli）：

1. 可复用的 Agent 内核架构
   - 核心 AI 逻辑与前端交互界面分离
   - 内核层不包含任何 UI 相关代码
   - API 设计与具体应用无关

2. LLM 作为动态调度器的开发范式
   - 开发者声明式地注册原子化工具
   - 工具组合的决策权交给 LLM
   - LLM 在运行时动态生成执行计划

3. 工具系统的声明式设计
   - DeclarativeTool: 工具定义（schema + 验证 + 执行）
   - ToolInvocation: 已验证的工具调用封装
   - ToolRegistry: 工具注册表，管理工具生命周期
   - ToolScheduler: 工具调度器，管理调用队列
"""

from .tool_base import (
    ToolResult,
    ToolError,
    ToolKind,
    ToolLocation,
    ToolInvocation,
    BaseToolInvocation,
    DeclarativeTool,
    BaseDeclarativeTool,
    MUTATOR_KINDS,
)
from .tool_registry import ToolRegistry
from .tool_scheduler import (
    ToolScheduler,
    ToolCallStatus,
    ToolCall,
    ToolCallRequest,
    ToolCallResponse,
    execute_tool_simple,
)
from .builtin_tools import (
    ReadFileTool,
    ReadDirectoryTool,
    GrepTool,
    GlobTool,
    FindTool,
    BashTool,
    SaveMemoryTool,
    SaveFunctionAnalysisTool,
    create_builtin_tools,
    get_builtin_tool_schemas,
)
from .tasklist_tools import (
    TaskListViewTool,
    TaskListCreateTool,
    TaskListUpdateTool,
    TaskListDeleteTool,
    TaskListClearTool,
    SyncStateTool,
    create_tasklist_tools,
)

__all__ = [
    # 工具结果和类型
    "ToolResult",
    "ToolError",
    "ToolKind",
    "ToolLocation",
    "MUTATOR_KINDS",
    # 工具调用
    "ToolInvocation",
    "BaseToolInvocation",
    # 声明式工具基类
    "DeclarativeTool",
    "BaseDeclarativeTool",
    # 工具注册表
    "ToolRegistry",
    # 工具调度器
    "ToolScheduler",
    "ToolCallStatus",
    "ToolCall",
    "ToolCallRequest",
    "ToolCallResponse",
    "execute_tool_simple",
    # 内置工具
    "ReadFileTool",
    "ReadDirectoryTool",
    "GrepTool",
    "GlobTool",
    "FindTool",
    "BashTool",
    "SaveMemoryTool",
    "SaveFunctionAnalysisTool",
    "create_builtin_tools",
    "get_builtin_tool_schemas",
    # 任务清单工具
    "TaskListViewTool",
    "TaskListCreateTool",
    "TaskListUpdateTool",
    "TaskListDeleteTool",
    "TaskListClearTool",
    "SyncStateTool",
    "create_tasklist_tools",
]


def create_default_registry() -> ToolRegistry:
    """
    创建包含所有默认工具的注册表
    
    Returns:
        ToolRegistry: 已注册所有内置工具的注册表
    """
    registry = ToolRegistry()
    registry.register_all(create_builtin_tools())
    registry.register_all(create_tasklist_tools())
    return registry

