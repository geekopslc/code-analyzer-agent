"""
工具调度器

参考 gemini-cli 的 coreToolScheduler.ts 设计：
- 管理工具调用的完整生命周期
- 支持工具调用队列
- 提供工具调用状态跟踪
- 支持确认机制和取消操作

工具调用生命周期：
validating -> scheduled -> executing -> success/error/cancelled
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

from .tool_base import (
    DeclarativeTool,
    ToolResult,
)
from .tool_registry import ToolRegistry

logger = logging.getLogger("agent.core.tool_scheduler")


class ToolCallStatus(Enum):
    """工具调用状态"""
    VALIDATING = "validating"       # 验证参数中
    SCHEDULED = "scheduled"         # 已调度，等待执行
    EXECUTING = "executing"         # 执行中
    SUCCESS = "success"             # 执行成功
    ERROR = "error"                 # 执行出错
    CANCELLED = "cancelled"         # 已取消


@dataclass
class ToolCallRequest:
    """工具调用请求"""
    call_id: str                    # 调用 ID
    name: str                       # 工具名称
    args: Dict[str, Any]            # 参数


@dataclass
class ToolCallResponse:
    """工具调用响应"""
    call_id: str
    result: ToolResult
    error: Optional[str] = None


@dataclass
class ToolCall:
    """
    工具调用记录
    
    跟踪工具调用的完整生命周期：
    - 请求信息
    - 当前状态
    - 执行结果
    - 时间统计
    """
    request: ToolCallRequest
    status: ToolCallStatus
    tool: Optional[DeclarativeTool] = None
    prepared_params: Optional[Dict[str, Any]] = None
    response: Optional[ToolCallResponse] = None
    start_time: Optional[float] = None
    duration_ms: Optional[float] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "call_id": self.request.call_id,
            "tool_name": self.request.name,
            "status": self.status.value,
            "args": self.request.args,
        }
        if self.response:
            result["result"] = self.response.result.llm_content
        if self.duration_ms:
            result["duration_ms"] = self.duration_ms
        if self.error_message:
            result["error"] = self.error_message
        return result


# 回调类型定义
OutputUpdateHandler = Callable[[str, str], None]  # (call_id, output) -> None
ToolCallsUpdateHandler = Callable[[List[ToolCall]], None]  # (tool_calls) -> None
AllCompleteHandler = Callable[[List[ToolCall]], None]  # (completed_calls) -> None


class ToolScheduler:
    """
    工具调度器
    
    参考 gemini-cli 的 CoreToolScheduler 类：
    - 管理工具调用队列
    - 顺序执行工具调用
    - 提供状态更新回调
    - 支持取消操作
    
    使用示例：
    ```python
    registry = ToolRegistry()
    registry.register(read_file_tool)
    
    scheduler = ToolScheduler(registry, repo_root="/path/to/repo")
    
    # 调度工具调用
    scheduler.schedule([
        ToolCallRequest("call_1", "read_file", {"file_path": "main.py"})
    ])
    
    # 获取完成的调用
    completed = scheduler.get_completed_calls()
    ```
    """
    
    def __init__(
        self,
        registry: ToolRegistry,
        repo_root: str,
        on_output_update: Optional[OutputUpdateHandler] = None,
        on_tool_calls_update: Optional[ToolCallsUpdateHandler] = None,
        on_all_complete: Optional[AllCompleteHandler] = None,
    ):
        """
        初始化调度器
        
        Args:
            registry: 工具注册表
            repo_root: 仓库根目录
            on_output_update: 输出更新回调
            on_tool_calls_update: 工具调用状态更新回调
            on_all_complete: 所有调用完成回调
        """
        self._registry = registry
        self._repo_root = repo_root
        self._on_output_update = on_output_update
        self._on_tool_calls_update = on_tool_calls_update
        self._on_all_complete = on_all_complete
        
        # 当前活跃的工具调用
        self._active_calls: List[ToolCall] = []
        # 等待队列
        self._queue: List[ToolCall] = []
        # 已完成的调用
        self._completed_calls: List[ToolCall] = []
        # 是否正在调度
        self._is_scheduling = False
        # 是否已取消
        self._is_cancelled = False
        # 工具调用计数器
        self._call_counter = 0
    
    def schedule(
        self,
        requests: Union[ToolCallRequest, List[ToolCallRequest]],
    ) -> List[ToolCall]:
        """
        调度工具调用
        
        Args:
            requests: 工具调用请求（单个或列表）
            
        Returns:
            已完成的工具调用列表
        """
        if isinstance(requests, ToolCallRequest):
            requests = [requests]
        
        self._is_scheduling = True
        self._is_cancelled = False
        self._completed_calls = []
        
        try:
            # 创建工具调用并加入队列
            for request in requests:
                tool_call = self._create_tool_call(request)
                self._queue.append(tool_call)
            
            # 处理队列
            self._process_queue()
            
        finally:
            self._is_scheduling = False
        
        # 通知完成
        if self._on_all_complete and self._completed_calls:
            self._on_all_complete(self._completed_calls)
        
        return self._completed_calls
    
    def _create_tool_call(self, request: ToolCallRequest) -> ToolCall:
        """创建工具调用"""
        tool = self._registry.get_tool(request.name)
        
        if not tool:
            # 工具未找到
            suggestion = self._registry.suggest_tool(request.name)
            error_message = f"工具 '{request.name}' 未在注册表中找到。{suggestion}"
            
            return ToolCall(
                request=request,
                status=ToolCallStatus.ERROR,
                error_message=error_message,
                response=ToolCallResponse(
                    call_id=request.call_id,
                    result=ToolResult.failure(error_message, "TOOL_NOT_REGISTERED"),
                    error=error_message,
                ),
            )
        
        # 预处理 + 验证参数
        try:
            prepared = tool.prepare_params(request.args)
            return ToolCall(
                request=request,
                status=ToolCallStatus.VALIDATING,
                tool=tool,
                prepared_params=prepared,
                start_time=time.time(),
            )
        except ValueError as e:
            error_message = f"参数验证失败: {str(e)}"
            return ToolCall(
                request=request,
                status=ToolCallStatus.ERROR,
                tool=tool,
                error_message=error_message,
                response=ToolCallResponse(
                    call_id=request.call_id,
                    result=ToolResult.failure(error_message, "INVALID_PARAMS"),
                    error=error_message,
                ),
            )
        except Exception as e:
            error_message = f"参数预处理异常: {str(e)}"
            return ToolCall(
                request=request,
                status=ToolCallStatus.ERROR,
                tool=tool,
                error_message=error_message,
                response=ToolCallResponse(
                    call_id=request.call_id,
                    result=ToolResult.failure(error_message, "VALIDATION_EXCEPTION"),
                    error=error_message,
                ),
            )
    
    def _process_queue(self) -> None:
        """处理队列中的工具调用"""
        while self._queue and not self._is_cancelled:
            tool_call = self._queue.pop(0)
            self._active_calls = [tool_call]
            self._notify_update()
            
            # 如果已经是错误状态，直接加入完成列表
            if tool_call.status == ToolCallStatus.ERROR:
                self._completed_calls.append(tool_call)
                self._active_calls = []
                self._notify_update()
                continue
            
            # 执行工具
            self._execute_tool_call(tool_call)
            
            # 更新完成列表
            self._completed_calls.append(tool_call)
            self._active_calls = []
            self._notify_update()
    
    def _execute_tool_call(self, tool_call: ToolCall) -> None:
        """执行单个工具调用"""
        if not tool_call.tool:
            return
        
        # 更新状态为调度中
        tool_call.status = ToolCallStatus.SCHEDULED
        self._notify_update()
        
        # 更新状态为执行中
        tool_call.status = ToolCallStatus.EXECUTING
        self._notify_update()
        
        try:
            # 执行工具
            prepared = tool_call.prepared_params or tool_call.request.args
            result = tool_call.tool.invoke(prepared, self._repo_root)
            
            # 计算执行时间
            if tool_call.start_time:
                tool_call.duration_ms = (time.time() - tool_call.start_time) * 1000
            
            # 更新状态
            if result.is_success:
                tool_call.status = ToolCallStatus.SUCCESS
            else:
                tool_call.status = ToolCallStatus.ERROR
                tool_call.error_message = result.error.message if result.error else "未知错误"
            
            tool_call.response = ToolCallResponse(
                call_id=tool_call.request.call_id,
                result=result,
            )
            
        except Exception as e:
            # 执行异常
            tool_call.status = ToolCallStatus.ERROR
            tool_call.error_message = str(e)
            tool_call.response = ToolCallResponse(
                call_id=tool_call.request.call_id,
                result=ToolResult.failure(str(e), "EXECUTION_EXCEPTION"),
                error=str(e),
            )
            
            if tool_call.start_time:
                tool_call.duration_ms = (time.time() - tool_call.start_time) * 1000
            
            logger.error(f"工具执行异常: {tool_call.request.name}: {e}")
    
    def _notify_update(self) -> None:
        """通知状态更新"""
        if self._on_tool_calls_update:
            all_calls = self._completed_calls + self._active_calls + self._queue
            self._on_tool_calls_update(all_calls)
    
    def cancel_all(self) -> None:
        """取消所有工具调用"""
        self._is_cancelled = True
        
        # 取消当前活跃的调用
        for tool_call in self._active_calls:
            if tool_call.status in (
                ToolCallStatus.VALIDATING,
                ToolCallStatus.SCHEDULED,
                ToolCallStatus.EXECUTING,
            ):
                tool_call.status = ToolCallStatus.CANCELLED
                tool_call.error_message = "用户取消操作"
                self._completed_calls.append(tool_call)
        
        self._active_calls = []
        
        # 取消队列中的调用
        for tool_call in self._queue:
            tool_call.status = ToolCallStatus.CANCELLED
            tool_call.error_message = "用户取消操作"
            self._completed_calls.append(tool_call)
        
        self._queue = []
        self._notify_update()
    
    def get_completed_calls(self) -> List[ToolCall]:
        """获取已完成的工具调用"""
        return self._completed_calls.copy()
    
    def get_all_calls(self) -> List[ToolCall]:
        """获取所有工具调用"""
        return self._completed_calls + self._active_calls + self._queue
    
    def generate_call_id(self) -> str:
        """生成工具调用 ID"""
        self._call_counter += 1
        return f"call_{self._call_counter}"
    
    def reset(self) -> None:
        """重置调度器状态"""
        self._active_calls = []
        self._queue = []
        self._completed_calls = []
        self._is_scheduling = False
        self._is_cancelled = False
    
    @property
    def is_running(self) -> bool:
        """是否有工具正在执行"""
        return bool(self._active_calls)
    
    @property
    def registry(self) -> ToolRegistry:
        """获取工具注册表"""
        return self._registry


def execute_tool_simple(
    registry: ToolRegistry,
    tool_name: str,
    arguments: Dict[str, Any],
    repo_root: str,
) -> ToolResult:
    """
    简单的工具执行函数
    
    这是一个便捷方法，用于单次工具执行，不需要完整的调度器。
    
    Args:
        registry: 工具注册表
        tool_name: 工具名称
        arguments: 参数
        repo_root: 仓库根目录
        
    Returns:
        ToolResult: 执行结果
    """
    tool = registry.get_tool(tool_name)
    
    if not tool:
        suggestion = registry.suggest_tool(tool_name)
        return ToolResult.failure(
            f"工具 '{tool_name}' 未找到。{suggestion}",
            "TOOL_NOT_REGISTERED"
        )
    
    return tool.execute(arguments, repo_root)

