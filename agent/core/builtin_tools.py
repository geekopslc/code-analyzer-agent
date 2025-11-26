"""
内置工具实现

参考 gemini-cli 的工具实现模式，将现有工具重构为声明式工具。
每个工具由两部分组成：
1. XxxTool: 工具定义类（继承 BaseDeclarativeTool）
2. XxxToolInvocation: 工具调用类（继承 BaseToolInvocation）

设计优势：
- 参数验证与执行逻辑分离
- 工具可独立测试
- 支持统一的调度和确认机制
"""

import fnmatch
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

from .tool_base import (
    BaseDeclarativeTool,
    BaseToolInvocation,
    DeclarativeTool,
    ToolInvocation,
    ToolKind,
    ToolLocation,
    ToolResult,
)

logger = logging.getLogger("agent.core.builtin_tools")


# ============================================================================
# 辅助函数
# ============================================================================

def _safe_path(base: str, path: str) -> str:
    """确保路径在仓库根目录内"""
    p = path if os.path.isabs(path) else os.path.join(base, path)
    p = os.path.realpath(p)
    base = os.path.realpath(base)
    if not p.startswith(base):
        raise ValueError("路径超出仓库根目录范围")
    return p


# ============================================================================
# ReadFile 工具
# ============================================================================

class ReadFileParams(Dict[str, Any]):
    """ReadFile 工具参数"""
    pass


class ReadFileInvocation(BaseToolInvocation[ReadFileParams]):
    """ReadFile 工具调用"""
    
    def get_description(self) -> str:
        file_path = self.params.get("file_path", "")
        max_bytes = self.params.get("max_bytes", 500000)
        return f"读取文件: {file_path} (最大 {max_bytes} 字节)"
    
    def get_locations(self) -> List[ToolLocation]:
        file_path = self.params.get("file_path", "")
        return [ToolLocation(path=file_path)]
    
    def execute(self, repo_root: str) -> ToolResult:
        file_path = self.params.get("file_path", "")
        max_bytes = int(self.params.get("max_bytes", 500000))
        
        try:
            abs_path = _safe_path(repo_root, file_path)
            if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
                return ToolResult.failure(f"文件不存在: {file_path}", "FILE_NOT_FOUND")
            
            with open(abs_path, "rb") as f:
                data = f.read(max_bytes)
            
            content = data.decode("utf-8", errors="ignore")
            return ToolResult.success(content)
            
        except ValueError as e:
            return ToolResult.failure(str(e), "PATH_ESCAPE")
        except Exception as e:
            return ToolResult.failure(str(e), "READ_ERROR")


class ReadFileTool(BaseDeclarativeTool[ReadFileParams]):
    """
    读取文件工具
    
    读取指定文件的内容（最多返回指定字节）。
    """
    
    NAME = "read_file"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="读取文件",
            description="读取指定文件的内容（最多返回指定字节）。使用此工具读取代码文件后，必须立即分析文件内容，提取所有函数信息。",
            kind=ToolKind.READ,
            parameter_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "max_bytes": {"type": "integer", "default": 500000, "description": "最大读取字节数"},
                },
                "required": ["file_path"],
            },
        )
    
    def validate_params(self, params: ReadFileParams) -> Optional[str]:
        error = super().validate_params(params)
        if error:
            return error
        
        file_path = params.get("file_path", "").strip()
        if not file_path:
            return "file_path 不能为空"
        
        return None
    
    def create_invocation(self, params: ReadFileParams) -> ToolInvocation[ReadFileParams]:
        return ReadFileInvocation(params, self.name, self.display_name)


# ============================================================================
# ReadDirectory 工具
# ============================================================================

class ReadDirectoryParams(Dict[str, Any]):
    """ReadDirectory 工具参数"""
    pass


class ReadDirectoryInvocation(BaseToolInvocation[ReadDirectoryParams]):
    """ReadDirectory 工具调用"""
    
    def get_description(self) -> str:
        directory_path = self.params.get("directory_path", ".")
        recursive = self.params.get("recursive", True)
        return f"列出目录: {directory_path} (递归: {recursive})"
    
    def execute(self, repo_root: str) -> ToolResult:
        directory_path = self.params.get("directory_path", ".")
        recursive = bool(self.params.get("recursive", True))
        max_files = int(self.params.get("max_files", 1000))
        
        try:
            abs_dir = _safe_path(repo_root, directory_path)
            if not os.path.exists(abs_dir) or not os.path.isdir(abs_dir):
                return ToolResult.failure(f"目录不存在: {directory_path}", "DIR_NOT_FOUND")
            
            out: List[str] = []
            if recursive:
                for root, dirs, files in os.walk(abs_dir):
                    for d in dirs:
                        out.append(os.path.relpath(os.path.join(root, d), repo_root))
                        if len(out) >= max_files:
                            break
                    for f in files:
                        out.append(os.path.relpath(os.path.join(root, f), repo_root))
                        if len(out) >= max_files:
                            break
                    if len(out) >= max_files:
                        break
            else:
                for name in os.listdir(abs_dir):
                    out.append(os.path.relpath(os.path.join(abs_dir, name), repo_root))
                    if len(out) >= max_files:
                        break
            
            # 写入目录缓存
            try:
                mem_dir = os.path.join(repo_root, "memory")
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
            
            return ToolResult.success(json.dumps(out[:max_files], ensure_ascii=False))
            
        except ValueError as e:
            return ToolResult.failure(str(e), "PATH_ESCAPE")
        except Exception as e:
            return ToolResult.failure(str(e), "LIST_ERROR")


class ReadDirectoryTool(BaseDeclarativeTool[ReadDirectoryParams]):
    """
    列出目录结构工具
    """
    
    NAME = "read_directory"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="列出目录",
            description="列出目录结构（可递归），供探索用",
            kind=ToolKind.READ,
            parameter_schema={
                "type": "object",
                "properties": {
                    "directory_path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": True},
                    "max_files": {"type": "integer", "default": 1000},
                },
                "required": [],
            },
        )
    
    def create_invocation(self, params: ReadDirectoryParams) -> ToolInvocation[ReadDirectoryParams]:
        return ReadDirectoryInvocation(params, self.name, self.display_name)


# ============================================================================
# Grep 工具
# ============================================================================

class GrepParams(Dict[str, Any]):
    """Grep 工具参数"""
    pass


class GrepInvocation(BaseToolInvocation[GrepParams]):
    """Grep 工具调用"""
    
    def get_description(self) -> str:
        pattern = self.params.get("pattern", "")
        directory = self.params.get("directory", ".")
        return f"搜索模式: '{pattern}' in {directory}"
    
    def execute(self, repo_root: str) -> ToolResult:
        pattern = self.params.get("pattern", "")
        directory = self.params.get("directory", ".")
        max_results = int(self.params.get("max_results", 2000))
        
        try:
            abs_dir = _safe_path(repo_root, directory)
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
                                rel = os.path.relpath(fpath, repo_root)
                                results.append(f"{rel}:{i}:{line}")
                                if len(results) >= max_results:
                                    return ToolResult.success("\n".join(results))
                    except Exception:
                        continue
            
            return ToolResult.success("\n".join(results))
            
        except re.error as e:
            return ToolResult.failure(f"正则表达式错误: {e}", "REGEX_ERROR")
        except Exception as e:
            return ToolResult.failure(str(e), "GREP_ERROR")


class GrepTool(BaseDeclarativeTool[GrepParams]):
    """
    正则搜索工具
    """
    
    NAME = "grep"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="正则搜索",
            description="在目录中文本文件内执行正则搜索（大小写敏感）",
            kind=ToolKind.SEARCH,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式模式"},
                    "directory": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 2000},
                },
                "required": ["pattern"],
            },
        )
    
    def validate_params(self, params: GrepParams) -> Optional[str]:
        error = super().validate_params(params)
        if error:
            return error
        
        pattern = params.get("pattern", "").strip()
        if not pattern:
            return "pattern 不能为空"
        
        # 验证正则表达式
        try:
            re.compile(pattern)
        except re.error as e:
            return f"无效的正则表达式: {e}"
        
        return None
    
    def create_invocation(self, params: GrepParams) -> ToolInvocation[GrepParams]:
        return GrepInvocation(params, self.name, self.display_name)


# ============================================================================
# Glob 工具
# ============================================================================

class GlobParams(Dict[str, Any]):
    """Glob 工具参数"""
    pass


class GlobInvocation(BaseToolInvocation[GlobParams]):
    """Glob 工具调用"""
    
    def get_description(self) -> str:
        pattern = self.params.get("pattern", "*")
        directory = self.params.get("directory", ".")
        return f"匹配模式: '{pattern}' in {directory}"
    
    def execute(self, repo_root: str) -> ToolResult:
        import glob as _glob
        
        pattern = self.params.get("pattern", "*")
        directory = self.params.get("directory", ".")
        max_results = int(self.params.get("max_results", 2000))
        
        try:
            abs_dir = _safe_path(repo_root, directory)
            matches = _glob.glob(os.path.join(abs_dir, pattern), recursive=True)
            rels = [os.path.relpath(m, repo_root) for m in matches][:max_results]
            return ToolResult.success(json.dumps(rels, ensure_ascii=False))
            
        except Exception as e:
            return ToolResult.failure(str(e), "GLOB_ERROR")


class GlobTool(BaseDeclarativeTool[GlobParams]):
    """
    通配符匹配工具
    """
    
    NAME = "glob"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="通配符匹配",
            description="使用通配符匹配文件",
            kind=ToolKind.SEARCH,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "通配符模式"},
                    "directory": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 2000},
                },
                "required": ["pattern"],
            },
        )
    
    def create_invocation(self, params: GlobParams) -> ToolInvocation[GlobParams]:
        return GlobInvocation(params, self.name, self.display_name)


# ============================================================================
# Find 工具
# ============================================================================

class FindParams(Dict[str, Any]):
    """Find 工具参数"""
    pass


class FindInvocation(BaseToolInvocation[FindParams]):
    """Find 工具调用"""
    
    def get_description(self) -> str:
        name_pattern = self.params.get("name_pattern", "*")
        directory = self.params.get("directory", ".")
        type_ = self.params.get("type", "f")
        return f"查找: '{name_pattern}' (类型: {type_}) in {directory}"
    
    def execute(self, repo_root: str) -> ToolResult:
        name_pattern = self.params.get("name_pattern", "*")
        directory = self.params.get("directory", ".")
        max_results = int(self.params.get("max_results", 2000))
        type_ = self.params.get("type", "f")
        
        try:
            abs_dir = _safe_path(repo_root, directory)
            find_results: List[str] = []
            
            for root, dirs, files in os.walk(abs_dir):
                if type_ in ("a", "d"):
                    for d in dirs:
                        if fnmatch.fnmatch(d, name_pattern):
                            find_results.append(os.path.relpath(os.path.join(root, d), repo_root))
                            if len(find_results) >= max_results:
                                return ToolResult.success(json.dumps(find_results, ensure_ascii=False))
                if type_ in ("a", "f"):
                    for f in files:
                        if fnmatch.fnmatch(f, name_pattern):
                            find_results.append(os.path.relpath(os.path.join(root, f), repo_root))
                            if len(find_results) >= max_results:
                                return ToolResult.success(json.dumps(find_results, ensure_ascii=False))
            
            return ToolResult.success(json.dumps(find_results[:max_results], ensure_ascii=False))
            
        except Exception as e:
            return ToolResult.failure(str(e), "FIND_ERROR")


class FindTool(BaseDeclarativeTool[FindParams]):
    """
    按名称模式查找文件/目录
    """
    
    NAME = "find"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="查找文件",
            description="按名称模式查找文件/目录（纯 Python 实现）",
            kind=ToolKind.SEARCH,
            parameter_schema={
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "default": "*"},
                    "directory": {"type": "string", "default": "."},
                    "type": {"type": "string", "enum": ["f", "d", "a"], "default": "f"},
                    "max_results": {"type": "integer", "default": 2000},
                },
                "required": [],
            },
        )
    
    def create_invocation(self, params: FindParams) -> ToolInvocation[FindParams]:
        return FindInvocation(params, self.name, self.display_name)


