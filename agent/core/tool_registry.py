"""
工具注册表

参考 gemini-cli 的 tool-registry.ts 设计：
- 统一管理所有工具的注册、发现、排序
- 支持工具的动态注册和移除
- 提供工具的 Schema 获取接口

设计理念：
1. 工具注册表是工具的中央管理器
2. LLM 通过注册表获取可用工具列表
3. 工具调度器通过注册表查找工具
"""

import logging
from typing import Any, Dict, List, Optional, Set
from difflib import SequenceMatcher

from .tool_base import DeclarativeTool, ToolKind

logger = logging.getLogger("agent.core.tool_registry")


class ToolRegistry:
    """
    工具注册表
    
    参考 gemini-cli 的 ToolRegistry 类：
    - 管理所有注册的工具
    - 提供工具查找和 Schema 获取
    - 支持工具排除和过滤
    """
    
    def __init__(self, excluded_tools: Optional[Set[str]] = None):
        """
        初始化工具注册表
        
        Args:
            excluded_tools: 排除的工具名称集合
        """
        self._tools: Dict[str, DeclarativeTool] = {}
        self._excluded_tools = excluded_tools or set()
        self._tool_order: List[str] = []  # 保持注册顺序
    
    def register(self, tool: DeclarativeTool) -> None:
        """
        注册工具
        
        Args:
            tool: 工具实例
        """
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已注册，将被覆盖")
        else:
            self._tool_order.append(tool.name)
        
        self._tools[tool.name] = tool
        logger.debug(f"工具已注册: {tool.name} ({tool.display_name})")
    
    def register_all(self, tools: List[DeclarativeTool]) -> None:
        """
        批量注册工具
        
        Args:
            tools: 工具列表
        """
        for tool in tools:
            self.register(tool)
    
    def unregister(self, tool_name: str) -> bool:
        """
        注销工具
        
        Args:
            tool_name: 工具名称
            
        Returns:
            是否成功注销
        """
        if tool_name in self._tools:
            del self._tools[tool_name]
            self._tool_order.remove(tool_name)
            logger.debug(f"工具已注销: {tool_name}")
            return True
        return False
    
    def get_tool(self, name: str) -> Optional[DeclarativeTool]:
        """
        获取工具
        
        Args:
            name: 工具名称
            
        Returns:
            工具实例（如果存在且未被排除）
        """
        tool = self._tools.get(name)
        if tool and self._is_active(tool):
            return tool
        return None
    
    def _is_active(self, tool: DeclarativeTool) -> bool:
        """检查工具是否活跃（未被排除）"""
        return tool.name not in self._excluded_tools
    
    def get_active_tools(self) -> List[DeclarativeTool]:
        """获取所有活跃的工具（按注册顺序）"""
        return [
            self._tools[name] 
            for name in self._tool_order 
            if name in self._tools and self._is_active(self._tools[name])
        ]
    
    def get_all_tool_names(self) -> List[str]:
        """获取所有活跃工具的名称"""
        return [tool.name for tool in self.get_active_tools()]
    
    def get_function_declarations(self) -> List[Dict[str, Any]]:
        """
        获取所有活跃工具的 Schema 声明
        
        返回符合 OpenAI Function Calling 格式的工具定义列表
        """
        return [tool.schema for tool in self.get_active_tools()]
    
    def get_function_declarations_filtered(self, tool_names: List[str]) -> List[Dict[str, Any]]:
        """
        获取指定工具的 Schema 声明
        
        Args:
            tool_names: 工具名称列表
            
        Returns:
            工具 Schema 列表
        """
        declarations = []
        for name in tool_names:
            tool = self.get_tool(name)
            if tool:
                declarations.append(tool.schema)
        return declarations
    
    def get_tools_by_kind(self, kind: ToolKind) -> List[DeclarativeTool]:
        """
        按类型获取工具
        
        Args:
            kind: 工具类型
            
        Returns:
            指定类型的工具列表
        """
        return [
            tool for tool in self.get_active_tools() 
            if tool.kind == kind
        ]
    
    def suggest_tool(self, unknown_name: str, top_n: int = 3) -> str:
        """
        为未知工具名称生成建议
        
        参考 gemini-cli 的 getToolSuggestion 方法：
        使用相似度算法找到最接近的工具名称
        
        Args:
            unknown_name: 未知的工具名称
            top_n: 返回建议数量
            
        Returns:
            建议字符串
        """
        all_names = self.get_all_tool_names()
        if not all_names:
            return ""
        
        # 计算相似度
        similarities = []
        for name in all_names:
            ratio = SequenceMatcher(None, unknown_name.lower(), name.lower()).ratio()
            similarities.append((name, ratio))
        
        # 排序并取前 N 个
        similarities.sort(key=lambda x: x[1], reverse=True)
        top_suggestions = similarities[:top_n]
        
        if not top_suggestions:
            return ""
        
        suggested_names = [f'"{name}"' for name, _ in top_suggestions]
        
        if len(suggested_names) == 1:
            return f" 你是指 {suggested_names[0]} 吗？"
        else:
            return f" 你是指以下工具之一吗：{', '.join(suggested_names)}？"
    
    def exclude_tool(self, tool_name: str) -> None:
        """排除工具"""
        self._excluded_tools.add(tool_name)
    
    def include_tool(self, tool_name: str) -> None:
        """取消排除工具"""
        self._excluded_tools.discard(tool_name)
    
    def set_excluded_tools(self, tool_names: Set[str]) -> None:
        """设置排除的工具集合"""
        self._excluded_tools = tool_names
    
    def clear(self) -> None:
        """清空所有工具"""
        self._tools.clear()
        self._tool_order.clear()
    
    def __len__(self) -> int:
        """返回活跃工具数量"""
        return len(self.get_active_tools())
    
    def __contains__(self, tool_name: str) -> bool:
        """检查工具是否存在"""
        return self.get_tool(tool_name) is not None
    
    def __repr__(self) -> str:
        active_count = len(self.get_active_tools())
        total_count = len(self._tools)
        return f"ToolRegistry(active={active_count}, total={total_count})"

