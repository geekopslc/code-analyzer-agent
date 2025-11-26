"""
工具基类定义

我们结合 declarative 风格与 Pythonic 实践，自建了一套轻量的工具抽象：
- DeclarativeTool 专注于参数规范与执行编排
- ToolInvocation 作为可选的执行封装，便于复杂工具共享逻辑
- ToolResult 统一描述运行结果

核心理念：
1. Schema 描述能力与执行逻辑解耦
2. 参数预处理、校验、执行三段式流水线
3. 方便扩展确认机制与安全防护
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar, Union

logger = logging.getLogger("agent.core.tool_base")


class ToolKind(Enum):
    """工具类型，用于分类和权限控制"""
    READ = "read"           # 读取操作
    EDIT = "edit"           # 编辑操作
    DELETE = "delete"       # 删除操作
    SEARCH = "search"       # 搜索操作
    EXECUTE = "execute"     # 执行命令
    MEMORY = "memory"       # 记忆操作
    TASK = "task"          # 任务管理
    OTHER = "other"         # 其他


# 有副作用的工具类型
MUTATOR_KINDS = [ToolKind.EDIT, ToolKind.DELETE, ToolKind.EXECUTE]


@dataclass
class ToolLocation:
    """工具操作的文件位置"""
    path: str
    line: Optional[int] = None


@dataclass
class ToolError:
    """工具执行错误"""
    message: str
    error_type: Optional[str] = None


@dataclass
class ToolResult:
    """
    工具执行结果
    
    参考 gemini-cli 的 ToolResult 接口：
    - llm_content: 发送给 LLM 的内容
    - display: 用户展示的内容
    - error: 错误信息（如果有）
    """
    llm_content: str  # 发送给 LLM 的内容
    display: Optional[str] = None  # 用户展示的内容
    error: Optional[ToolError] = None  # 错误信息
    
    @property
    def is_success(self) -> bool:
        """是否成功"""
        return self.error is None
    
    @classmethod
    def success(cls, content: str, display: Optional[str] = None) -> "ToolResult":
        """创建成功结果"""
        return cls(llm_content=content, display=display or content)
    
    @classmethod
    def failure(cls, message: str, error_type: Optional[str] = None) -> "ToolResult":
        """创建失败结果"""
        return cls(
            llm_content=f"错误: {message}",
            display=f"错误: {message}",
            error=ToolError(message=message, error_type=error_type)
        )


# 类型变量，用于泛型
TParams = TypeVar("TParams", bound=Dict[str, Any])
TResult = TypeVar("TResult", bound=ToolResult)


class ToolInvocation(ABC, Generic[TParams]):
    """
    工具调用接口
    
    参考 gemini-cli 的 ToolInvocation 接口：
    一个 ToolInvocation 实例代表一次已验证的、待执行的工具调用。
    它封装了执行所需的所有参数和上下文。
    """
    
    @property
    @abstractmethod
    def params(self) -> TParams:
        """获取已验证的参数"""
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """获取操作描述，用于日志和确认提示"""
        pass
    
    def get_locations(self) -> List[ToolLocation]:
        """获取工具操作涉及的文件位置"""
        return []
    
    @abstractmethod
    def execute(self, repo_root: str) -> ToolResult:
        """
        执行工具操作
        
        Args:
            repo_root: 仓库根目录
            
        Returns:
            ToolResult: 执行结果
        """
        pass
    
    def should_confirm(self) -> bool:
        """是否需要用户确认（用于危险操作）"""
        return False


class BaseToolInvocation(ToolInvocation[TParams]):
    """
    工具调用基类
    
    提供 ToolInvocation 的基本实现，子类只需实现 execute 和 get_description 方法。
    """
    
    def __init__(
        self,
        params: TParams,
        tool_name: Optional[str] = None,
        tool_display_name: Optional[str] = None,
    ):
        self._params = params
        self._tool_name = tool_name
        self._tool_display_name = tool_display_name
    
    @property
    def params(self) -> TParams:
        return self._params
    
    @property
    def tool_name(self) -> Optional[str]:
        return self._tool_name
    
    @property
    def tool_display_name(self) -> Optional[str]:
        return self._tool_display_name


class DeclarativeTool(ABC, Generic[TParams]):
    """
    声明式工具基类
    
    提供统一的参数 Schema、验证和执行入口，子类只需关心自身逻辑。
    """
    
    def __init__(
        self,
        name: str,
        display_name: str,
        description: str,
        kind: ToolKind,
        parameter_schema: Dict[str, Any],
    ):
        self._name = name
        self._display_name = display_name
        self._description = description
        self._kind = kind
        self._parameter_schema = parameter_schema
    
    @property
    def name(self) -> str:
        """工具名称（API 调用用）"""
        return self._name
    
    @property
    def display_name(self) -> str:
        """显示名称（用户界面用）"""
        return self._display_name
    
    @property
    def description(self) -> str:
        """工具描述"""
        return self._description
    
    @property
    def kind(self) -> ToolKind:
        """工具类型"""
        return self._kind
    
    @property
    def parameter_schema(self) -> Dict[str, Any]:
        """参数 JSON Schema"""
        return self._parameter_schema
    
    @property
    def schema(self) -> Dict[str, Any]:
        """
        完整的工具 schema，符合 OpenAI Function Calling 格式
        """
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": self._description,
                "parameters": self._parameter_schema,
            }
        }
    
    def validate_params(self, params: TParams) -> Optional[str]:
        """
        验证参数
        
        Args:
            params: 原始参数
            
        Returns:
            错误消息（如果验证失败），None 表示验证通过
        """
        # 基础验证：检查必填字段
        required = self._parameter_schema.get("required", [])
        for field in required:
            if field not in params or params.get(field) is None:
                return f"缺少必填参数: {field}"
        return None
    
    def prepare_params(self, params: TParams) -> TParams:
        """
        预处理 + 校验参数
        
        子类可复写 normalize_params() 进行自定义转换。
        """
        normalized = self.normalize_params(params)
        error = self.validate_params(normalized)
        if error:
            raise ValueError(error)
        return normalized
    
    def normalize_params(self, params: TParams) -> TParams:
        """默认进行浅拷贝，子类可覆盖实现更复杂的规范化逻辑"""
        if params is None:
            return params
        if isinstance(params, dict):
            return params.copy()  # type: ignore[return-value]
        return params
    
    def execute(self, params: TParams, repo_root: str) -> ToolResult:
        """
        对外唯一执行入口：预处理 -> 校验 -> 真正执行
        """
        try:
            prepared = self.prepare_params(params)
        except ValueError as e:
            return ToolResult.failure(str(e), "VALIDATION_ERROR")
        except Exception as e:
            return ToolResult.failure(str(e), "VALIDATION_EXCEPTION")
        
        return self.invoke(prepared, repo_root)
    
    def invoke(self, prepared_params: TParams, repo_root: str) -> ToolResult:
        """
        在参数已通过校验的前提下执行，供调度器复用。
        """
        try:
            return self._execute(prepared_params, repo_root)
        except Exception as e:
            logger.exception("工具执行异常: %s", self._name)
            return ToolResult.failure(str(e), "EXECUTION_ERROR")
    
    @abstractmethod
    def _execute(self, params: TParams, repo_root: str) -> ToolResult:
        """子类实现实际执行逻辑"""
        pass


class BaseDeclarativeTool(DeclarativeTool[TParams]):
    """
    声明式工具默认实现：仍然允许通过 ToolInvocation 复用复杂执行逻辑。
    """
    
    def _execute(self, params: TParams, repo_root: str) -> ToolResult:
        invocation = self.create_invocation(params)
        return invocation.execute(repo_root)
    
    @abstractmethod
    def create_invocation(self, params: TParams) -> ToolInvocation[TParams]:
        """子类返回对应的 ToolInvocation 实例"""
        pass