# ============================================================================
# Bash 工具
# ============================================================================

class BashParams(Dict[str, Any]):
    """Bash 工具参数"""
    pass


class BashInvocation(BaseToolInvocation[BashParams]):
    """Bash 工具调用"""
    
    # 禁止的命令片段
    DENY_LIST = [" rm ", "rm -", "curl ", "wget ", "sudo ", "apt ", "yum ", "pip ", "npm ", "docker ", "kubectl "]
    
    def get_description(self) -> str:
        command = self.params.get("command", "")
        return f"执行命令: {command[:100]}{'...' if len(command) > 100 else ''}"
    
    def should_confirm(self) -> bool:
        """Bash 命令需要确认"""
        return True
    
    def execute(self, repo_root: str) -> ToolResult:
        command = self.params.get("command", "")
        timeout = int(self.params.get("timeout", 25))
        
        # 检查危险命令
        cmd_l = f" {command} ".lower()
        for bad in self.DENY_LIST:
            if bad.strip() in cmd_l:
                return ToolResult.failure("命令包含被禁止的片段", "FORBIDDEN_COMMAND")
        
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()
            return ToolResult.success(output)
            
        except subprocess.TimeoutExpired:
            return ToolResult.failure("命令执行超时", "TIMEOUT")
        except Exception as e:
            return ToolResult.failure(str(e), "BASH_ERROR")


class BashTool(BaseDeclarativeTool[BashParams]):
    """
    执行 Bash 命令工具
    """
    
    NAME = "bash"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="执行命令",
            description="在仓库根目录下执行安全的 bash 命令（禁止破坏性与网络命令）",
            kind=ToolKind.EXECUTE,
            parameter_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {"type": "integer", "default": 15, "description": "超时时间（秒）"},
                },
                "required": ["command"],
            },
        )
    
    def validate_params(self, params: BashParams) -> Optional[str]:
        error = super().validate_params(params)
        if error:
            return error
        
        command = params.get("command", "").strip()
        if not command:
            return "command 不能为空"
        
        return None
    
    def create_invocation(self, params: BashParams) -> ToolInvocation[BashParams]:
        return BashInvocation(params, self.name, self.display_name)


# ============================================================================
# SaveMemory 工具
# ============================================================================

class SaveMemoryParams(Dict[str, Any]):
    """SaveMemory 工具参数"""
    pass


class SaveMemoryInvocation(BaseToolInvocation[SaveMemoryParams]):
    """SaveMemory 工具调用"""
    
    def get_description(self) -> str:
        file_path = self.params.get("file_path", "")
        func_count = len(self.params.get("functions", [])) if isinstance(self.params.get("functions"), list) else 0
        return f"保存记忆到: {file_path} ({func_count} 个函数)"
    
    def execute(self, repo_root: str) -> ToolResult:
        file_path = self.params.get("file_path", "")
        content = self.params.get("content", "")
        functions = self.params.get("functions", [])
        
        try:
            abs_path = _safe_path(repo_root, file_path)
            
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
            
            rel_path = os.path.relpath(abs_path, repo_root)
            return ToolResult.success(f"成功将记忆保存到 {rel_path}（共记录 {func_count} 个函数）")
            
        except Exception as e:
            return ToolResult.failure(str(e), "SAVE_ERROR")


class SaveMemoryTool(BaseDeclarativeTool[SaveMemoryParams]):
    """
    保存记忆工具
    """
    
    NAME = "save_memory"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="保存记忆",
            description="将阅读代码库过程中的心得、理解、结论记录到 md 记忆文件中。重点：需要记录每个函数对应的行号范围。",
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
                                "line_range": {"type": "string"}
                            },
                            "required": ["file", "function_name", "line_range"]
                        }
                    }
                },
                "required": ["file_path", "content"],
            },
        )
    
    def validate_params(self, params: SaveMemoryParams) -> Optional[str]:
        error = super().validate_params(params)
        if error:
            return error
        
        file_path = params.get("file_path", "").strip()
        if not file_path:
            return "file_path 不能为空"
        
        content = params.get("content", "").strip()
        if not content:
            return "content 不能为空"
        
        return None
    
    def create_invocation(self, params: SaveMemoryParams) -> ToolInvocation[SaveMemoryParams]:
        return SaveMemoryInvocation(params, self.name, self.display_name)


# ============================================================================
# SaveFunctionAnalysis 工具
# ============================================================================

class SaveFunctionAnalysisParams(Dict[str, Any]):
    """SaveFunctionAnalysis 工具参数"""
    pass


class SaveFunctionAnalysisInvocation(BaseToolInvocation[SaveFunctionAnalysisParams]):
    """SaveFunctionAnalysis 工具调用"""
    
    def get_description(self) -> str:
        functions = self.params.get("functions", [])
        count = len(functions) if isinstance(functions, list) else 0
        return f"保存 {count} 个函数分析结果"
    
    def execute(self, repo_root: str) -> ToolResult:
        file_path = self.params.get("file_path", "memory/function_analysis.json")
        functions = self.params.get("function") if "function" in self.params else self.params.get("functions", [])
        if functions is None:
            functions = []
        
        # 如果 functions 是字符串，尝试解析为 JSON
        if isinstance(functions, str):
            try:
                functions = json.loads(functions)
            except json.JSONDecodeError as e:
                return ToolResult.failure(f"functions 参数是无效的 JSON 字符串: {str(e)}", "INVALID_JSON")
        
        # 如果解析后是单个字典对象，包装成列表
        if isinstance(functions, dict):
            functions = [functions]
        
        # 验证是否为列表；允许空列表表示当前文件无可记录函数
        if not isinstance(functions, list):
            return ToolResult.failure("未提供函数分析结果或格式不正确", "INVALID_PARAMS")
        
        try:
            abs_path = _safe_path(repo_root, file_path)
            
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
                except Exception:
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
            
            rel_path = os.path.relpath(abs_path, repo_root)
            return ToolResult.success(f"成功保存函数分析结果到 {rel_path}（新增 {added_count} 个函数，总计 {len(all_functions)} 个函数）")
            
        except Exception as e:
            return ToolResult.failure(str(e), "SAVE_ERROR")


class SaveFunctionAnalysisTool(BaseDeclarativeTool[SaveFunctionAnalysisParams]):
    """
    保存函数分析结果工具
    """
    
    NAME = "save_function_analysis"
    
    def __init__(self):
        super().__init__(
            name=self.NAME,
            display_name="保存函数分析",
            description="保存函数分析结果到 JSON 汇总文件。在使用 read_file 读取代码文件后，必须立即分析文件内容，提取所有函数信息，然后调用此工具。",
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
                                "description": {"type": "string"}
                            },
                            "required": ["file", "function_name", "line_range", "description"]
                        }
                    }
                },
                "required": ["functions"],
            },
        )
    
    def create_invocation(self, params: SaveFunctionAnalysisParams) -> ToolInvocation[SaveFunctionAnalysisParams]:
        return SaveFunctionAnalysisInvocation(params, self.name, self.display_name)


# ============================================================================
# 工具工厂函数
# ============================================================================

def create_builtin_tools() -> List[DeclarativeTool]:
    """
    创建所有内置工具实例
    
    Returns:
        工具列表
    """
    return [
        ReadFileTool(),
        ReadDirectoryTool(),
        GrepTool(),
        GlobTool(),
        FindTool(),
        BashTool(),
        SaveMemoryTool(),
        SaveFunctionAnalysisTool(),
    ]


def get_builtin_tool_schemas() -> List[Dict[str, Any]]:
    """
    获取所有内置工具的 Schema
    
    Returns:
        工具 Schema 列表
    """
    return [tool.schema for tool in create_builtin_tools()]

