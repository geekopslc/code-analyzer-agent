# -*- coding: utf-8 -*-
"""
Shell 命令执行工具

提供安全的 bash 命令执行能力。
"""

import subprocess
from typing import Any, Dict, List

from ..core.tool_base import (
    BaseDeclarativeTool,
    BaseToolInvocation,
    ToolInvocation,
    ToolKind,
    ToolResult,
    ToolErrorType,
)


# 禁止的命令片段
BLOCKED_COMMANDS = [
    " rm ", "rm -",
    "curl ", "wget ",
    "sudo ",
    "apt ", "yum ",
    "pip ", "npm ",
    "docker ", "kubectl ",
]


class BashInvocation(BaseToolInvocation[Dict[str, Any]]):
    """Bash 命令执行调用实例"""
    
    def get_description(self) -> str:
        command = self.params.get("command", "")
        return f"执行命令: {command[:100]}{'...' if len(command) > 100 else ''}"
    
    def should_confirm(self) -> bool:
        """Shell 命令通常需要确认"""
        # 在当前实现中，默认不需要确认（保持原有行为）
        # 但可以在此添加确认逻辑
        return False
    
    def execute(self) -> ToolResult:
        command = self.params.get("command", "")
        timeout = int(self.params.get("timeout", 25))
        
        # 检查危险命令
        cmd_l = f" {command} ".lower()
        for bad in BLOCKED_COMMANDS:
            if bad.strip() in cmd_l:
                return ToolResult.failure(
                    "命令包含被禁止的片段",
                    ToolErrorType.COMMAND_BLOCKED
                )
        
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            
            output = (proc.stdout + "\n" + proc.stderr).strip()
            
            if proc.returncode != 0:
                return ToolResult.success(
                    output,
                    display=f"命令退出码: {proc.returncode}",
                    exit_code=proc.returncode
                )
            
            return ToolResult.success(output, exit_code=0)
            
        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                f"命令执行超时 ({timeout}s)",
                ToolErrorType.TIMEOUT
            )
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class BashTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    Bash 命令执行工具
    
    在仓库根目录下执行安全的 bash 命令。
    禁止执行破坏性和网络命令。
    """
    
    def __init__(self):
        super().__init__(
            name="bash",
            display_name="Shell",
            description="在仓库根目录下执行安全的 bash 命令（禁止破坏性与网络命令）",
            kind=ToolKind.EXECUTE,
            parameter_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 bash 命令"
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 15,
                        "description": "超时时间（秒）"
                    },
                },
                "required": ["command"],
            },
        )
    
    def validate_params(self, params: Dict[str, Any]) -> str:
        """额外的参数验证"""
        error = super().validate_params(params)
        if error:
            return error
        
        command = params.get("command", "")
        if not command.strip():
            return "命令不能为空"
        
        # 检查危险命令
        cmd_l = f" {command} ".lower()
        for bad in BLOCKED_COMMANDS:
            if bad.strip() in cmd_l:
                return f"命令包含被禁止的片段: {bad.strip()}"
        
        return None
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return BashInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )

