import json
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .driver import agent_chat_with_tools, simple_chat, VllmOpenAIAdapter
from .tools import get_tools, execute_tool
from .prompt import SYSTEM_PROMPT, build_initial_prompt, build_preprocess_prompt, build_multi_requirements_prompt
from .todolist_tool import (
    get_summary,
)
from .ast_parser import validate_and_correct_function_ranges, correct_implementation_location

# 配置日志
logger = logging.getLogger("agent.analyzer")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 防止日志传播到根 logger，避免重复打印


def _sanitize_json_like_text(text: str) -> str:
    import re

    def _fix_invalid_escape(match: "re.Match[str]") -> str:
        # 移除非法转义字符前的反斜杠，保留原字符
        return match.group(1)

    # 仅处理 JSON 未定义的转义序列，合法转义（\" \\ \/ \b \f \n \r \t \uXXXX）保持
    return re.sub(r'\\([^"\\/bfnrtu])', _fix_invalid_escape, text)


def _extract_json(text: str) -> Dict[str, Any]:
    # 仅用于从模型输出中提取 JSON（不做业务字符串判断）
    if not text:
        return {}
    t = text.strip()
    sanitized_t = _sanitize_json_like_text(t)
    
    # 1. 尝试提取代码块中的 JSON
    if t.startswith("```"):
        parts = t.split("```")
        for seg in parts:
            s = seg.strip()
            if s.startswith("json"):
                s = s[4:].strip()
            if s.startswith("{") or s.startswith("["):
                t = s
                sanitized_t = _sanitize_json_like_text(t)
                break
    
    # 2. 直接尝试解析整个文本
    try:
        return json.loads(sanitized_t)
    except Exception:
        pass
    
    # 3. 尝试在文本中查找最外层 JSON 对象（从第一个 { 开始）
    import re
    # 查找第一个 { 到最后一个 } 之间的内容
    brace_start = t.find("{")
    if brace_start >= 0:
        # 从第一个 { 开始，找到匹配的 }
        brace_count = 0
        json_end = -1
        for i in range(brace_start, len(t)):
            if t[i] == '{':
                brace_count += 1
            elif t[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_end = i + 1
                    break
        if json_end > 0:
            try:
                json_str = _sanitize_json_like_text(t[brace_start:json_end])
                return json.loads(json_str)
            except Exception:
                pass
    
    # 4. 尝试查找所有可能的 JSON 对象，取最大的
    json_matches = list(re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', t, re.DOTALL))
    if json_matches:
        # 尝试解析每个匹配，取第一个成功的
        for match in json_matches[::-1]:  # 从后往前，优先取最大的
            try:
                return json.loads(_sanitize_json_like_text(match.group(0)))
            except Exception:
                continue
    
    # 5. 最后尝试：查找包含 "categories" 或 "feature_analysis" 的 JSON
    if "categories" in t or "feature_analysis" in t:
        # 尝试找到包含这些关键词的 JSON 对象
        pattern = r'\{[^{}]*"(?:categories|feature_analysis)"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
        matches = list(re.finditer(pattern, t, re.DOTALL))
        for match in matches[::-1]:
            try:
                return json.loads(_sanitize_json_like_text(match.group(0)))
            except Exception:
                continue
    
    # 6. 尝试处理被截断的 JSON（如果包含 categories 或 feature_analysis）
    if "categories" in t or "feature_analysis" in t:
        # 找到第一个 { 的位置
        brace_start = t.find("{")
        if brace_start >= 0:
            # 尝试从第一个 { 开始，逐步向后查找，找到最长的可解析 JSON
            # 从后往前尝试，找到最后一个完整的字段
            for end_pos in range(len(t), brace_start, -1):
                candidate = t[brace_start:end_pos]
                # 尝试补全 JSON（如果最后一个字段不完整）
                # 移除最后一个不完整的字段
                if candidate.rstrip().endswith(","):
                    candidate = candidate.rstrip()[:-1]
                # 尝试补全闭合括号
                open_braces = candidate.count("{")
                close_braces = candidate.count("}")
                if open_braces > close_braces:
                    candidate += "}" * (open_braces - close_braces)
                # 尝试补全闭合方括号
                open_brackets = candidate.count("[")
                close_brackets = candidate.count("]")
                if open_brackets > close_brackets:
                    candidate += "]" * (open_brackets - close_brackets)
                # 移除末尾可能的逗号
                candidate = candidate.rstrip().rstrip(",")
                # 尝试解析
                try:
                    sanitized = _sanitize_json_like_text(candidate)
                    result = json.loads(sanitized)
                    # 验证结果是否包含必要的字段
                    if isinstance(result, dict) and ("categories" in result or "feature_analysis" in result):
                        return result
                except Exception:
                    continue
    
    return {}


def _function_key(file_path: str, function_name: str) -> str:
    return f"{file_path}::{function_name}"


def _collect_existing_function_keys(features: List[Dict[str, Any]]) -> set:
    keys = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        impl_locs = feature.get("implementation_location", [])
        if not isinstance(impl_locs, list):
            continue
        for impl_loc in impl_locs:
            if not isinstance(impl_loc, dict):
                continue
            file_path = str(impl_loc.get("file", "")).strip()
            func_name = str(impl_loc.get("function", "")).strip()
            if file_path and func_name:
                keys.add(_function_key(file_path, func_name))
    return keys


def _ensure_categories_cover_requirements(
    categories: List[Dict[str, Any]],
    sub_requirements: List[str],
) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        name = str(cat.get("name", "")).strip()
        if name and name not in seen:
            seen.add(name)
            result.append(cat)
    for req in sub_requirements:
        req_name = str(req).strip()
        if req_name and req_name not in seen:
            result.append({"name": req_name, "description": req_name})
            seen.add(req_name)
    return result


def _derive_requirement_indices(sub_requirements: List[str]) -> Dict[str, int]:
    def find_index(keywords: List[str], default: int = 0) -> int:
        for idx, req in enumerate(sub_requirements):
            if any(keyword in req for keyword in keywords):
                return idx
        return min(default, len(sub_requirements) - 1) if sub_requirements else 0

    return {
        "api": find_index(["API", "接口"]),
        "db": find_index(["数据库", "存储"], default=0),
        "msg_list": find_index(["消息列表", "分页", "排序"], default=0),
        "channel_create": find_index(["频道创建", "创建频道"], default=0),
        "deploy": find_index(["部署", "Docker", "容器"], default=0),
    }


def _classify_function_to_requirement(
    function_info: Dict[str, Any],
    sub_requirements: List[str],
    req_indices: Dict[str, int],
) -> int:
    if not sub_requirements:
        return 0
    file_lower = str(function_info.get("file", "")).lower()
    name_lower = str(function_info.get("function_name", "")).lower()
    desc = str(function_info.get("description", ""))

    if "docker" in file_lower:
        return req_indices["deploy"]
    if "docker-compose" in file_lower:
        return req_indices["deploy"]
    if "resolver" in file_lower:
        if "channel" in file_lower and "create" in name_lower:
            return req_indices["channel_create"]
        if "message" in file_lower and "findall" in name_lower:
            return req_indices["msg_list"]
        return req_indices["api"]
    if "findall" in name_lower and "message" in file_lower:
        return req_indices["msg_list"]
    if "message" in file_lower and ("列表" in desc or "查询" in desc):
        return req_indices["msg_list"]
    if "create" in name_lower and "channel" in file_lower:
        return req_indices["channel_create"]
    if "entity" in file_lower:
        return req_indices["db"]
    if "service" in file_lower:
        if "findall" in name_lower and "message" in file_lower:
            return req_indices["msg_list"]
        if "create" in name_lower and "channel" in file_lower:
            return req_indices["channel_create"]
        return req_indices["db"]
    return req_indices["db"]


def _build_features_for_missing_functions(
    missing_functions: List[Dict[str, Any]],
    sub_requirements: List[str],
) -> List[Dict[str, Any]]:
    if not missing_functions or not sub_requirements:
        return []

    req_indices = _derive_requirement_indices(sub_requirements)
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for func in missing_functions:
        idx = _classify_function_to_requirement(func, sub_requirements, req_indices)
        grouped[idx].append(func)

    fallback_index = 0
    if sub_requirements:
        fallback_index = len(sub_requirements) - 1

    features: List[Dict[str, Any]] = []
    for idx, funcs in grouped.items():
        req_idx = idx if 0 <= idx < len(sub_requirements) else fallback_index
        feature_desc = sub_requirements[req_idx]
        impl_locations = []
        for f in funcs:
            impl_locations.append(
                {
                    "file": f.get("file", ""),
                    "function": f.get("function_name", ""),
                    "lines": f.get("line_range") or f.get("original_line_range", ""),
                }
            )
        features.append(
            {
                "feature_description": feature_desc,
                "implementation_location": impl_locations,
            }
        )
    return features


def _preprocess_requirements(problem_description: str, client: Any) -> List[str]:
    """预处理需求文本，拆分为子需求列表"""
    logger.info("预处理需求，拆分为子需求...")
    
    preprocess_prompt = build_preprocess_prompt(problem_description)
    # 通过环境变量控制是否使用本地模型，默认不使用（避免CPU上加载超大模型导致卡死）
    _local_llm_dir = os.getenv("LOCAL_LLM_DIR") or None
    response = simple_chat(
        client=client,
        api_key="",  # 使用本地 / VLLM 客户端，不依赖外部 API Key
        prompt=preprocess_prompt,
        system="你是一位需求分析师。输出仅 JSON，不要其他文字。",
        local_model_dir=_local_llm_dir,
        local_max_output_len=int(os.getenv("OLLAMA_MAX_TOKENS", "512")),
    )
    
    data = _extract_json(response)
    
    # 检查 data 是否是字典
    if not isinstance(data, dict):
        logger.warning(f"解析返回的不是字典，类型: {type(data)}，使用原始需求")
        return [problem_description]
    
    sub_reqs = data.get("sub_requirements", [])
    
    # 如果 sub_requirements 是字符串，尝试按换行符分割
    if isinstance(sub_reqs, str):
        logger.info("解析到字符串格式的子需求，尝试按换行符分割...")
        raw_lines = [line.strip() for line in sub_reqs.split("\n") if line.strip()]
        if len(raw_lines) > 0:
            logger.info(f"按换行符拆分为 {len(raw_lines)} 个子需求")
            deduped_list = []
            seen = set()
            for s in raw_lines:
                k = s.lower()
                if k not in seen:
                    deduped_list.append(s)
                    seen.add(k)
            if len(deduped_list) > 5:
                logger.info(f"子需求超过5个，截断到5个（原有 {len(deduped_list)} 个）")
                deduped_list = deduped_list[:5]
            if len(deduped_list) > 0:
                logger.info(f"最终子需求数量: {len(deduped_list)}")
                return deduped_list
        else:
            logger.warning("分割后为空，使用原始需求")
            return [problem_description]
    
    # 如果是列表
    if isinstance(sub_reqs, list) and len(sub_reqs) > 0:
        result_raw = [str(s).strip() for s in sub_reqs if s and str(s).strip()]
        deduped_list = []
        seen = set()
        for s in result_raw:
            k = s.lower()
            if k not in seen:
                deduped_list.append(s)
                seen.add(k)
        if len(deduped_list) > 5:
            logger.info(f"子需求超过5个，截断到5个（原有 {len(deduped_list)} 个）")
            deduped_list = deduped_list[:5]
        if len(deduped_list) > 0:
            logger.info(f"拆分为 {len(deduped_list)} 个子需求")
            return deduped_list
    
    # 回退：如果解析失败，将原需求作为单个子需求
    logger.warning(f"无法解析子需求，data: {data}，使用原始需求")
    return [problem_description]


def _generate_fallback_result(functions: List[Dict[str, Any]], all_categories: List[Dict[str, Any]], all_feature_analysis: List[Dict[str, Any]]) -> None:
    """备用方案：按文件分组生成结果"""
    logger.info("使用备用方案：按文件分组生成结果")
    if not functions:
        return
    
    # 按文件分组
    file_groups: Dict[str, List[Dict[str, Any]]] = {}
    for func in functions:
        if isinstance(func, dict):
            file_path = func.get("file", "")
            if file_path:
                if file_path not in file_groups:
                    file_groups[file_path] = []
                file_groups[file_path].append(func)
    
    # 为每个文件创建一个 feature_analysis 条目
    for file_path, funcs in file_groups.items():
        implementation_locations = []
        for func in funcs:
            func_name = func.get("function_name", "")
            line_range = func.get("line_range", "")
            if func_name and line_range:
                implementation_locations.append({
                    "file": file_path,
                    "function": func_name,
                    "lines": line_range
                })
        
        if implementation_locations:
            # 使用第一个函数的描述作为 feature_description，或者使用文件名
            description = funcs[0].get("description", f"{file_path} 中的函数实现")
            all_feature_analysis.append({
                "feature_description": description,
                "implementation_location": implementation_locations
            })
    
    # 添加默认分类
    if not all_categories:
        all_categories.append({"name": "代码实现", "description": "代码库中的函数实现"})
    
    logger.info(f"备用方案生成了 {len(all_feature_analysis)} 个功能分析条目")


def analyze_repository(problem_description: str, repo_root: str) -> Dict[str, Any]:
    logger.info(f"开始分析代码库: {repo_root}")
    logger.info(f"需求: {problem_description[:100]}...")
    
    client = VllmOpenAIAdapter(
        base_url="http://localhost:8000/v1",
        model_name="/data/Qwen3-Coder-30B-A3B-Instruct",
    )

    # 步骤1: 预处理需求，拆分为子需求


    sub_requirements = _preprocess_requirements(problem_description, client=client)
    
    tools = get_tools()
    read_files_log: List[str] = []
    all_categories: List[Dict[str, Any]] = []
    all_feature_analysis: List[Dict[str, Any]] = []
    
    # 步骤2: 对每个子需求分别调用 agent_chat_with_tools（深度阅读代码过程）
    # 工具调用计数器（用于跟踪步骤）
    tool_call_counter = {"count": 0}
    
    def _extract_tool_params_summary(tool_name: str, arguments: Dict[str, Any]) -> str:
        """提取工具参数的关键信息用于日志"""
        if tool_name == "read_file":
            file_path = arguments.get("file_path", "未知")
            max_bytes = arguments.get("max_bytes", "")
            return f"文件: {file_path}" + (f", 最大字节: {max_bytes}" if max_bytes else "")
        elif tool_name == "read_directory":
            dir_path = arguments.get("directory_path", ".")
            recursive = arguments.get("recursive", False)
            max_files = arguments.get("max_files", "")
            return f"目录: {dir_path}, 递归: {recursive}" + (f", 最大文件数: {max_files}" if max_files else "")
        elif tool_name == "grep":
            pattern = arguments.get("pattern", "")
            directory = arguments.get("directory", ".")
            max_results = arguments.get("max_results", "")
            return f"模式: {pattern[:50]}{'...' if len(pattern) > 50 else ''}, 目录: {directory}" + (f", 最大结果: {max_results}" if max_results else "")
        elif tool_name == "glob":
            pattern = arguments.get("pattern", "")
            directory = arguments.get("directory", ".")
            return f"模式: {pattern}, 目录: {directory}"
        elif tool_name == "find":
            name_pattern = arguments.get("name_pattern", "")
            directory = arguments.get("directory", ".")
            type_ = arguments.get("type", "f")
            return f"名称模式: {name_pattern}, 目录: {directory}, 类型: {type_}"
        elif tool_name == "bash":
            command = arguments.get("command", "")
            return f"命令: {command[:100]}{'...' if len(command) > 100 else ''}"
        elif tool_name == "save_memory":
            file_path = arguments.get("file_path", "")
            func_count = len(arguments.get("functions", [])) if isinstance(arguments.get("functions"), list) else 0
            return f"文件: {file_path}, 函数数: {func_count}"
        elif tool_name.startswith("tasklist_"):
            # tasklist 相关工具的参数简化
            if "task_ids" in arguments:
                ids = arguments.get("task_ids")
                if isinstance(ids, list):
                    return f"task_ids: {', '.join(ids[:3])}"
                return f"task_ids: {ids}"
            elif "sections" in arguments:
                return "创建分区"
            else:
                return "无参数" if not arguments else json.dumps(arguments, ensure_ascii=False)[:100]
        else:
            # 默认：输出参数的前100字符
            params_str = json.dumps(arguments, ensure_ascii=False)
            return params_str[:100] + ("..." if len(params_str) > 100 else "")
    
    def _record_read_file(arguments: Dict[str, Any]) -> None:
        file_path = arguments.get("file_path")
        if not file_path:
            return
        try:
            abs_path = file_path if os.path.isabs(file_path) else os.path.join(repo_root, file_path)
            abs_path = os.path.realpath(abs_path)
            rel_path = os.path.relpath(abs_path, repo_root)
        except Exception:
            rel_path = str(file_path)
        read_files_log.append(rel_path)
        logger.info(f"   📄 已记录阅读文件 #{len(read_files_log)}: {rel_path}")

    def tool_executor(tool_name: str, arguments: Dict[str, Any], step_info: Optional[Dict[str, int]] = None) -> str:
        """工具执行包装器，添加详细日志（不输出文件内容）"""
        tool_call_counter["count"] += 1
        current_step = tool_call_counter["count"]
        
        # 提取步骤信息
        total_steps = step_info.get("total_steps", "?") if step_info else "?"
        iteration = step_info.get("iteration", 0) if step_info else 0
        
        # 提取关键参数用于日志
        tool_params_summary = _extract_tool_params_summary(tool_name, arguments)
        
        # 输出工具调用信息
        logger.info(f"[步骤 {current_step}] 🔧 {tool_name} (迭代 {iteration + 1})")
        logger.info(f"   参数: {tool_params_summary}")
        
        # 执行工具，确保任务清单工具正确传递 repo_root 参数
        # 对于任务清单工具，我们需要确保 repo_root 参数被正确传递
        if tool_name.startswith("tasklist_"):
            # 确保 arguments 中包含 repo_root
            tool_args = arguments.copy()
            tool_args["repo_root"] = repo_root
            result = execute_tool(tool_name, tool_args, repo_root)
        else:
            result = execute_tool(tool_name, arguments, repo_root)
        if tool_name == "read_file":
            _record_read_file(arguments)
        result_str = str(result)
        
        # 根据工具类型决定是否输出结果内容
        if tool_name in ["read_file", "read_directory"]:
            # 文件读取类工具：只输出文件/目录路径和结果大小，不输出内容
            logger.info(f"   结果: 已读取 ({len(result_str)} 字符)")
        elif tool_name == "grep":
            # grep 工具：只输出匹配数量，不输出具体内容
            match_count = len(result_str.splitlines()) if result_str else 0
            logger.info(f"   结果: 找到 {match_count} 处匹配")
        elif tool_name == "save_memory":
            # save_memory 工具：输出保存结果
            logger.info(f"   结果: {result_str[:200]}")
        elif tool_name.startswith("tasklist_"):
            # tasklist 工具：输出简化结果
            logger.info(f"   结果: {result_str[:200]}")
        else:
            # 其他工具：输出简化结果（最多200字符）
            if len(result_str) > 200:
                logger.info(f"   结果: {result_str[:200]}... ({len(result_str)} 字符)")
            else:
                logger.info(f"   结果: {result_str}")
        
        return result
    
    # 确保 sub_requirements 是列表
    if not isinstance(sub_requirements, list):
        logger.warning(f"sub_requirements 不是列表，类型: {type(sub_requirements)}，转换为列表")
        if isinstance(sub_requirements, str):
            # 如果是字符串，按换行符分割
            temp_str = str(sub_requirements)  # 显式转换为 str 类型
            sub_requirements = [line.strip() for line in temp_str.split("\n") if line.strip()]
        else:
            sub_requirements = [str(sub_requirements)]
    
    # 确保列表中都是非空字符串
    sub_requirements = [str(s).strip() for s in sub_requirements if s and str(s).strip()]
    if not sub_requirements:
        logger.warning("子需求列表为空，使用原始需求")
        sub_requirements = [problem_description]
    
    logger.info(f"最终确定的子需求数量: {len(sub_requirements)}")
    logger.info("子需求列表：")
    for idx, sub_req in enumerate(sub_requirements, 1):
        logger.info(f"  {idx}. {sub_req}")
    
    # 预先准备目录缓存和任务清单摘要（避免在并发中重复构建）
    # 优先加载目录缓存（memory/dir_cache.json），不存在则扫描并写入缓存
    root_tree = ""
    dir_cache_path = os.path.join(repo_root, "memory", "dir_cache.json")
    dir_cache_items: List[str] = []
    try:
        if os.path.exists(dir_cache_path):
            with open(dir_cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            dir_cache_items = cache.get("items", [])
            logger.info(f"已加载目录缓存，共 {len(dir_cache_items)} 项")
        else:
            dir_cache_result = execute_tool("read_directory", {"directory_path": ".", "recursive": True, "max_files": 2000}, repo_root)
            dir_cache_items = str(dir_cache_result).splitlines()
    except Exception:
        pass
    root_tree = "\n".join(dir_cache_items[:2000])

    # 加载 memory/todolist.json 构建摘要
    def _summarize_tasklist(repo_root: str) -> Tuple[str, List[Dict[str, Any]]]:
        try:
            mem_todo = os.path.join(repo_root, "memory", "todolist.json")
            tasks: List[Dict[str, Any]] = []
            if os.path.exists(mem_todo):
                with open(mem_todo, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            else:
                summary = get_summary(repo_root)
                text = (
                    f"总计 {summary['total']} 项，待处理: {summary['status_counts'].get('pending', 0)}，"
                    f"已完成: {summary['status_counts'].get('completed', 0)}，已取消: {summary['status_counts'].get('cancelled', 0)}"
                )
                return text, []
            if isinstance(raw, dict) and "tasks" in raw:
                tasks = [t for t in raw.get("tasks", []) if isinstance(t, dict)]
            elif isinstance(raw, list):
                tasks = [t for t in raw if isinstance(t, dict)]
            else:
                tasks = []

            total = len(tasks)
            pending = [i for i in tasks if i.get("status") == "pending"]
            completed = [i for i in tasks if i.get("status") == "completed" or i.get("status") == "done"]
            cancelled = [i for i in tasks if i.get("status") == "cancelled"]
            lines = []
            lines.append(f"共 {total} 项；待处理 {len(pending)}；已完成 {len(completed)}；已取消 {len(cancelled)}")
            if completed:
                lines.append("- ✅ 已完成（最多展示3项）：")
                for it in completed[:3]:
                    lines.append(f"  - {it.get('content') or it.get('title', '')}")
            if pending:
                lines.append("- ⏳ 待处理（最多展示5项）：")
                for it in pending[:5]:
                    lines.append(f"  - {it.get('content') or it.get('title', '')}")
            return "\n".join(lines), tasks
        except Exception:
            summary = get_summary(repo_root)
            text = (
                f"总计 {summary['total']} 项，待处理: {summary['status_counts'].get('pending', 0)}，"
                f"已完成: {summary['status_counts'].get('completed', 0)}，已取消: {summary['status_counts'].get('cancelled', 0)}"
            )
            return text, []

    tasklist_summary, tasklist_items = _summarize_tasklist(repo_root)

    # 状态提示（显式告知模型状态已加载）
    state_tip_lines: List[str] = []
    if tasklist_items:
        completed_count = len([i for i in tasklist_items if i.get("status") in ("completed", "done")])
        state_tip_lines.append(f"你当前的任务清单已加载，共 {len(tasklist_items)} 项，其中 {completed_count} 项已完成。")
    if dir_cache_items:
        state_tip_lines.append(f"目录缓存已加载：{len(dir_cache_items)} 个条目。可复用缓存避免重复扫描。")
    state_tip = "\n".join(state_tip_lines)

    # 步骤2: 一次性处理所有子需求，模型自主探索
    logger.info(f"\n开始分析所有子需求（共 {len(sub_requirements)} 个）")
    logger.info("子需求列表：")
    for idx, sub_req in enumerate(sub_requirements, 1):
        logger.info(f"  {idx}. {sub_req}")
    
    # 清空记忆文件（在分析开始时）
    memory_file_path = "memory/memory.md"
    memory_file_abs_path = os.path.join(repo_root, memory_file_path)
    try:
        # 确保目录存在
        memory_dir = os.path.dirname(memory_file_abs_path)
        if memory_dir and not os.path.exists(memory_dir):
            os.makedirs(memory_dir, exist_ok=True)
        # 清空文件（如果存在）
        if os.path.exists(memory_file_abs_path):
            with open(memory_file_abs_path, "w", encoding="utf-8") as f:
                f.write("")  # 清空文件
            logger.info(f"已清空记忆文件: {memory_file_path}")
        else:
            # 创建空文件
            with open(memory_file_abs_path, "w", encoding="utf-8") as f:
                f.write("")
            logger.info(f"已创建新的记忆文件: {memory_file_path}")
    except Exception as e:
        logger.warning(f"清空记忆文件失败: {e}")
    
    # 清空函数分析汇总文件（在分析开始时）
    function_analysis_file_path = "memory/function_analysis.json"
    function_analysis_abs_path = os.path.join(repo_root, function_analysis_file_path)
    try:
        # 确保目录存在
        function_analysis_dir = os.path.dirname(function_analysis_abs_path)
        if function_analysis_dir and not os.path.exists(function_analysis_dir):
            os.makedirs(function_analysis_dir, exist_ok=True)
        # 清空文件（如果存在），创建空的 JSON 结构
        initial_data = {
            "functions": [],
            "updated_at": None
        }
        with open(function_analysis_abs_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f, ensure_ascii=False, indent=2)
        logger.info(f"已清空函数分析汇总文件: {function_analysis_file_path}")
    except Exception as e:
        logger.warning(f"清空函数分析汇总文件失败: {e}")
    
    # 重置工具调用计数器
    tool_call_counter["count"] = 0
    
    # 构建包含所有子需求的初始提示
    initial_prompt = build_multi_requirements_prompt(
        sub_requirements,
        tasklist_summary=tasklist_summary,
        root_tree=str(root_tree)[: int(os.getenv("ROOT_TREE_MAX_CHARS", "20000")) ],
        state_tip=state_tip,
    )
    
    _local_llm_dir = "/data/qwen3-coder-30b"
    final_text, tool_history = agent_chat_with_tools(
        api_key="",  # 使用本地 / VLLM 客户端，不依赖外部 API Key
        client=client,
        system=SYSTEM_PROMPT,
        initial_prompt=initial_prompt,
        tools=tools,
        tool_executor=tool_executor,
        max_iterations=9999,
        repo_root=repo_root,
        memory_file_path=memory_file_path,
        local_model_dir=_local_llm_dir,
        local_max_output_len=int(os.getenv("OLLAMA_MAX_TOKENS", "2048")),
    )
    
    logger.info(f"第一阶段（代码库分析）完成，工具调用 {len(tool_history)} 次")
    
    # 调试：记录模型输出的前500字符
    final_text_preview = str(final_text) if final_text else ""
    logger.info(f"模型输出预览: {final_text_preview}")
    
    # ========== 第二阶段：读取函数分析文件并生成最终JSON ==========
    logger.info("=" * 80)
    logger.info("进入第二阶段：函数分类和最终JSON生成")
    logger.info("=" * 80)
    
    # 读取函数分析汇总文件
    function_analysis_file = os.path.join(repo_root, "memory", "function_analysis.json")
    function_analysis_data = None
    functions = []
    
    if os.path.exists(function_analysis_file):
        try:
            with open(function_analysis_file, "r", encoding="utf-8") as f:
                function_analysis_data = json.load(f)
                logger.info(f"成功读取汇总文件: {function_analysis_file}")
                if isinstance(function_analysis_data, dict) and "functions" in function_analysis_data:
                    functions = function_analysis_data.get("functions", [])
                    logger.info(f"汇总文件中包含 {len(functions)} 个函数分析结果")
        except Exception as e:
            logger.warning(f"读取汇总文件失败: {str(e)}")
    else:
        logger.warning(f"函数分析汇总文件不存在: {function_analysis_file}")
    
    if not functions:
        logger.warning("未找到函数分析数据，生成默认结果")
        all_categories = [{"name": "分析结果", "description": "代码库分析结果"}]
        all_feature_analysis = [{"feature_description": "功能分析", "implementation_location": []}]
    else:
        # ========== 使用 AST 解析器验证和修正函数行号 ==========
        logger.info("=" * 80)
        logger.info("使用 AST 解析器验证和修正函数行号范围")
        logger.info("=" * 80)
        logger.info(f"开始验证 {len(functions)} 个函数的行号范围...")
        
        # try:
        #     functions = validate_and_correct_function_ranges(functions, repo_root)
        #     logger.info(f"函数行号验证完成，共处理 {len(functions)} 个函数")
        # except Exception as e:
        #     logger.error(f"函数行号验证失败: {str(e)}", exc_info=True)
        #     logger.warning("继续使用原始函数数据，但行号可能不准确")
        
        # 使用模型将函数分类到子需求中
        logger.info(f"开始将 {len(functions)} 个函数分类到 {len(sub_requirements)} 个子需求中")
        
        # 构建第二阶段提示词
        functions_json = json.dumps(functions, ensure_ascii=False, indent=2)
        sub_reqs_text = "\n".join(f"{i+1}. {req}" for i, req in enumerate(sub_requirements))
        
        phase2_prompt = f"""你是一位代码分析师。现在需要将已分析的函数分类到各个子需求中,并生成最终的JSON格式输出。

【子需求列表】
{sub_reqs_text}

【已分析的函数列表（共 {len(functions)} 个）】
{functions_json}

【任务要求】
1. 仔细分析每个函数的功能描述和文件路径
2. 将每个函数归类到最相关的子需求中
3. **关键：所有 {len(functions)} 个函数都必须出现在最终输出中,不能遭漏任何一个！**
4. 如果某个函数与多个子需求相关,可以在多个 feature_analysis 中重复引用
5. 生成符合以下格式的JSON输出：

{{
  "categories": [
    {{"name": "分类名称", "description": "分类描述"}}
  ],
  "feature_analysis": [
    {{
      "feature_description": "功能描述（对应子需求或功能点）",
      "implementation_location": [
        {{"file": "文件相对路径", "function": "函数名", "lines": "行号范围（如 12-90）"}}
      ]
    }}
  ]
}}

【重要要求】
- categories 应该基于子需求创建,每个分类对应一个子需求或功能模块
- feature_analysis 中的每个条目应该对应一个子需求或功能点
- implementation_location 中的函数应该来自已分析的函数列表
- **确保所有 {len(functions)} 个函数都被归类到相应的 feature_analysis 中**
- **在输出前,请检查是否所有函数都已包含,确保数量匹配**
- **关键：function 字段必须使用准确的函数名或方法名，不能使用类名！**
  - 如果 function_name 是类名（如 AppModule, AppController），应该查找该类中的具体方法
  - 如果无法确定具体方法，应该使用类名但标注为类定义
  - 优先使用函数/方法名，而不是类名
- **关键：lines 字段必须使用函数分析数据中的准确 line_range 字段，确保行号范围精确对应函数体，不能是整个类的范围**
- 行号范围必须精确对应函数体，不能包含整个类定义
- 输出必须是标准的JSON格式,不要有额外符号,确保兼容JSON解析

请直接输出JSON,不要任何额外文字。"""
        print(f"phase2_prompt: {phase2_prompt}")
        try:
            # 调用模型生成分类结果
            phase2_result = simple_chat(
                api_key="",
                client=client,
                prompt=phase2_prompt,
                system="你是一位代码分析师,擅长将函数分类到需求中并生成结构化JSON输出。",
                model="/data/Qwen3-Coder-30B-A3B-Instruct",
                local_max_output_len=int(os.getenv("OLLAMA_MAX_TOKENS", "8192")),  # 增加到8192以容纳更多函数
            )
            
            # 记录完整的输出（用于调试）
            logger.info(f"第二阶段模型输出长度: {len(phase2_result)} 字符")
            logger.info(f"第二阶段模型输出（前1000字符）: {phase2_result[:1000]}...")
            if len(phase2_result) > 1000:
                logger.info(f"第二阶段模型输出（后500字符）: ...{phase2_result[-500:]}")
            
            # 提取JSON
            phase2_data = _extract_json(phase2_result)
            
            # 如果提取失败，记录原始输出用于调试（限制长度）
            if not phase2_data or not isinstance(phase2_data, dict):
                if len(phase2_result) > 2000:
                    logger.warning(f"JSON提取失败，原始输出长度: {len(phase2_result)} 字符")
                    logger.warning(f"原始输出（前1000字符）: {phase2_result[:1000]}...")
                    logger.warning(f"原始输出（后500字符）: ...{phase2_result[-500:]}")
                else:
                    logger.warning(f"JSON提取失败，原始输出: {phase2_result}")
            if isinstance(phase2_data, dict):
                cats = phase2_data.get("categories", []) or []
                features = phase2_data.get("feature_analysis", []) or []
                logger.info(f"第二阶段提取结果: {len(cats)} 个分类, {len(features)} 个功能分析")
                
                # 验证是否所有函数都被包含
                if isinstance(features, list):
                    output_function_count = 0
                    for feature in features:
                        if isinstance(feature, dict):
                            impl_locs = feature.get("implementation_location", [])
                            if isinstance(impl_locs, list):
                                output_function_count += len(impl_locs)
                    
                    logger.info(f"函数数量验证: 输入 {len(functions)} 个, 输出 {output_function_count} 个")
                    
                    if output_function_count < len(functions):
                        logger.warning(f"警告: 模型输出仅包含 {output_function_count}/{len(functions)} 个函数,可能存在遭漏")
                        missing_count = len(functions) - output_function_count
                        logger.warning(f"缺失 {missing_count} 个函数,可能是输出长度限制导致被截断")
                
                if isinstance(cats, list):
                    all_categories.extend(cats)
                if isinstance(features, list):
                    # 对每个 feature 中的 implementation_location 进行行号验证和修正
                    logger.info("验证最终输出中的函数行号范围...")
                    corrected_features = []
                    for feature in features:
                        if isinstance(feature, dict):
                            corrected_feature = feature.copy()
                            impl_locs = feature.get("implementation_location", [])
                            if isinstance(impl_locs, list):
                                corrected_impl_locs = []
                                for impl_loc in impl_locs:
                                    if isinstance(impl_loc, dict):
                                        corrected_impl_loc = correct_implementation_location(impl_loc, repo_root)
                                        corrected_impl_locs.append(corrected_impl_loc)
                                    else:
                                        corrected_impl_locs.append(impl_loc)
                                corrected_feature["implementation_location"] = corrected_impl_locs
                            corrected_features.append(corrected_feature)
                        else:
                            corrected_features.append(feature)
                    all_feature_analysis.extend(corrected_features)
                    logger.info("最终输出中的函数行号验证完成")
            else:
                logger.warning("第二阶段未能提取有效JSON，使用备用方案")
                # 备用方案：按文件分组
                _generate_fallback_result(functions, all_categories, all_feature_analysis)
        except Exception as e:
            logger.error(f"第二阶段处理失败: {str(e)}", exc_info=True)
            # 备用方案：按文件分组
            _generate_fallback_result(functions, all_categories, all_feature_analysis)
    
    # 如果仍然没有结果，生成默认结果
    if not all_feature_analysis:
        logger.warning("第二阶段未能生成有效结果，生成默认JSON结果")
        # 生成默认的JSON结果以确保程序能够继续
        default_result = {
            "categories": [
                {"name": "分析结果", "description": "代码库分析结果"}
            ],
            "feature_analysis": [
                {
                    "feature_description": "功能分析",
                    "implementation_location": []
                }
            ]
        }
        all_categories.extend(default_result["categories"])
        all_feature_analysis.extend(default_result["feature_analysis"])
        logger.info("生成了默认JSON结果")
    
    function_key_map = {}
    for func in functions or []:
        file_path = str(func.get("file", "")).strip()
        func_name = str(func.get("function_name", "")).strip()
        if file_path and func_name:
            function_key_map[_function_key(file_path, func_name)] = func
    
    included_keys = _collect_existing_function_keys(all_feature_analysis)
    missing_keys = [key for key in function_key_map.keys() if key not in included_keys]
    if missing_keys:
        logger.warning(f"检测到 {len(missing_keys)} 个函数未出现在模型输出中，自动补全...")
        missing_functions = [function_key_map[key] for key in missing_keys]
        supplemental_features = _build_features_for_missing_functions(missing_functions, sub_requirements)
        all_feature_analysis.extend(supplemental_features)
        included_keys = _collect_existing_function_keys(all_feature_analysis)
        logger.info(f"已自动补充 {len(missing_functions)} 个函数到 JSON 输出中")
    
    # 去重 categories（按 name），并确保覆盖所有子需求
    unique_categories = _ensure_categories_cover_requirements(all_categories, sub_requirements)
    
    # 最终统计输出中的函数数量（按唯一函数计数）
    total_output_functions = len(included_keys)
    
    logger.info(f"\n分析完成: {len(unique_categories)} 个分类, {len(all_feature_analysis)} 个功能分析")
    logger.info(f"函数覆盖统计: 输入 {len(functions)} 个, 最终输出 {total_output_functions} 个")
    if read_files_log:
        logger.info("模型阅读过的代码文件（按阅读顺序）:")
        for idx, path in enumerate(read_files_log, 1):
            logger.info(f"  {idx}. {path}")
    else:
        logger.info("模型在分析过程中未调用 read_file 读取代码文件")
    
    if functions and total_output_functions < len(functions):
        logger.warning(f"警告: 最终输出仅包含 {total_output_functions}/{len(functions)} 个函数")
        logger.warning("建议增加 OLLAMA_MAX_TOKENS 环境变量以容纳更多函数")
    elif functions and total_output_functions == len(functions):
        logger.info("✅ 所有函数均已包含在输出中")
    
    return {
        "categories": unique_categories,
        "feature_analysis": all_feature_analysis,
        "read_files": read_files_log,
    }


