# -*- coding: utf-8 -*-
"""
搜索工具

包含 grep、glob、find 等搜索工具的实现。
"""

import fnmatch
import glob as _glob
import json
import os
import re
from typing import Any, Dict, List

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


# ============== Grep Tool ==============

class GrepInvocation(BaseToolInvocation[Dict[str, Any]]):
    """Grep 工具调用实例"""
    
    def get_description(self) -> str:
        pattern = self.params.get("pattern", "")
        directory = self.params.get("directory", ".")
        return f"搜索模式 '{pattern}' 在 {directory}"
    
    def execute(self) -> ToolResult:
        pattern = self.params.get("pattern", "")
        directory = self.params.get("directory", ".")
        max_results = int(self.params.get("max_results", 2000))
        
        try:
            abs_dir = _safe_path(self.repo_root, directory)
            rx = re.compile(pattern)
            results: List[str] = []
            
            for root, _, files in os.walk(abs_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "rb") as f:
                            data = f.read(1024 * 1024)
                        text = data.decode("utf-8", errors="ignore")
                        for i, line in enumerate(text.splitlines(), start=1):
                            if rx.search(line):
                                rel = os.path.relpath(fpath, self.repo_root)
                                results.append(f"{rel}:{i}:{line}")
                                if len(results) >= max_results:
                                    content = "\n".join(results)
                                    return ToolResult.success(
                                        content,
                                        match_count=len(results),
                                        truncated=True
                                    )
                    except Exception:
                        continue
            
            content = "\n".join(results)
            return ToolResult.success(content, match_count=len(results))
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except re.error as e:
            return ToolResult.failure(f"无效的正则表达式: {e}", ToolErrorType.INVALID_PARAMS)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class GrepTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    Grep 搜索工具
    
    在目录中文本文件内执行正则搜索。
    """
    
    def __init__(self):
        super().__init__(
            name="grep",
            display_name="Grep",
            description="在目录中文本文件内执行正则搜索（大小写敏感）",
            kind=ToolKind.SEARCH,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式模式"
                    },
                    "directory": {
                        "type": "string",
                        "default": ".",
                        "description": "搜索目录"
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 2000,
                        "description": "最大结果数"
                    },
                },
                "required": ["pattern"],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return GrepInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== Glob Tool ==============

class GlobInvocation(BaseToolInvocation[Dict[str, Any]]):
    """Glob 工具调用实例"""
    
    def get_description(self) -> str:
        pattern = self.params.get("pattern", "*")
        directory = self.params.get("directory", ".")
        return f"匹配模式 '{pattern}' 在 {directory}"
    
    def execute(self) -> ToolResult:
        pattern = self.params.get("pattern", "*")
        directory = self.params.get("directory", ".")
        max_results = int(self.params.get("max_results", 2000))
        
        try:
            abs_dir = _safe_path(self.repo_root, directory)
            matches = _glob.glob(os.path.join(abs_dir, pattern), recursive=True)
            rels = [os.path.relpath(m, self.repo_root) for m in matches][:max_results]
            content = json.dumps(rels, ensure_ascii=False)
            return ToolResult.success(content, match_count=len(rels))
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class GlobTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    Glob 匹配工具
    
    使用通配符匹配文件。
    """
    
    def __init__(self):
        super().__init__(
            name="glob",
            display_name="Glob",
            description="使用通配符匹配文件",
            kind=ToolKind.SEARCH,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "通配符模式"
                    },
                    "directory": {
                        "type": "string",
                        "default": ".",
                        "description": "搜索目录"
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 2000,
                        "description": "最大结果数"
                    },
                },
                "required": ["pattern"],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return GlobInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== Find Tool ==============

class FindInvocation(BaseToolInvocation[Dict[str, Any]]):
    """Find 工具调用实例"""
    
    def get_description(self) -> str:
        name_pattern = self.params.get("name_pattern", "*")
        directory = self.params.get("directory", ".")
        type_ = self.params.get("type", "f")
        return f"查找 '{name_pattern}' 在 {directory} (类型: {type_})"
    
    def execute(self) -> ToolResult:
        name_pattern = self.params.get("name_pattern", "*")
        directory = self.params.get("directory", ".")
        max_results = int(self.params.get("max_results", 2000))
        type_ = self.params.get("type", "f")
        
        try:
            abs_dir = _safe_path(self.repo_root, directory)
            find_results: List[str] = []
            
            for root, dirs, files in os.walk(abs_dir):
                if type_ in ("a", "d"):
                    for d in dirs:
                        if fnmatch.fnmatch(d, name_pattern):
                            find_results.append(
                                os.path.relpath(os.path.join(root, d), self.repo_root)
                            )
                            if len(find_results) >= max_results:
                                content = json.dumps(find_results, ensure_ascii=False)
                                return ToolResult.success(content, match_count=len(find_results))
                
                if type_ in ("a", "f"):
                    for f in files:
                        if fnmatch.fnmatch(f, name_pattern):
                            find_results.append(
                                os.path.relpath(os.path.join(root, f), self.repo_root)
                            )
                            if len(find_results) >= max_results:
                                content = json.dumps(find_results, ensure_ascii=False)
                                return ToolResult.success(content, match_count=len(find_results))
            
            content = json.dumps(find_results[:max_results], ensure_ascii=False)
            return ToolResult.success(content, match_count=len(find_results))
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except Exception as e:
            return ToolResult.failure(str(e), ToolErrorType.EXECUTION_FAILED)


class FindTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    Find 查找工具
    
    按名称模式查找文件/目录。
    """
    
    def __init__(self):
        super().__init__(
            name="find",
            display_name="Find",
            description="按名称模式查找文件/目录（纯 Python 实现）",
            kind=ToolKind.SEARCH,
            parameter_schema={
                "type": "object",
                "properties": {
                    "name_pattern": {
                        "type": "string",
                        "default": "*",
                        "description": "名称模式"
                    },
                    "directory": {
                        "type": "string",
                        "default": ".",
                        "description": "搜索目录"
                    },
                    "type": {
                        "type": "string",
                        "enum": ["f", "d", "a"],
                        "default": "f",
                        "description": "类型：f=文件，d=目录，a=全部"
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 2000,
                        "description": "最大结果数"
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
        return FindInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )

