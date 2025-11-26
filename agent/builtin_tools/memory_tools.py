# -*- coding: utf-8 -*-
"""
记忆/存储工具

包含保存记忆和函数分析结果的工具。
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

from ..core.tool_base import (
    BaseDeclarativeTool,
    BaseToolInvocation,
    ToolInvocation,
    ToolKind,
    ToolResult,
    ToolErrorType,
)

logger = logging.getLogger("agent.tools.memory")


def _safe_path(base: str, path: str) -> str:
    """安全路径处理，防止路径越界"""
    p = path if os.path.isabs(path) else os.path.join(base, path)
    p = os.path.realpath(p)
    base = os.path.realpath(base)
    if not p.startswith(base):
        raise ValueError("路径越界：路径不在仓库根目录内")
    return p


# ============== SaveMemory Tool ==============

class SaveMemoryInvocation(BaseToolInvocation[Dict[str, Any]]):
    """保存记忆工具调用实例"""
    
    def get_description(self) -> str:
        file_path = self.params.get("file_path", "")
        func_count = len(self.params.get("functions", []))
        return f"保存记忆到 {file_path} (函数数: {func_count})"
    
    def execute(self) -> ToolResult:
        file_path = self.params.get("file_path", "")
        content = self.params.get("content", "")
        functions = self.params.get("functions", [])
        
        if not file_path:
            return ToolResult.failure("未指定文件路径", ToolErrorType.INVALID_PARAMS)
        if not content:
            return ToolResult.failure("未提供要记录的内容", ToolErrorType.INVALID_PARAMS)
        
        try:
            abs_path = _safe_path(self.repo_root, file_path)
            
            # 确保目录存在
            file_dir = os.path.dirname(abs_path)
            if file_dir and not os.path.exists(file_dir):
                os.makedirs(file_dir, exist_ok=True)
            
            # 格式化时间戳
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 构建要写入的内容
            lines = []
            lines.append("---")
            lines.append(f"时间: {timestamp}")
            lines.append("---")
            lines.append("")
            lines.append("## 心得与理解")
            lines.append("")
            lines.append(content)
            lines.append("")
            
            # 如果有函数信息，添加函数列表
            func_count = 0
            if functions and isinstance(functions, list):
                lines.append("## 相关函数")
                lines.append("")
                for func in functions:
                    if isinstance(func, dict):
                        file = func.get("file", "")
                        func_name = func.get("function_name", "")
                        line_range = func.get("line_range", "")
                        if file and func_name and line_range:
                            func_count += 1
                            lines.append(f"- **{func_name}**")
                            lines.append(f"  - 文件: `{file}`")
                            lines.append(f"  - 行号范围: `{line_range}`")
                            lines.append("")
            
            lines.append("---")
            lines.append("")
            
            # 追加写入文件
            content_to_write = "\n".join(lines)
            with open(abs_path, "a", encoding="utf-8") as f:
                f.write(content_to_write)
            
            rel_path = os.path.relpath(abs_path, self.repo_root)
            return ToolResult.success(
                f"成功将记忆保存到 {rel_path}（共记录 {func_count} 个函数）",
                file_path=rel_path,
                func_count=func_count
            )
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except Exception as e:
            logger.error(f"保存记忆失败: {str(e)}")
            return ToolResult.failure(f"保存记忆失败 - {str(e)}", ToolErrorType.EXECUTION_FAILED)


class SaveMemoryTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    保存记忆工具
    
    将阅读代码库过程中的心得、理解、结论记录到 md 记忆文件中。
    """
    
    def __init__(self):
        super().__init__(
            name="save_memory",
            display_name="SaveMemory",
            description=(
                "将阅读代码库过程中的心得、理解、结论记录到 md 记忆文件中。"
                "重点：需要记录每个函数对应的行号范围。"
            ),
            kind=ToolKind.MEMORY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "md 记忆文件的路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "要记录的心得、理解、结论等内容"
                    },
                    "functions": {
                        "type": "array",
                        "description": "函数信息列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "function_name": {"type": "string"},
                                "line_range": {"type": "string"},
                            },
                            "required": ["file", "function_name", "line_range"],
                        },
                    },
                },
                "required": ["file_path", "content"],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return SaveMemoryInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )


# ============== SaveFunctionAnalysis Tool ==============

class SaveFunctionAnalysisInvocation(BaseToolInvocation[Dict[str, Any]]):
    """保存函数分析结果调用实例"""
    
    def get_description(self) -> str:
        functions = self.params.get("functions") or self.params.get("function", [])
        if isinstance(functions, dict):
            functions = [functions]
        count = len(functions) if isinstance(functions, list) else 0
        return f"保存函数分析结果 ({count} 个函数)"
    
    def execute(self) -> ToolResult:
        file_path = self.params.get("file_path", "memory/function_analysis.json")
        # 兼容 function 和 functions 两种参数名
        functions = self.params.get("function") if "function" in self.params else self.params.get("functions", [])
        
        # 如果 functions 是字符串，尝试解析为 JSON
        if isinstance(functions, str):
            try:
                functions = json.loads(functions)
            except json.JSONDecodeError as e:
                return ToolResult.failure(
                    f"function/functions 参数是无效的 JSON 字符串: {str(e)}",
                    ToolErrorType.INVALID_PARAMS
                )
        
        # 如果解析后是单个字典对象，包装成列表
        if isinstance(functions, dict):
            functions = [functions]
        
        # 验证是否为列表
        if not functions or not isinstance(functions, list):
            return ToolResult.failure(
                f"未提供函数分析结果或格式不正确，当前类型: {type(functions).__name__}",
                ToolErrorType.INVALID_PARAMS
            )
        
        try:
            abs_path = _safe_path(self.repo_root, file_path)
            
            # 确保目录存在
            file_dir = os.path.dirname(abs_path)
            if file_dir and not os.path.exists(file_dir):
                os.makedirs(file_dir, exist_ok=True)
            
            # 读取现有数据
            existing_functions = []
            if os.path.exists(abs_path):
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict) and "functions" in data:
                            existing_functions = data.get("functions", [])
                        elif isinstance(data, list):
                            existing_functions = data
                except Exception as e:
                    logger.warning(f"读取现有函数分析文件失败，将创建新文件: {str(e)}")
                    existing_functions = []
            
            # 验证新函数数据的格式
            valid_functions = []
            for func in functions:
                if isinstance(func, dict):
                    file = func.get("file", "")
                    func_name = func.get("function_name", "")
                    line_range = func.get("line_range", "")
                    description = func.get("description", "")
                    if file and func_name and line_range and description:
                        valid_functions.append({
                            "file": file,
                            "function_name": func_name,
                            "line_range": line_range,
                            "description": description
                        })
            
            # 合并函数列表（去重）
            all_functions = existing_functions.copy()
            existing_keys = set()
            for func in existing_functions:
                if isinstance(func, dict):
                    key = (func.get("file", ""), func.get("function_name", ""), func.get("line_range", ""))
                    existing_keys.add(key)
            
            added_count = 0
            for func in valid_functions:
                key = (func["file"], func["function_name"], func["line_range"])
                if key not in existing_keys:
                    all_functions.append(func)
                    existing_keys.add(key)
                    added_count += 1
            
            # 保存到 JSON 文件
            output_data = {
                "functions": all_functions,
                "updated_at": datetime.now().isoformat() + "Z"
            }
            
            with open(abs_path, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)
            
            rel_path = os.path.relpath(abs_path, self.repo_root)
            return ToolResult.success(
                f"成功保存函数分析结果到 {rel_path}（新增 {added_count} 个函数，总计 {len(all_functions)} 个函数）",
                file_path=rel_path,
                added_count=added_count,
                total_count=len(all_functions)
            )
            
        except ValueError as e:
            return ToolResult.failure(str(e), ToolErrorType.PATH_ESCAPE)
        except Exception as e:
            logger.error(f"保存函数分析结果失败: {str(e)}")
            return ToolResult.failure(f"保存函数分析结果失败 - {str(e)}", ToolErrorType.EXECUTION_FAILED)


class SaveFunctionAnalysisTool(BaseDeclarativeTool[Dict[str, Any]]):
    """
    保存函数分析结果工具
    
    将函数分析结果保存到 JSON 汇总文件。
    """
    
    def __init__(self):
        super().__init__(
            name="save_function_analysis",
            display_name="SaveFunctionAnalysis",
            description=(
                "保存函数分析结果到 JSON 汇总文件。"
                "**重要**：在使用 read_file 读取代码文件后，必须立即分析文件内容，"
                "提取所有函数信息，然后调用此工具保存。"
            ),
            kind=ToolKind.MEMORY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "汇总 JSON 文件的路径",
                        "default": "memory/function_analysis.json"
                    },
                    "functions": {
                        "type": "array",
                        "description": "函数分析结果列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "function_name": {"type": "string"},
                                "line_range": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["file", "function_name", "line_range", "description"],
                        },
                    },
                },
                "required": ["functions"],
            },
        )
    
    def create_invocation(
        self,
        params: Dict[str, Any],
        repo_root: str,
    ) -> ToolInvocation[Dict[str, Any]]:
        return SaveFunctionAnalysisInvocation(
            params=params,
            tool_name=self.name,
            tool_display_name=self.display_name,
            repo_root=repo_root,
        )

