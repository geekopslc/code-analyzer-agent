# -*- coding: utf-8 -*-
"""
文件操作工具

包含读取文件和目录的工具实现。
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from ..core.tool_base import (
    BaseDeclarativeTool,
    BaseToolInvocation,
    ToolInvocation,
    ToolKind,
    ToolResult,
    ToolErrorType,
)


def _safe_path(base: str, path: str) -> str:
    """安全路径处理，防止路径越界"""
    p = path if os.path.isabs(path) else os.path.join(base, path)
    p = os.path.realpath(p)
    base = os.path.realpath(base)
    if not p.startswith(base):
        raise ValueError("路径越界：路径不在仓库根目录内")
    return p


# ============== ReadFile Tool ==============

class ReadFileInvocation(BaseToolInvocation[Dict[str, Any]]):
    """读取文件工具调用实例"""
    
    def get_description(self) -> str:
        file_path = self.params.get("file_path", "")
        max_bytes = self.params.get("max_bytes", 500000)
        return f"读取文件: {file_path} (最大 {max_bytes} 字节)"
    
    def execute(self) -> ToolResult:
        file_path = self.params.get("file_path", "")
        max_bytes = int(self.params.get("max_bytes", 500000))
        
        try:
            abs_path = _safe_path(self.repo_root, file_path)
            if not os.path.exists(abs_path):
                return ToolResult.failure(
                    f"文件不存在: {file_path}",
                    ToolErrorType.FILE_NOT_FOUND
                )
            if not os.path.isfile(abs_path):
                return ToolResult.failure(
                    f"不是文件: {file_path}",
                    ToolErrorType.INVALID_PARAMS
                )
            
            with open(abs_path, "rb") as f:
                data = f.read(max_bytes)
            
            content = data.decode("utf-8", errors="ignore")
            return ToolResult.success(content, file_path=file_path)
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except PermissionError:
            return ToolResult.failure(
                f"权限不足: {file_path}",
                ToolErrorType.PERMISSION_DENIED
            )
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class ReadFileTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    读取文件工具
    
    读取指定文件的内容，支持限制最大字节数。
    """
    
    def __init__(self):
        super().__init__(
            name="read_file",
            display_name="ReadFile",
            description=(
                "读取指定文件的内容（最多返回指定字节）。"
                "**重要**：使用此工具读取代码文件后，必须立即分析文件内容，"
                "提取所有函数信息（函数名、行号范围、功能描述），输出规范的 JSON 格式，"
                "然后调用 save_function_analysis 工具将分析结果保存到汇总文件中。"
            ),
            kind=ToolKind.READ,
            parameter_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "max_bytes": {
                        "type": "integer",
                        "default": 500000,
                        "description": "最大读取字节数"
                    },
                },
                "required": ["file_path"],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return ReadFileInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== ReadDirectory Tool ==============

class ReadDirectoryInvocation(BaseToolInvocation[Dict[str, Any]]):
    """读取目录工具调用实例"""
    
    def get_description(self) -> str:
        dir_path = self.params.get("directory_path", ".")
        recursive = self.params.get("recursive", True)
        return f"列出目录: {dir_path} (递归: {recursive})"
    
    def execute(self) -> ToolResult:
        directory_path = self.params.get("directory_path", ".")
        recursive = bool(self.params.get("recursive", True))
        max_files = int(self.params.get("max_files", 1000))
        
        try:
            abs_dir = _safe_path(self.repo_root, directory_path)
            if not os.path.exists(abs_dir):
                return ToolResult.failure(
                    f"目录不存在: {directory_path}",
                    ToolErrorType.FILE_NOT_FOUND
                )
            if not os.path.isdir(abs_dir):
                return ToolResult.failure(
                    f"不是目录: {directory_path}",
                    ToolErrorType.INVALID_PARAMS
                )
            
            out = []
            if recursive:
                for root, dirs, files in os.walk(abs_dir):
                    for d in dirs:
                        out.append(os.path.relpath(os.path.join(root, d), self.repo_root))
                        if len(out) >= max_files:
                            break
                    for f in files:
                        out.append(os.path.relpath(os.path.join(root, f), self.repo_root))
                        if len(out) >= max_files:
                            break
                    if len(out) >= max_files:
                        break
            else:
                for name in os.listdir(abs_dir):
                    out.append(os.path.relpath(os.path.join(abs_dir, name), self.repo_root))
                    if len(out) >= max_files:
                        break
            
            # 写入目录缓存
            try:
                mem_dir = os.path.join(self.repo_root, "memory")
                os.makedirs(mem_dir, exist_ok=True)
                cache_path = os.path.join(mem_dir, "dir_cache.json")
                payload = {
                    "directory_path": directory_path,
                    "recursive": recursive,
                    "max_files": max_files,
                    "items": out[:max_files],
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                }
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            
            content = json.dumps(out[:max_files], ensure_ascii=False)
            return ToolResult.success(content, directory_path=directory_path)
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class ReadDirectoryTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    读取目录工具
    
    列出目录结构，支持递归。
    """
    
    def __init__(self):
        super().__init__(
            name="read_directory",
            display_name="ReadDirectory",
            description="列出目录结构（可递归），供探索用",
            kind=ToolKind.READ,
            parameter_schema={
                "type": "object",
                "properties": {
                    "directory_path": {
                        "type": "string",
                        "default": ".",
                        "description": "目录路径"
                    },
                    "recursive": {
                        "type": "boolean",
                        "default": True,
                        "description": "是否递归"
                    },
                    "max_files": {
                        "type": "integer",
                        "default": 1000,
                        "description": "最大文件数"
                    },
                },
                "required": [],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return ReadDirectoryInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )

