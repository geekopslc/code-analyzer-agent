"""
AST 解析器模块：自动提取和验证函数的完整行号范围
支持 TypeScript/JavaScript、Python、Go 等语言
"""
import ast
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.ast_parser")


def _get_file_extension(file_path: str) -> str:
    """获取文件扩展名"""
    return os.path.splitext(file_path)[1].lower()


def _get_file_total_lines(file_path: str) -> int:
    """获取文件总行数"""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return len(f.readlines())
    except Exception as e:
        logger.warning(f"无法读取文件总行数 {file_path}: {e}")
        return 0


def _parse_line_range(line_range_str: str) -> Optional[Tuple[int, int]]:
    """解析行号范围字符串，返回 (start_line, end_line)"""
    if not line_range_str:
        return None
    
    # 支持格式： "12-90", "12-90", "12:90", "12,90"
    patterns = [
        r'(\d+)\s*[-~]\s*(\d+)',  # "12-90" 或 "12~90"
        r'(\d+)\s*:\s*(\d+)',      # "12:90"
        r'(\d+)\s*,\s*(\d+)',      # "12,90"
    ]
    
    for pattern in patterns:
        match = re.match(pattern, line_range_str.strip())
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
            if start > 0 and end >= start:
                return (start, end)
    
    return None


def _is_in_string_or_comment(line: str, pos: int) -> bool:
    """简单判断位置是否在字符串或注释中（用于大括号匹配）"""
    # 检查单行注释
    if "//" in line[:pos]:
        comment_pos = line.find("//")
        if comment_pos < pos:
            return True
    
    # 检查多行注释开始
    if "/*" in line[:pos]:
        comment_start = line.rfind("/*", 0, pos)
        if comment_start >= 0:
            comment_end = line.find("*/", comment_start)
            if comment_end < 0 or comment_end >= pos:
                return True
    
    # 简单检查字符串（单引号、双引号、模板字符串）
    in_single_quote = False
    in_double_quote = False
    in_template = False
    escape_next = False
    
    for i, char in enumerate(line[:pos]):
        if escape_next:
            escape_next = False
            continue
        
        if char == "\\":
            escape_next = True
            continue
        
        if char == "'" and not in_double_quote and not in_template:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote and not in_template:
            in_double_quote = not in_double_quote
        elif char == "`" and not in_single_quote and not in_double_quote:
            in_template = not in_template
    
    return in_single_quote or in_double_quote or in_template


def _find_function_end_js_ts(content: str, start_line: int, function_name: str) -> Optional[int]:
    """对于 JavaScript/TypeScript，通过大括号匹配找到函数结束行"""
    lines = content.splitlines()
    if start_line < 1 or start_line > len(lines):
        return None
    
    # 从起始行开始查找函数体开始的大括号
    start_idx = start_line - 1
    brace_start_line = None
    brace_start_pos = None
    
    # 找到函数定义行和函数体开始的大括号
    for i in range(start_idx, min(start_idx + 5, len(lines))):  # 通常函数体在定义行或之后几行
        line = lines[i]
        # 查找第一个未闭合的大括号（函数体开始）
        for pos, char in enumerate(line):
            if char == '{' and not _is_in_string_or_comment(line, pos):
                brace_start_line = i
                brace_start_pos = pos
                break
        if brace_start_line is not None:
            break
    
    if brace_start_line is None:
        # 如果没有找到大括号，可能是箭头函数或单行函数，返回定义行
        return start_line
    
    # 从大括号开始行开始，计算大括号层级
    brace_level = 0
    end_line = None
    
    for i in range(brace_start_line, len(lines)):
        line = lines[i]
        start_pos = brace_start_pos if (i == brace_start_line and brace_start_pos is not None) else 0
        
        for pos in range(start_pos, len(line)):
            if _is_in_string_or_comment(line, pos):
                continue
            
            if line[pos] == '{':
                brace_level += 1
            elif line[pos] == '}':
                brace_level -= 1
                if brace_level == 0:
                    end_line = i + 1  # 行号从1开始
                    break
        
        if end_line is not None:
            break
    
    return end_line if end_line is not None else len(lines)


def _find_function_end_python(content: str, start_line: int, function_name: str) -> Optional[int]:
    """对于 Python，通过缩进找到函数结束行"""
    lines = content.splitlines()
    if start_line < 1 or start_line > len(lines):
        return None
    
    start_idx = start_line - 1
    if start_idx >= len(lines):
        return None
    
    # 获取函数定义行的缩进
    def_line = lines[start_idx]
    # 找到 def 关键字后的缩进
    def_match = re.search(r'^\s*(def|class|async\s+def)', def_line)
    if not def_match:
        return start_line
    
    base_indent = len(def_line) - len(def_line.lstrip())
    
    # 查找下一个相同或更小缩进的非空非注释行
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        
        # 跳过空行和注释
        if not stripped or stripped.startswith('#'):
            continue
        
        # 检查缩进
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= base_indent:
            # 找到函数结束（下一个相同或更小缩进的行）
            return i  # 返回前一行（函数结束行）
    
    # 如果没找到，返回文件末尾
    return len(lines)


def _find_function_end_go(content: str, start_line: int, function_name: str) -> Optional[int]:
    """对于 Go，通过大括号匹配找到函数结束行（类似 JS/TS）"""
    return _find_function_end_js_ts(content, start_line, function_name)


def _extract_function_by_name_js_ts(content: str, function_name: str) -> Optional[Tuple[int, int]]:
    """从 JavaScript/TypeScript 代码中提取函数行号范围"""
    lines = content.splitlines()
    
    # 构建函数匹配模式（支持多种函数定义形式）
    # 注意：使用更精确的模式，避免误匹配
    patterns = [
        # function functionName(...) 或 export function functionName(...)
        rf'^\s*(?:export\s+)?(?:async\s+)?function\s+{re.escape(function_name)}\s*\(',
        # const functionName = (...) => {...} 或 const functionName = function(...)
        rf'^\s*(?:export\s+)?(?:const|let|var)\s+{re.escape(function_name)}\s*=\s*(?:async\s+)?(?:function\s*\()?',
        # methodName(...) {...}  (类方法，可能有多行修饰符)
        rf'^\s*(?:public\s+|private\s+|protected\s+|static\s+)?(?:async\s+)?{re.escape(function_name)}\s*\(',
        # @Decorator() methodName(...) {...}  (带装饰器的方法)
        rf'^\s*(?:@\w+[^\n]*\n\s*)*(?:public\s+|private\s+|protected\s+|static\s+)?(?:async\s+)?{re.escape(function_name)}\s*\(',
    ]
    
    start_line = None
    # 先尝试精确匹配（在同一行）
    for i, line in enumerate(lines, 1):
        for pattern in patterns:
            if re.search(pattern, line):
                start_line = i
                break
        if start_line:
            break
    
    # 如果没找到，尝试多行匹配（处理装饰器等情况）
    if start_line is None:
        for i in range(len(lines)):
            # 检查当前行和下一行
            combined = lines[i] + "\n" + (lines[i+1] if i+1 < len(lines) else "")
            for pattern in patterns:
                if re.search(pattern, combined, re.MULTILINE):
                    start_line = i + 1
                    break
            if start_line:
                break
    
    if start_line is None:
        return None
    
    end_line = _find_function_end_js_ts(content, start_line, function_name)
    if end_line is None:
        return None
    
    return (start_line, end_line)


def _extract_function_by_name_python(content: str, function_name: str) -> Optional[Tuple[int, int]]:
    """从 Python 代码中提取函数行号范围"""
    lines = content.splitlines()
    
    # 构建函数匹配模式
    patterns = [
        rf'^\s*(?:async\s+)?def\s+{re.escape(function_name)}\s*\(',
        rf'^\s*class\s+{re.escape(function_name)}\s*[\(:]',
    ]
    
    start_line = None
    for i, line in enumerate(lines, 1):
        for pattern in patterns:
            if re.search(pattern, line):
                start_line = i
                break
        if start_line:
            break
    
    if start_line is None:
        return None
    
    end_line = _find_function_end_python(content, start_line, function_name)
    if end_line is None:
        return None
    
    return (start_line, end_line)


def _extract_function_by_name_go(content: str, function_name: str) -> Optional[Tuple[int, int]]:
    """从 Go 代码中提取函数行号范围"""
    lines = content.splitlines()
    
    # 构建函数匹配模式
    patterns = [
        rf'^\s*func\s+(?:\(\s*\w+\s+\w+\s*\)\s+)?{re.escape(function_name)}\s*\(',
        rf'^\s*func\s+{re.escape(function_name)}\s*\(',
    ]
    
    start_line = None
    for i, line in enumerate(lines, 1):
        for pattern in patterns:
            if re.search(pattern, line):
                start_line = i
                break
        if start_line:
            break
    
    if start_line is None:
        return None
    
    end_line = _find_function_end_go(content, start_line, function_name)
    if end_line is None:
        return None
    
    return (start_line, end_line)


def _extract_class_by_name_js_ts(content: str, class_name: str) -> Optional[Tuple[int, int]]:
    """从 JavaScript/TypeScript 代码中提取类定义的行号范围"""
    lines = content.splitlines()
    
    # 构建类匹配模式
    patterns = [
        rf'^\s*(?:export\s+)?(?:abstract\s+)?class\s+{re.escape(class_name)}\s*(?:extends|implements|\{{)',
        rf'^\s*(?:export\s+)?(?:abstract\s+)?class\s+{re.escape(class_name)}\s*$',
    ]
    
    start_line = None
    for i, line in enumerate(lines, 1):
        for pattern in patterns:
            if re.search(pattern, line):
                start_line = i
                break
        if start_line:
            break
    
    if start_line is None:
        return None
    
    # 使用大括号匹配找到类结束行
    end_line = _find_function_end_js_ts(content, start_line, class_name)
    if end_line is None:
        return None
    
    return (start_line, end_line)


def _extract_class_by_name_python(content: str, class_name: str) -> Optional[Tuple[int, int]]:
    """从 Python 代码中提取类定义的行号范围"""
    lines = content.splitlines()
    
    # 构建类匹配模式
    pattern = rf'^\s*class\s+{re.escape(class_name)}\s*[\(:]'
    
    start_line = None
    for i, line in enumerate(lines, 1):
        if re.search(pattern, line):
            start_line = i
            break
    
    if start_line is None:
        return None
    
    # 使用缩进找到类结束行
    end_line = _find_function_end_python(content, start_line, class_name)
    if end_line is None:
        return None
    
    return (start_line, end_line)


def extract_function_range(
    file_path: str,
    function_name: str,
    repo_root: str,
    current_range: Optional[str] = None
) -> Optional[Tuple[int, int]]:
    """
    提取函数或类的完整行号范围
    
    Args:
        file_path: 文件相对路径
        function_name: 函数名或类名
        repo_root: 仓库根目录
        current_range: 当前行号范围（用于验证和修正）
    
    Returns:
        (start_line, end_line) 或 None
    """
    abs_path = os.path.join(repo_root, file_path)
    if not os.path.exists(abs_path):
        logger.warning(f"文件不存在: {abs_path}")
        return None
    
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"无法读取文件 {abs_path}: {e}")
        return None
    
    ext = _get_file_extension(file_path)
    total_lines = _get_file_total_lines(abs_path)
    
    # 优先尝试作为函数提取（函数/方法优先于类）
    result = None
    if ext in ['.ts', '.tsx', '.js', '.jsx']:
        result = _extract_function_by_name_js_ts(content, function_name)
    elif ext == '.py':
        result = _extract_function_by_name_python(content, function_name)
    elif ext == '.go':
        result = _extract_function_by_name_go(content, function_name)
    else:
        # 对于其他类型，尝试使用 JS/TS 方法（大多数语言使用大括号）
        result = _extract_function_by_name_js_ts(content, function_name)
    
    # 如果函数提取失败，且名称首字母大写，尝试作为类提取
    if result is None and function_name and function_name[0].isupper():
        if ext in ['.ts', '.tsx', '.js', '.jsx']:
            result = _extract_class_by_name_js_ts(content, function_name)
        elif ext == '.py':
            result = _extract_class_by_name_python(content, function_name)
        
        # 如果作为类提取成功，但行号范围很大（可能是整个类），记录警告
        if result:
            start_line, end_line = result
            if end_line - start_line > 50:  # 如果类定义超过50行，可能是整个类
                logger.warning(
                    f"函数名 {function_name} 在 {file_path} 被识别为类定义，"
                    f"行号范围 {start_line}-{end_line} 可能包含整个类。"
                    f"建议使用类中的具体方法名而不是类名。"
                )
    
    if result is None:
        logger.warning(f"无法找到函数/类 {function_name} 在文件 {file_path} 中")
        return None
    
    start_line, end_line = result
    
    # 验证行号范围不超过文件总行数
    if end_line > total_lines:
        logger.warning(f"函数/类 {function_name} 的结束行 {end_line} 超过文件总行数 {total_lines}，修正为 {total_lines}")
        end_line = total_lines
    
    if start_line > total_lines:
        logger.warning(f"函数/类 {function_name} 的起始行 {start_line} 超过文件总行数 {total_lines}")
        return None
    
    # 如果提供了当前范围，记录修正信息
    if current_range:
        parsed = _parse_line_range(current_range)
        if parsed:
            old_start, old_end = parsed
            if old_start != start_line or old_end != end_line:
                logger.info(
                    f"修正函数/类 {function_name} 在 {file_path} 的行号范围: "
                    f"{old_start}-{old_end} -> {start_line}-{end_line}"
                )
    
    return (start_line, end_line)


def correct_implementation_location(
    impl_location: Dict[str, Any],
    repo_root: str
) -> Dict[str, Any]:
    """
    修正单个 implementation_location 中的行号范围
    
    Args:
        impl_location: implementation_location 字典，包含 file, function, lines
        repo_root: 仓库根目录
    
    Returns:
        修正后的 implementation_location
    """
    if not isinstance(impl_location, dict):
        return impl_location
    
    file_path = impl_location.get("file", "")
    function_name = impl_location.get("function", "")
    current_lines = impl_location.get("lines", "")
    
    if not file_path or not function_name:
        return impl_location
    
    # 解析当前行号范围
    current_range = _parse_line_range(current_lines) if current_lines else None
    current_range_size = (current_range[1] - current_range[0] + 1) if current_range else 0
    
    # 尝试提取正确的行号范围
    result = extract_function_range(file_path, function_name, repo_root, current_lines)
    
    if result:
        start_line, end_line = result
        new_range_size = end_line - start_line + 1
        new_lines = f"{start_line}-{end_line}"
        
        # 如果函数名首字母大写（可能是类名），且新范围很大（超过50行），
        # 但原始范围较小（小于50行），则保留原始范围（可能是类中的方法）
        if (function_name and function_name[0].isupper() and 
            new_range_size > 50 and current_range_size > 0 and current_range_size < 50):
            logger.info(
                f"保留原始行号范围 {current_lines}（可能是类 {function_name} 中的方法），"
                f"而不是整个类的范围 {new_lines}"
            )
            corrected = impl_location.copy()
            # 保留原始行号范围
            return corrected
        
        corrected = impl_location.copy()
        corrected["lines"] = new_lines
        
        # 如果行号被修正，记录原始行号
        if current_lines and current_lines != new_lines:
            corrected["original_lines"] = current_lines
        
        return corrected
    
    return impl_location


def validate_and_correct_function_ranges(
    functions: List[Dict[str, Any]],
    repo_root: str
) -> List[Dict[str, Any]]:
    """
    验证并修正函数行号范围列表
    
    Args:
        functions: 函数列表，每个元素包含 file, function_name, line_range 等字段
        repo_root: 仓库根目录
    
    Returns:
        修正后的函数列表
    """
    corrected_functions = []
    corrected_count = 0
    failed_count = 0
    
    for func in functions:
        if not isinstance(func, dict):
            continue
        
        file_path = func.get("file", "")
        function_name = func.get("function_name", "")
        current_range = func.get("line_range", "")
        
        if not file_path or not function_name:
            # 保留原函数信息（即使缺少必要字段）
            corrected_functions.append(func)
            continue
        
        # 尝试提取正确的行号范围
        result = extract_function_range(file_path, function_name, repo_root, current_range)
        
        if result:
            start_line, end_line = result
            new_range = f"{start_line}-{end_line}"
            
            # 创建修正后的函数信息
            corrected_func = func.copy()
            corrected_func["line_range"] = new_range
            
            # 如果行号被修正，记录原始行号
            if current_range and current_range != new_range:
                corrected_func["original_line_range"] = current_range
                corrected_count += 1
            
            corrected_functions.append(corrected_func)
        else:
            # 如果无法提取，保留原信息但标记为未验证
            logger.warning(f"无法验证函数 {function_name} 在 {file_path} 的行号范围")
            corrected_func = func.copy()
            corrected_func["line_range_verified"] = False
            corrected_functions.append(corrected_func)
            failed_count += 1
    
    logger.info(
        f"函数行号验证完成: 总计 {len(functions)} 个函数, "
        f"修正 {corrected_count} 个, 失败 {failed_count} 个"
    )
    
    return corrected_functions

