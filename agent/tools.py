"""
工具模块 - 兼容层

此模块提供了与现有代码的向后兼容接口，同时使用新的核心模块实现。

新架构（参考 gemini-cli）：
- agent.core.tool_base: 工具基类定义
- agent.core.tool_registry: 工具注册表
- agent.core.tool_scheduler: 工具调度器
- agent.core.builtin_tools: 内置工具实现
- agent.core.tasklist_tools: 任务清单工具

向后兼容接口：
- get_tools(): 返回工具 schema 列表（OpenAI Function Calling 格式）
- execute_tool(): 执行工具调用
"""

import logging
from typing import Any, Dict, List, Optional

# 导入新的核心模块
from .core import (
    ToolRegistry,
    ToolResult,
    create_builtin_tools,
    create_tasklist_tools,
    execute_tool_simple,
)

# 配置日志
logger = logging.getLogger("agent.tools")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# 全局工具注册表（延迟初始化）
_global_registry: Optional[ToolRegistry] = None


def _get_registry() -> ToolRegistry:
    """获取全局工具注册表"""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
        _global_registry.register_all(create_builtin_tools())
        _global_registry.register_all(create_tasklist_tools())
    return _global_registry


def get_tools() -> List[Dict[str, Any]]:
    """
    获取工具 schema 列表
    
    返回符合 OpenAI Function Calling 格式的工具定义列表。
    此函数保持与现有代码的向后兼容。
    
    Returns:
        工具 schema 列表
    """
    registry = _get_registry()
    return registry.get_function_declarations()


def execute_tool(tool_name: str, arguments: Dict[str, Any], repo_root: str) -> str:
    """
    执行工具调用
    
    此函数保持与现有代码的向后兼容接口。
    
    Args:
        tool_name: 工具名称
        arguments: 工具参数
        repo_root: 仓库根目录
        
    Returns:
        执行结果字符串
    """
    registry = _get_registry()
    
    try:
        result = execute_tool_simple(registry, tool_name, arguments, repo_root)
        
        if result.is_success:
            return result.llm_content
        else:
            return f"错误: {result.error.message if result.error else '未知错误'}"
    except Exception as e:
        logger.error(f"工具执行异常: {tool_name}: {e}")
        return f"错误: {str(e)}"


def get_registry() -> ToolRegistry:
    """
    获取工具注册表
    
    供需要直接访问注册表的代码使用。
    
    Returns:
        ToolRegistry: 工具注册表
    """
    return _get_registry()


def reset_registry() -> None:
    """
    重置工具注册表
    
    用于测试或需要重新初始化工具的场景。
    """
    global _global_registry
    _global_registry = None


# 导出常用类型和函数
__all__ = [
    "get_tools",
    "execute_tool",
    "get_registry",
    "reset_registry",
    "ToolRegistry",
    "ToolResult",
]
