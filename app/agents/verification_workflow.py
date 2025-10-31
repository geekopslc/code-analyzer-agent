"""独立的验证工作流 - 生成测试代码并执行验证，支持自修复"""
import json
import re
import time
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.utils.logger import get_logger
from app.agents.js_runner import execute_js_test
from app.agents.model_driver import ModelDriver
from app.utils.code_parser import read_text, walk_repository
import os

log = get_logger("verification")

# 最大重试次数
MAX_RETRIES = 1


# ============ 工具定义：用于qcoder模型读取代码 ============

def _ensure_url_scheme(url: Optional[str]) -> Optional[str]:
    """确保URL包含明确的协议(http/https)，缺省时默认http。
    返回规范化后的URL，非字符串或为空则原样返回。
    """
    if not isinstance(url, str) or not url:
        return url
    u = url.strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    # 处理以 // 开头的协议相对URL
    if u.startswith("//"):
        return "http:" + u
    return "http://" + u

def get_code_reading_tools() -> List[Dict[str, Any]]:
    """定义两个工具：读取文件和读取目录，供qcoder模型使用"""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取指定文件的完整内容。用于阅读代码文件以理解实现细节、API参数、输入输出等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "文件的相对路径（相对于代码库根目录）或绝对路径"
                        },
                        "max_bytes": {
                            "type": "integer",
                            "description": "最大读取字节数，默认为500000（50万字节）",
                            "default": 500000
                        }
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_directory",
                "description": "列出目录下的所有文件（递归），并可选地返回关键文件的内容摘要。用于探索代码库结构，找到相关的API文件、路由文件、控制器等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory_path": {
                            "type": "string",
                            "description": "目录的相对路径（相对于代码库根目录）或绝对路径，默认为根目录 '.'"
                        },
                        "include_content": {
                            "type": "boolean",
                            "description": "是否包含文件内容预览（前200行），默认false仅返回文件列表",
                            "default": False
                        },
                        "file_patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选的文件名模式过滤（如['controller', 'route', 'api']），默认返回所有文件"
                        }
                    },
                    "required": []
                }
            }
        }
    ]


def execute_code_reading_tool(tool_name: str, arguments: Dict[str, Any], repo_root: str) -> str:
    """执行代码阅读工具
    
    Args:
        tool_name: 工具名称 ('read_file' 或 'read_directory')
        arguments: 工具参数
        repo_root: 代码库根目录
        
    Returns:
        工具执行结果（字符串）
    """
    try:
        if tool_name == "read_file":
            file_path = arguments.get("file_path", "")
            max_bytes = arguments.get("max_bytes", 500000)
            
            if not file_path:
                return "错误: file_path 参数不能为空"
            
            # 如果是相对路径，拼接repo_root
            if not os.path.isabs(file_path):
                abs_path = os.path.join(repo_root, file_path)
            else:
                abs_path = file_path
            
            if not os.path.exists(abs_path):
                return f"错误: 文件不存在: {file_path}"
            
            if not os.path.isfile(abs_path):
                return f"错误: 不是文件: {file_path}"
            
            content = read_text(abs_path, max_bytes=max_bytes)
            if not content:
                return f"文件 {file_path} 内容为空或无法读取"
            
            file_size = os.path.getsize(abs_path)
            return f"文件: {file_path}\n大小: {file_size} 字节\n内容:\n{content}"
        
        elif tool_name == "read_directory":
            directory_path = arguments.get("directory_path", ".")
            include_content = arguments.get("include_content", False)
            file_patterns = arguments.get("file_patterns", [])
            
            # 如果是相对路径，拼接repo_root
            if not os.path.isabs(directory_path):
                abs_dir = os.path.join(repo_root, directory_path)
            else:
                abs_dir = directory_path
            
            if not os.path.exists(abs_dir):
                return f"错误: 目录不存在: {directory_path}"
            
            if not os.path.isdir(abs_dir):
                return f"错误: 不是目录: {directory_path}"
            
            # 获取所有文件（递归）
            all_files = []
            for root, dirs, files in os.walk(abs_dir):
                # 排除常见的不需要遍历的目录
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", "dist", "build", ".next"}]
                for file in files:
                    rel_path = os.path.relpath(os.path.join(root, file), repo_root)
                    all_files.append(rel_path)
            
            # 应用文件模式过滤
            if file_patterns:
                filtered_files = []
                for f in all_files:
                    f_lower = f.lower()
                    if any(pattern.lower() in f_lower for pattern in file_patterns):
                        filtered_files.append(f)
                all_files = filtered_files
            
            result = f"目录: {directory_path}\n找到 {len(all_files)} 个文件:\n\n"
            
            if include_content:
                # 返回文件列表和关键文件的前200行内容
                for f in all_files[:20]:  # 最多处理20个文件
                    abs_file = os.path.join(repo_root, f)
                    if os.path.exists(abs_file) and os.path.isfile(abs_file):
                        content = read_text(abs_file, max_bytes=20000)  # 最多20KB
                        lines = content.splitlines()[:200]  # 前200行
                        result += f"\n{'='*60}\n文件: {f}\n{'-'*60}\n"
                        result += "\n".join(lines)
                        result += f"\n{'='*60}\n"
            else:
                # 仅返回文件列表
                for f in all_files[:100]:  # 最多返回100个文件
                    result += f"{f}\n"
                if len(all_files) > 100:
                    result += f"\n... 还有 {len(all_files) - 100} 个文件未显示\n"
            
            return result
        
        else:
            return f"错误: 未知的工具名称: {tool_name}"
    
    except Exception as e:
        log.exception(f"执行工具 {tool_name} 时出错")
        return f"错误: {str(e)}"


def deep_read_code_for_feature(
    driver: ModelDriver,
    feature: Dict[str, Any],
    repo_root: str,
    reading_log_path: Optional[str] = None
) -> Dict[str, Any]:
    """让qcoder模型深度阅读代码，完整理解功能实现
    
    使用工具调用让模型主动探索和阅读相关代码文件，确保对功能有完整理解。
    阅读过程会记录到md文件中。
    
    Args:
        driver: ModelDriver实例
        feature: 功能描述和实现位置
        repo_root: 代码库根目录
        reading_log_path: 阅读记录文件的路径，如果为None则自动生成
        
    Returns:
        包含理解摘要和阅读历史的字典
    """
    start_time = time.time()
    description = feature.get("feature_description", "")
    impls = feature.get("implementation_location", [])
    
    log.info("[deep_read_code] 开始深度阅读代码: %s", description[:80])
    
    # 准备阅读记录文件路径
    if reading_log_path is None:
        reading_log_dir = os.path.join(repo_root, "code_reading_logs")
        os.makedirs(reading_log_dir, exist_ok=True)
        # 使用功能描述的slug作为文件名
        slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", description.strip())[:50] or "feature"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        reading_log_path = os.path.join(reading_log_dir, f"reading_{slug}_{timestamp}.md")
    
    # 初始化阅读记录
    reading_log_content = []
    reading_log_content.append(f"# 代码深度阅读记录\n\n")
    reading_log_content.append(f"**功能描述**: {description}\n\n")
    reading_log_content.append(f"**开始时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    reading_log_content.append(f"**实现位置**:\n")
    for impl in impls:
        reading_log_content.append(f"- 文件: {impl.get('file', '')}, 函数: {impl.get('function', '')}, 行数: {impl.get('lines', '')}\n")
    reading_log_content.append(f"\n---\n\n")
    
    # 先获取代码库的目录结构，让模型有全局视图
    try:
        all_files = walk_repository(repo_root)
        # 只显示源代码文件，过滤掉 node_modules、dist 等
        source_files = [
            os.path.relpath(f, repo_root) 
            for f in all_files 
            if any(f.endswith(ext) for ext in ['.ts', '.js', '.py', '.go', '.java', '.graphql', '.gql'])
        ][:200]  # 最多显示200个文件
        directory_structure = "\n".join(sorted(source_files))
    except Exception as e:
        log.warning("Failed to get directory structure: %s", e)
        directory_structure = "无法获取目录结构"
    
    # 构建初始提示
    impl_files = [impl.get("file", "") for impl in impls if impl.get("file")]
    initial_prompt = f"""你是一位资深的代码审查专家。请完整理解以下功能的代码实现。

**功能描述**: {description}

**已知实现位置参考**:
{json.dumps(impls[:10], ensure_ascii=False, indent=2)}

**代码库目录结构（源文件）**:
{directory_structure[:3000]}

**你的任务流程**:
1. **先探索目录结构**: 使用 read_directory 工具了解项目结构，找到相关目录
2. **自主决定阅读顺序**: 根据功能描述，判断需要阅读哪些文件（不限于已知实现位置）
   - 优先阅读：路由定义、控制器、resolver、schema
   - 其次阅读：服务层、DTO/Input类型定义、Entity
   - 必要时阅读：配置文件、工具函数
3. **使用工具主动探索**: 
   - read_file: 读取具体文件内容
   - read_directory: 浏览目录结构，可指定 file_patterns 过滤
4. **持续迭代直到完全理解**:
   - Web API的完整URL路径（包括协议 http/https）
   - HTTP方法（GET/POST/PUT/DELETE/PATCH）
   - 请求格式（JSON、表单、GraphQL query/mutation）
   - 请求参数（headers、query params、request body的确切字段名）
   - 响应格式和输出字段
   - 业务逻辑流程
5. **确认理解后输出详细的 JSON 规范**（仅输出 JSON，不要其他文字）:
   {{
     "url": "完整的请求地址，如 http://localhost:3000/graphql",
     "protocol": "http" 或 "https",
     "method": "GET" | "POST" | "PUT" | "DELETE" | "PATCH",
     "headers": {{"Content-Type": "application/json"}},
     "params_format": "json" | "form" | "query" | "graphql",
     "query_params": {{}} 或 null,
     "json_body": {{
       "query": "完整的 GraphQL query/mutation 字符串（对于 GraphQL）",
       "variables": {{
         "inputVariableName": {{
           "field1": "示例值（注明类型）",
           "field2": "示例值（注明类型）"
         }}
       }}
     }} 或其他格式的 body,
     "input_fields": [
       {{
         "name": "字段名",
         "type": "String | Int | Boolean | ID 等",
         "required": true/false,
         "description": "字段说明",
         "example_value": "示例值"
       }}
     ],
     "response_fields": [
       {{
         "name": "字段名",
         "type": "类型",
         "path": "在响应中的路径（如 data.createChannel.id）"
       }}
     ],
     "data_dependencies": [
       "需要预先存在的数据说明（如：需要先创建 channel）"
     ],
     "expected_status": 200,
     "expected_contains": "响应中应包含的字符串",
     "expected_path": "JSON path（如 data.createChannel.id）"
   }}

**关键要求**:
- 必须从 DTO/Input/Args 类中准确提取所有字段定义
- 明确标注哪些字段是必需(required)的，哪些是可选的
- 提供每个字段的示例值（基于类型和验证规则）
- 识别数据依赖（如创建消息需要 channelId，则需要先有 channel）
- 对于 GraphQL，完整写出 mutation/query 字符串，包含所有必需字段
- 严禁臆造字段名，所有字段必须在代码中找到定义

请开始探索。"""
    
    # 包装工具执行函数
    def tool_executor(tool_name: str, arguments: Dict[str, Any]) -> str:
        result = execute_code_reading_tool(tool_name, arguments, repo_root)
        # 记录到md文件
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reading_log_content.append(f"\n## 工具调用: {timestamp}\n\n")
        reading_log_content.append(f"**工具**: {tool_name}\n\n")
        reading_log_content.append(f"**参数**:\n```json\n{json.dumps(arguments, ensure_ascii=False, indent=2)}\n```\n\n")
        reading_log_content.append(f"**结果**:\n```\n{result[:5000]}\n```\n\n")  # 限制结果长度，避免文件过大
        reading_log_content.append(f"---\n\n")
        return result
    
    # 获取工具定义
    tools = get_code_reading_tools()
    
    # 调用模型进行工具对话
    try:
        system_prompt = """你是一位代码分析专家。请使用提供的工具仔细阅读和理解代码。你的目标是完整理解功能的实现细节，特别是API的参数、路径、输入输出等。请主动探索相关文件，不要遗漏重要信息。"""
        
        final_response, tool_history = driver.agent_chat_with_tools(
            initial_prompt=initial_prompt,
            tools=tools,
            tool_executor_func=tool_executor,
            system=system_prompt,
            max_iterations=15,  # 允许最多15次工具调用
            max_tokens=4000
        )
        
        # 记录最终响应
        reading_log_content.append(f"\n## 模型最终理解总结\n\n")
        reading_log_content.append(f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        reading_log_content.append(f"**原始输出**:\n\n{final_response}\n\n")

        # 解析结构化端点规范
        endpoint_spec = driver.extract_json(final_response or "") or {}
        if isinstance(endpoint_spec, dict):
            # 规范化协议与URL
            url_val = endpoint_spec.get("url")
            if url_val:
                endpoint_spec["url"] = _ensure_url_scheme(url_val)
                endpoint_spec["protocol"] = "https" if str(endpoint_spec["url"]).startswith("https://") else "http"
        else:
            endpoint_spec = {}

        reading_log_content.append("\n## 解析后的端点规范(JSON)\n\n")
        try:
            reading_log_content.append("```json\n" + json.dumps(endpoint_spec, ensure_ascii=False, indent=2) + "\n```\n\n")
        except Exception:
            reading_log_content.append("<无法序列化端点规范>\n\n")
        
        # 额外记录字段信息（便于调试）
        if isinstance(endpoint_spec, dict):
            input_fields = endpoint_spec.get("input_fields") or []
            response_fields = endpoint_spec.get("response_fields") or []
            data_deps = endpoint_spec.get("data_dependencies") or []
            
            if input_fields:
                reading_log_content.append("\n### 提取的输入字段\n\n")
                reading_log_content.append("| 字段名 | 类型 | 必需 | 示例值 | 说明 |\n")
                reading_log_content.append("|--------|------|------|--------|------|\n")
                for field in input_fields:
                    name = field.get("name", "")
                    ftype = field.get("type", "")
                    required = "是" if field.get("required") else "否"
                    example = str(field.get("example_value", ""))
                    desc = field.get("description", "")
                    reading_log_content.append(f"| {name} | {ftype} | {required} | {example} | {desc} |\n")
                reading_log_content.append("\n")
            
            if response_fields:
                reading_log_content.append("\n### 响应字段\n\n")
                for field in response_fields:
                    reading_log_content.append(f"- `{field.get('path')}`: {field.get('type')}\n")
                reading_log_content.append("\n")
            
            if data_deps:
                reading_log_content.append("\n### 数据依赖\n\n")
                for dep in data_deps:
                    reading_log_content.append(f"- {dep}\n")
                reading_log_content.append("\n")
        
        # 记录工具调用历史摘要
        reading_log_content.append(f"\n## 工具调用历史摘要\n\n")
        reading_log_content.append(f"共调用 {len(tool_history)} 次工具\n\n")
        for idx, call in enumerate(tool_history, 1):
            reading_log_content.append(f"{idx}. **{call.get('tool', 'unknown')}**\n")
            reading_log_content.append(f"   参数: {json.dumps(call.get('arguments', {}), ensure_ascii=False)}\n")
            reading_log_content.append(f"   结果摘要: {call.get('result', '')[:200]}...\n\n")
        
        reading_log_content.append(f"\n**结束时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        reading_log_content.append(f"**总耗时**: {time.time() - start_time:.2f} 秒\n")
        
        # 写入md文件
        try:
            with open(reading_log_path, "w", encoding="utf-8") as f:
                f.write("".join(reading_log_content))
            log.info("[deep_read_code] 阅读记录已保存到: %s", reading_log_path)
        except Exception as e:
            log.warning("[deep_read_code] 保存阅读记录失败: %s", e)
        
        elapsed = time.time() - start_time
        log.info("[deep_read_code] 完成深度阅读 (耗时: %.2f秒, 工具调用: %d次)", elapsed, len(tool_history))
        
        return {
            "understanding_summary": final_response,
            "endpoint_spec": endpoint_spec,
            "tool_call_history": tool_history,
            "reading_log_path": reading_log_path,
            "success": True
        }
    
    except Exception as e:
        log.exception("[deep_read_code] 深度阅读过程出错")
        # 记录错误
        reading_log_content.append(f"\n## 错误\n\n")
        reading_log_content.append(f"**错误信息**: {str(e)}\n\n")
        reading_log_content.append(f"**结束时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        try:
            with open(reading_log_path, "w", encoding="utf-8") as f:
                f.write("".join(reading_log_content))
        except:
            pass
        
        return {
            "understanding_summary": "",
            "tool_call_history": [],
            "reading_log_path": reading_log_path,
            "success": False,
            "error": str(e)
        }


def should_test_feature(feature: Dict[str, Any], repo_root: str) -> bool:
    """判断某个功能是否需要测试
    
    过滤规则：
    - 跳过空的实现位置
    - 跳过只有类型定义的文件
    - 跳过空函数
    - 跳过配置文件
    - 跳过工具函数文件
    
    Args:
        feature: 功能分析结果，包含 feature_description 和 implementation_location
        repo_root: 代码库根目录
        
    Returns:
        bool: True 表示需要测试，False 表示跳过
    """
    impls = feature.get("implementation_location", [])
    
    # 没有实现位置，跳过
    if not impls:
        log.debug("Skip feature (no implementation): %s", feature.get("feature_description", ""))
        return False
    
    # 检查每个实现位置
    has_testable_impl = False
    
    for impl in impls:
        file_path = impl.get("file", "")
        function_name = impl.get("function", "")
        
        if not file_path:
            continue
        
        # 跳过类型定义文件
        file_lower = file_path.lower()
        if any(pattern in file_lower for pattern in [
            ".d.ts",           # TypeScript 类型定义
            "types.ts",        # 类型文件
            "interfaces.ts",   # 接口文件
            "dto.ts",          # DTO 类型
            "entity.ts",       # 实体定义
            "schema.ts",       # Schema 定义
            "model.ts",        # 模型定义（可能只是数据结构）
            "constants.ts",    # 常量定义
            "config.ts",       # 配置文件
            "config.js",
            "config.py",
            ".config.",
        ]):
            log.debug("Skip file (type/config): %s", file_path)
            continue
        
        # 跳过明显的工具函数文件（通常不需要测试）
        if any(pattern in file_lower for pattern in [
            "util.ts", "utils.ts", "helper.ts", "helpers.ts",
            "util.js", "utils.js", "helper.js", "helpers.js",
        ]):
            log.debug("Skip file (utility): %s", file_path)
            continue
        
        # 检查函数名
        if function_name:
            func_lower = function_name.lower()
            
            # 跳过构造函数、getter/setter
            if func_lower in ["constructor", "__init__", "getters", "setters"]:
                log.debug("Skip function (constructor/getter/setter): %s", function_name)
                continue
            
            # 跳过简单的 getter 方法
            if func_lower.startswith("get") and len(func_lower) < 15:
                # 简单的 getter 可能不需要测试
                pass
        
        # 读取文件内容，检查是否是空函数或只有类型定义
        abs_path = os.path.join(repo_root, file_path)
        if os.path.exists(abs_path):
            content = read_text(abs_path, max_bytes=10000)
            if content:
                # 检查是否主要是类型定义
                lines = content.splitlines()
                code_lines = [l for l in lines if l.strip() and not l.strip().startswith("//") and not l.strip().startswith("#")]
                
                if len(code_lines) < 5:
                    log.debug("Skip file (too short): %s", file_path)
                    continue
                
                # 统计类型定义关键字
                type_keywords = ["interface", "type ", "enum ", "export type", "export interface"]
                type_count = sum(1 for line in code_lines if any(kw in line for kw in type_keywords))
                
                # 如果大部分是类型定义，跳过
                if type_count > len(code_lines) * 0.6:
                    log.debug("Skip file (mostly type definitions): %s", file_path)
                    continue
                
                # 检查是否是空函数（只有声明，没有实现）
                if function_name:
                    # 查找函数定义
                    func_pattern = rf"\b{re.escape(function_name)}\b.*\{{.*?\}}"
                    func_match = re.search(func_pattern, content, re.DOTALL)
                    if func_match:
                        func_body = func_match.group(0)
                        # 去除注释和空行
                        body_lines = [
                            l.strip() 
                            for l in func_body.splitlines() 
                            if l.strip() and not l.strip().startswith("//") and not l.strip().startswith("#")
                        ]
                        # 如果函数体只有开闭括号，是空函数
                        if len(body_lines) <= 3:  # function name {, }, and maybe one line
                            log.debug("Skip function (empty body): %s in %s", function_name, file_path)
                            continue
        
        # 如果通过所有检查，标记为可测试
        has_testable_impl = True
        break
    
    if has_testable_impl:
        log.info("Feature needs testing: %s", feature.get("feature_description", ""))
    else:
        log.info("Skip feature (no testable impl): %s", feature.get("feature_description", ""))
    
    return has_testable_impl


def _infer_endpoint_info(feature: Dict[str, Any], repo_root: str) -> Optional[Dict[str, Any]]:
    """基于实现文件的内容，推断测试目标的 URL/方法/参数与期望。

    返回示例：
    {
        "url": "http://localhost:3000/graphql",
        "method": "POST",
        "params_hint": "{ query: 'mutation {...}', variables: {...} }",
        "expected_hint": "HTTP 200 and data field present",
        "type": "graphql" | "rest"
    }
    无法推断时返回 None。
    """
    impls = feature.get("implementation_location", []) or []
    files_checked = 0
    controller_base: Optional[str] = None
    method_path: Optional[str] = None
    method_http: Optional[str] = None
    is_graphql = False

    for impl in impls[:5]:
        file_path = impl.get("file", "") or ""
        if not file_path:
            continue
        abs_path = os.path.join(repo_root, file_path)
        if not os.path.exists(abs_path):
            continue
        content = read_text(abs_path, max_bytes=20000) or ""
        files_checked += 1

        lower_name = os.path.basename(file_path).lower()
        if "resolver" in lower_name or "graphql" in lower_name or "gql" in content or "GraphQL" in content:
            is_graphql = True

        # NestJS Controller 路由
        try:
            # @Controller('base')
            m = re.search(r"@Controller\(([^)]+)\)", content)
            if m:
                base_raw = m.group(1).strip().strip("`'\"")
                if base_raw:
                    controller_base = base_raw if base_raw.startswith("/") else f"/{base_raw}"
            # @(Get|Post|Put|Delete)('path')
            m2 = re.search(r"@(Get|Post|Put|Delete)\(([^)]*)\)", content)
            if m2:
                method_http = m2.group(1).upper()
                p_raw = (m2.group(2) or "").strip().strip("`'\"")
                if p_raw:
                    method_path = p_raw if p_raw.startswith("/") else f"/{p_raw}"
        except Exception:
            pass

        # Express/Koa 简单匹配 app.get('/path')
        if not method_http or not method_path:
            m3 = re.search(r"\.\s*(get|post|put|delete)\s*\(\s*['\"]([^'\"]+)['\"]", content, re.IGNORECASE)
            if m3:
                method_http = m3.group(1).upper()
                method_path = m3.group(2)
                if not method_path.startswith("/"):
                    method_path = f"/{method_path}"

    # GraphQL 推断
    if is_graphql:
        return {
            "url": "http://localhost:3000/graphql",
            "method": "POST",
            "params_hint": "{ query: 'query or mutation string', variables: {...} }",
            "expected_hint": "HTTP 200 and JSON with data.* non-null",
            "type": "graphql",
        }

    # REST 推断
    if method_http and (method_path or controller_base):
        full_path = (controller_base or "") + (method_path or "")
        # 规整双斜杠
        full_path = re.sub(r"//+", "/", full_path or "/")
        if not full_path.startswith("/"):
            full_path = "/" + full_path
        return {
            "url": f"http://localhost:3000{full_path}",
            "method": method_http,
            "params_hint": "JSON body or query params as required by the endpoint",
            "expected_hint": "HTTP 200 and valid JSON body",
            "type": "rest",
        }

    return None


def _collect_impl_snippets(feature: Dict[str, Any], repo_root: str, max_files: int = 5, max_bytes: int = 20000) -> List[Dict[str, str]]:
    impls = feature.get("implementation_location", []) or []
    snippets: List[Dict[str, str]] = []
    for impl in impls[:max_files]:
        file_path = impl.get("file", "") or ""
        abs_path = os.path.join(repo_root, file_path) if file_path else ""
        snippet = ""
        if abs_path and os.path.exists(abs_path):
            try:
                content = read_text(abs_path, max_bytes=max_bytes) or ""
                lines = content.splitlines()
                snippet = "\n".join(lines[:200])
            except Exception:
                snippet = ""
        snippets.append({
            "file": file_path,
            "function": impl.get("function", "") or "",
            "lines": impl.get("lines", "") or "",
            "snippet": snippet,
        })
    # 额外尝试读取 GraphQL schema.gql（若存在）
    schema_path = os.path.join(repo_root, "schema.gql")
    if os.path.exists(schema_path):
        try:
            text = read_text(schema_path, max_bytes=max_bytes) or ""
            if text:
                snippets.append({"file": "schema.gql", "function": "", "lines": "", "snippet": text[:4000]})
        except Exception:
            pass
    return snippets


def _augment_endpoint_with_llm(driver: ModelDriver, feature: Dict[str, Any], repo_root: str, base_ep: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """使用 LLM 在代码上下文中推断完整端点细节（URL/Method/Headers/Params/Expected）"""
    snippets = _collect_impl_snippets(feature, repo_root)
    description = feature.get("feature_description", "")
    impl_info = []
    for impl in (feature.get("implementation_location", []) or [])[:5]:
        impl_info.append({
            "file": impl.get("file", ""),
            "function": impl.get("function", ""),
            "lines": impl.get("lines", ""),
        })

    base_ep = base_ep or {}
    prompt = (
        "You are an API inference assistant. From the following code and schema snippets, "
        "produce a STRICT JSON spec for one endpoint to test this feature.\n\n"
        f"Feature: {description}\n\n"
        f"Implementations: {json.dumps(impl_info, ensure_ascii=False)}\n\n"
        f"Snippets (truncated): {json.dumps(snippets, ensure_ascii=False)[:5000]}\n\n"
        "Rules:\n"
        "- If GraphQL, URL is http://localhost:3000/graphql, method POST, body has { query, variables }.\n"
        "- If REST (NestJS/Express), infer path from @Controller and @Get/@Post, build full URL with http://localhost:3000.\n"
        "- Infer headers (e.g., {'Content-Type':'application/json'} for JSON).\n"
        "- Provide concrete example parameters (json_body for POST/PUT, or query_params for GET).\n"
        "- Provide expected assertions (expected_status, and expected_contains: a string that should appear in response body, or expected_path: a json path existing).\n"
        "- CRITICAL: Do NOT invent or guess parameter/header names. Only use names/types you can see in the snippets (resolver, DTO/Input/Args, schema.gql). If uncertain, set the value to null.\n"
        "- For GraphQL variables, only use variable names/types defined by resolver/@Args and InputType/schema. Do not add extra fields.\n"
        "- Output ONLY JSON with keys: url, method, headers, query_params, json_body, expected_status, expected_contains, expected_path. Missing/unknown values must be null. No extra text.\n"
    )

    resp = driver.chat(prompt, system="Output ONLY JSON with exact keys. Do not invent fields; use null if unknown.", max_tokens=800)
    spec = driver.extract_json(resp or "") if resp else None
    if not isinstance(spec, dict):
        return base_ep or None

    # 合并 base_ep 和 LLM spec（LLM 优先填充缺失值）
    merged = dict(base_ep)
    for k in ["url", "method", "headers", "query_params", "json_body", "expected_status", "expected_contains", "expected_path"]:
        if k not in merged or not merged.get(k):
            merged[k] = spec.get(k)
    # 规范化URL协议
    if merged.get("url"):
        merged["url"] = _ensure_url_scheme(merged.get("url"))
    return merged

def filter_features_by_llm(driver: ModelDriver, features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """使用 LLM 从 feature_analysis 中筛选"开发功能/模块"，剔除部署/文档相关项
    
    规则（交由 LLM 执行）：
    - 保留：业务逻辑、API、模块/类/函数实现、数据库读写逻辑、控制器/服务、路由处理等
    - 排除：部署、CI/CD、Docker、K8s、脚本、打包/构建配置、Lint/格式化、README/文档、Issue/模板、LICENSE
    
    LLM 输出：仅输出 JSON 数组，内容为要保留的索引列表（从 0 开始）。
    若解析失败，将回退到全部保留。
    """
    start_time = time.time()
    log.info("[filter_features_by_llm] 开始过滤 %d 个特性...", len(features))
    
    # 构造简化的列表，避免上下文过长
    summarized = []
    for idx, f in enumerate(features[:50]):  # 最多取前 50 项，避免超长
        impls = f.get("implementation_location", []) or []
        files = [impl.get("file", "") for impl in impls if impl.get("file")]
        summarized.append({
            "index": idx,
            "feature_description": f.get("feature_description", "")[:300],
            "files": files[:5]  # 每项最多显示 5 个文件
        })

    instruction = (
        "You are a code triage expert. From the following feature list, select ONLY features that represent "
        "EXECUTABLE RUNTIME CODE that performs business operations or data transformations.\n\n"
        "INCLUDE (code that runs and does work):\n"
        "- Features that process requests, manipulate data, or execute business logic\n"
        "- Code that handles user interactions, API endpoints, database operations\n"
        "- Functions/methods that transform inputs into outputs\n"
        "- Services, controllers, resolvers, handlers that implement functional requirements\n\n"
        "EXCLUDE (non-executable artifacts, meta-information, or development tooling):\n"
        "- Documentation, specifications, or schemas that describe the system but don't execute\n"
        "- Configuration files, environment setup, or deployment definitions\n"
        "- Build scripts, CI/CD pipelines, or development workflow automation\n"
        "- Testing frameworks, linters, formatters, or other development tools\n"
        "- Static type definitions, interfaces, or data models without logic\n"
        "- Error handling patterns or logging configurations (unless they're the primary feature)\n\n"
        "Key principle: If it's not code that RUNS to accomplish a user-facing or system function, exclude it.\n\n"
        "Return ONLY a JSON array of indices (0-based) to keep, no explanation."
    )

    prompt = f"""
{instruction}

Feature list (truncated):
{json.dumps(summarized, ensure_ascii=False, indent=2)}
"""

    response = driver.chat(prompt, system="Output ONLY JSON array of indices, no extra text.")

    try:
        # 清理可能的代码块
        if response and response.strip().startswith("```"):
            lines = response.strip().split("\n")
            response = "\n".join(lines[1:-1]) if len(lines) > 2 else response
        indices = json.loads(response or "[]")
        if not isinstance(indices, list):
            raise ValueError("LLM did not return a list")
        keep = []
        for i in indices:
            if isinstance(i, int) and 0 <= i < len(features):
                keep.append(features[i])
        elapsed = time.time() - start_time
        log.info("[filter_features_by_llm] 完成: %d -> %d 个特性 (耗时: %.2f秒)", len(features), len(keep), elapsed)
        return keep if keep else features
    except Exception:
        # 回退到全部保留，后续再用 should_test_feature 做细粒度过滤
        elapsed = time.time() - start_time
        log.warning("[filter_features_by_llm] 过滤失败，保留全部 (耗时: %.2f秒)", elapsed)
        return features


def generate_test_code(
    driver: ModelDriver,
    feature: Dict[str, Any],
    repo_root: str,
    language: str = "javascript",
    endpoint_info: Optional[Dict[str, Any]] = None,
    qcoder: bool = True,
    enable_deep_reading: bool = True,
    deep_reading_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """为单个功能生成测试代码和运行命令
    
    Args:
        driver: LLM 驱动器
        feature: 功能描述和实现位置
        repo_root: 代码库根目录
        language: 目标语言
        endpoint_info: 端点信息（可选）
        qcoder: 是否使用qcoder模型
        enable_deep_reading: 是否启用深度阅读（默认True）
        deep_reading_result: 深度阅读的结果（如果已经执行过，可传入避免重复）
        
    Returns:
        字典，包含 'test_code' 和 'run_command' 两个键
    """
    start_time = time.time()
    description = feature.get("feature_description", "")
    impls = feature.get("implementation_location", [])
    
    log.info("[generate_test_code] 开始生成测试: %s", description[:80])
    
    # Step 1: 深度阅读代码（如果启用且未提供结果）
    deep_reading_summary = ""
    if enable_deep_reading and qcoder:  # 只在qcoder模式下启用
        if deep_reading_result is None:
            log.info("[generate_test_code] 开始深度阅读代码...")
            deep_reading_result = deep_read_code_for_feature(driver, feature, repo_root)
            deep_reading_summary = deep_reading_result.get("understanding_summary", "")
            try:
                log.info(
                    "[generate_test_code] deep_reading_result: %s",
                    (json.dumps({
                        "success": deep_reading_result.get("success"),
                        "reading_log_path": deep_reading_result.get("reading_log_path"),
                        "summary_preview": (deep_reading_summary[:300] if isinstance(deep_reading_summary, str) else "")
                    }, ensure_ascii=False)[:1200])
                )
            except Exception:
                pass
        else:
            deep_reading_summary = deep_reading_result.get("understanding_summary", "")
            log.info("[generate_test_code] 使用已提供的深度阅读结果")
            try:
                log.info(
                    "[generate_test_code] provided deep_reading_result: %s",
                    (json.dumps({
                        "success": deep_reading_result.get("success"),
                        "reading_log_path": deep_reading_result.get("reading_log_path"),
                        "summary_preview": (deep_reading_summary[:300] if isinstance(deep_reading_summary, str) else "")
                    }, ensure_ascii=False)[:1200])
                )
            except Exception:
                pass
    else:
        log.info("[generate_test_code] 跳过深度阅读（enable_deep_reading=%s, qcoder=%s）", enable_deep_reading, qcoder)
    
    # 构建实现信息
    impl_info = []
    for impl in impls[:3]:  # 最多取前3个实现
        impl_info.append({
            "file": impl.get("file", ""),
            "function": impl.get("function", ""),
            "lines": impl.get("lines", "")
        })
    
    # 尝试读取部分代码作为上下文（如果深度阅读未启用，则提供部分代码片段）
    code_context = ""
    if not deep_reading_summary:
        # 未进行深度阅读时，提供部分代码片段作为上下文
        code_snippets = []
        for impl in impls[:2]:
            file_path = impl.get("file", "")
            if not file_path:
                continue
            
            abs_path = os.path.join(repo_root, file_path)
            if os.path.exists(abs_path):
                content = read_text(abs_path, max_bytes=5000)
                if content:
                    # 提取关键部分
                    lines = content.splitlines()[:50]  # 前50行
                    code_snippets.append(f"// From {file_path}\n" + "\n".join(lines))
        
        code_context = "\n\n".join(code_snippets) if code_snippets else "No code available"
    
    # 推断目标端点（若未提供）
    #ep = endpoint_info or _infer_endpoint_info(feature, repo_root)
    ep = endpoint_info
    # 如有深度阅读的结构化端点规范，优先合并使用
    if deep_reading_result and isinstance(deep_reading_result, dict):
        dr_spec = deep_reading_result.get("endpoint_spec") or {}
        if isinstance(dr_spec, dict) and dr_spec:
            try:
                # 规范化URL与协议
                if dr_spec.get("url"):
                    dr_spec["url"] = _ensure_url_scheme(dr_spec.get("url"))
                # 合并（深度阅读优先）
                ep = ep or {}
                for k_from, k_to in [
                    ("url", "url"),
                    ("method", "method"),
                    ("headers", "headers"),
                    ("query_params", "query_params"),
                    ("json_body", "json_body"),
                    ("expected_status", "expected_status"),
                    ("expected_contains", "expected_contains"),
                    ("expected_path", "expected_path"),
                ]:
                    v = dr_spec.get(k_from)
                    if v is not None and (not ep.get(k_to)):
                        ep[k_to] = v
            except Exception:
                pass
    if not ep:
        log.warning("No endpoint info inferred for feature: %s", description)
        return {"test_code": "", "run_command": "node test.js"}

    target_url = _ensure_url_scheme(ep.get("url", "http://localhost:3000"))
    target_method = ep.get("method", "GET")
    url_protocol = "https" if str(target_url).startswith("https://") else "http"
    
    headers = ep.get("headers") or {"Content-Type": "application/json" if target_method in ("POST", "PUT", "PATCH") else None}
    query_params = ep.get("query_params") or None
    json_body = ep.get("json_body") or None
    params_hint = json.dumps(json_body or query_params or {}, ensure_ascii=False)
    expected_hint = ep.get("expected_hint", "HTTP 200")
    expected_status = ep.get("expected_status") or 200
    expected_contains = ep.get("expected_contains") or ""
    expected_path = ep.get("expected_path") or ""

    # 构建提示词
    # 如果有深度阅读结果，优先使用深度阅读的理解
    if deep_reading_summary:
        # 提取字段定义信息
        dr_spec = (deep_reading_result or {}).get("endpoint_spec") or {}
        input_fields = dr_spec.get("input_fields") or []
        response_fields = dr_spec.get("response_fields") or []
        data_deps = dr_spec.get("data_dependencies") or []
        
        fields_info = ""
        if input_fields:
            fields_info += "\n**输入字段定义**（严格按此使用）:\n"
            for field in input_fields[:20]:  # 最多显示20个字段
                required_mark = "必需" if field.get("required") else "可选"
                fields_info += f"- {field.get('name')}: {field.get('type')} ({required_mark})"
                if field.get("example_value"):
                    fields_info += f" = {field.get('example_value')}"
                if field.get("description"):
                    fields_info += f" // {field.get('description')}"
                fields_info += "\n"
        
        if response_fields:
            fields_info += "\n**响应字段**:\n"
            for field in response_fields[:15]:
                fields_info += f"- {field.get('path')}: {field.get('type')}\n"
        
        if data_deps:
            fields_info += "\n**数据依赖**:\n"
            for dep in data_deps:
                fields_info += f"- {dep}\n"
        
        code_understanding_section = f"""
**深度代码理解（基于完整代码阅读）**:
{deep_reading_summary[:800]}
{fields_info}

**严格要求**: 
- 上述字段定义来自真实代码，必须严格使用，不得修改字段名或添加不存在的字段
- 必需字段必须全部提供，可选字段可以省略
- 示例值(example_value)仅供参考，但类型必须匹配
- 对于 GraphQL，必须使用完整的 mutation/query 字符串（在深度理解中已提供）
- 如有数据依赖，考虑在测试中处理或使用已知存在的测试数据
"""
        code_context_section = ""
    else:
        code_understanding_section = ""
        code_context_section = f"""
Code context (partial):
{code_context[:900]}
"""
    
    prompt = f"""You are a senior QA engineer writing automated tests.

Task: Write a minimal, standalone, runnable Node.js test to verify this feature.

Feature to test: "{description}"

Implementation details:
{json.dumps(impl_info, indent=2)}
{code_understanding_section}
{code_context_section}

Requirements:
1. Write ONLY JavaScript/Node.js code (no TypeScript)
2. Use built-in 'assert' module (no external test frameworks)
3. Test should be completely standalone and runnable
4. The backend service is ALREADY running at http://localhost:3000. DO NOT start or mock any server. DO NOT call http.createServer/express/fastify/etc.
5. Send real HTTP requests as a client only (use built-in 'http' or 'https')
6. Use try-catch to handle errors gracefully
7. Exit with code 0 on success, non-zero on failure
8. Keep it simple - focus on ONE key aspect of the feature
9. **IMPORTANT**: At the end of your code, add a comment line starting with "// RUN:" followed by the command to run this test (e.g., "// RUN: node test.js")
10. Provide directly runnable code with no irrelevant descriptions or characters that could interfere with successful execution.
11. CRITICAL: Do NOT invent or guess any parameters/headers/fields. Use ONLY the values provided below. If a value is null, omit it entirely.
12. For GraphQL:
    - Use the EXACT query/mutation string provided in json_body.query
    - Use the EXACT variable names and structure provided in json_body.variables
    - If input fields are specified above, ensure variables match those field definitions exactly
    - Do NOT add extra fields or modify field names
13. For field values:
    - Use example values provided in "输入字段定义" if available
    - For required fields, MUST provide a value (use example or reasonable test value)
    - For optional fields, can omit or use null
14. Prefer minimal code and the shortest correct implementation.
15. Choose the Node module strictly based on URL scheme: use 'http' for URLs starting with 'http://', and 'https' for 'https://'. Do not use 'https' for 'http://' URLs.
Target endpoint (MANDATORY to use as-is):
- URL protocol: {url_protocol}
- URL: {target_url}
- Method: {target_method}
- Example request params/body: {params_hint}
- Expected result hint: {expected_hint}

Output ONLY the JavaScript code (with RUN comment), no markdown, no explanation.

Target endpoint (MANDATORY to use as-is):
- URL: {target_url}
- Method: {target_method}
- Headers: {json.dumps(headers, ensure_ascii=False)}
- Example query params: {json.dumps(query_params, ensure_ascii=False)}
- Example JSON body: {json.dumps(json_body, ensure_ascii=False)}
- Expected status: {expected_status}
- Expected contains (string hint): {expected_contains}
- Expected JSON path exists (hint): {expected_path}

Example structure:
```
const assert = require('assert');

async function test() {{
    try {{
        // Your test logic here
        assert.strictEqual(1 + 1, 2, 'Math works');
        console.log('Test passed');
        process.exit(0);
    }} catch (err) {{
        console.error('Test failed:', err.message);
        process.exit(1);
    }}
}}

test();
// RUN: node test.js
```

Now write the test:"""


    # 调用 LLM 生成测试代码
    response = driver.chat(
        prompt,
        qcoder=qcoder,
        system="You are a QA engineer. Output only code with RUN comment. Do not invent parameters; use only provided endpoint spec.",
        max_tokens=1100,
    )
    
    if not response:
        log.warning("LLM returned empty response for test generation")
        return {"test_code": "", "run_command": "node test.js"}
    
    # 提取代码块
    test_code = driver.extract_code_block(response, language="javascript")
    
    # 提取运行命令
    run_command = "node test.js"  # 默认命令
    run_match = re.search(r"//\s*RUN:\s*(.+)$", test_code, re.MULTILINE | re.IGNORECASE)
    if run_match:
        run_command = run_match.group(1).strip()
        log.info("Extracted run command: %s", run_command)
    else:
        log.debug("No RUN comment found, using default: %s", run_command)
    
    elapsed = time.time() - start_time
    log.info("[generate_test_code] 完成生成测试代码 (%d 字符, 耗时: %.2f秒)", len(test_code), elapsed)
    return {"test_code": test_code, "run_command": run_command}


def try_execute_with_repair(
    driver: ModelDriver,
    test_code: str,
    feature_desc: str,
    repo_root: str,
    run_command: str = None,
    feature: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """执行测试代码，如果失败则尝试自修复
    
    Args:
        driver: LLM 驱动器
        test_code: 测试代码
        feature_desc: 功能描述
        repo_root: 代码库根目录
        run_command: 运行命令（可选）
        
    Returns:
        执行结果字典，包含 tests_passed 和 log
    """
    start_time = time.time()
    log.info("[try_execute_with_repair] 开始执行测试 (最大尝试 %d 次)", MAX_RETRIES)
    current_code = test_code

    # 准备测试输出目录
    tests_dir = os.path.join(repo_root, "verification_tests")
    try:
        os.makedirs(tests_dir, exist_ok=True)
    except Exception:
        # 如果创建失败，退回到 repo 根目录
        tests_dir = repo_root

    # 简单 slug 化 feature 名称用于文件名
    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", (feature_desc or "feature").strip())[:50] or "feature"
    
    for attempt in range(1, MAX_RETRIES + 1):
        log.info("[%s] Attempt %d/%d", feature_desc[:50], attempt, MAX_RETRIES)
        
        # 为本次尝试持久化测试文件
        test_file_path = os.path.join(tests_dir, f"test_{slug}_attempt{attempt}.js")
        log.info("[%s] Writing test file: %s", feature_desc[:50], test_file_path)

        # 解析将要执行的命令（用于日志）
        resolved_cmd = None
        if run_command:
            try:
                resolved_cmd = run_command.replace("test.js", test_file_path)
            except Exception:
                resolved_cmd = run_command

        if resolved_cmd:
            log.info("[%s] Run command: %s", feature_desc[:50], resolved_cmd)
        else:
            log.info("[%s] Run command: node %s", feature_desc[:50], test_file_path)

        # 执行测试
        result = execute_js_test(
            current_code,
            cwd=repo_root,
            run_command=run_command,
            persist_path=test_file_path,
        )
        
        if result["tests_passed"]:
            elapsed = time.time() - start_time
            log.info("[try_execute_with_repair] 测试通过 (尝试 %d 次, 耗时: %.2f秒)", attempt, elapsed)
            result["attempt"] = attempt
            result["final_code"] = current_code
            return result
        
        # 测试失败
        error_log = result.get("log", "")
        log.warning("[%s] Test failed (attempt %d): %s", feature_desc[:50], attempt, error_log[:500])
        
        # 如果是最后一次尝试，直接返回失败
        if attempt >= MAX_RETRIES:
            log.error("[%s] Max retries reached, giving up", feature_desc[:50])
            result["attempt"] = attempt
            result["final_code"] = current_code
            return result
        
        # 收集实现位置及代码片段，提供给修复器
        impls = (feature or {}).get("implementation_location", []) if feature else []
        impl_snippets: list[dict[str, str]] = []
        for impl in impls[:3]:
            file_path = impl.get("file", "") or ""
            abs_path = os.path.join(repo_root, file_path) if file_path else ""
            snippet = ""
            if abs_path and os.path.exists(abs_path):
                try:
                    content = read_text(abs_path, max_bytes=6000) or ""
                    # 取前后若干行，避免过长
                    lines = content.splitlines()
                    snippet = "\n".join(lines[:120])
                except Exception:
                    snippet = ""
            impl_snippets.append({
                "file": file_path,
                "function": impl.get("function", "") or "",
                "lines": impl.get("lines", "") or "",
                "snippet": snippet,
            })

        # 尝试修复代码
        log.info("[%s] Attempting to repair test code...", feature_desc[:50])
        repaired_code = repair_test_code(
            driver=driver,
            failed_code=current_code,
            error_log=error_log,
            feature_desc=feature_desc,
            impl_snippets=impl_snippets,
            test_file_path=test_file_path,
            run_command=resolved_cmd or (f"node {test_file_path}"),
        )
        
        if not repaired_code or repaired_code == current_code:
            log.warning("[%s] Repair returned no change, giving up", feature_desc[:50])
            result["attempt"] = attempt
            result["final_code"] = current_code
            return result
        
        current_code = repaired_code
        log.info("[%s] Test code repaired, retrying...", feature_desc[:50])
    
    # 不应该到达这里
    return {
        "tests_passed": False,
        "log": "Unknown error",
        "attempt": MAX_RETRIES,
        "final_code": current_code
    }


def repair_test_code(
    driver: ModelDriver,
    failed_code: str,
    error_log: str,
    feature_desc: str,
    impl_snippets: List[Dict[str, str]] | None = None,
    test_file_path: str | None = None,
    run_command: str | None = None,
) -> str:
    """使用 LLM 修复失败的测试代码
    
    Args:
        driver: LLM 驱动器
        failed_code: 失败的测试代码
        error_log: 错误日志
        feature_desc: 功能描述
        
    Returns:
        修复后的测试代码
    """
    prompt = f"""You are a debugging expert. A JavaScript test failed to run or crashed.

Feature being tested: {feature_desc}

Failed test code:
```javascript
{failed_code}
```

Error output:
```
{error_log[:800]}
```

 Implementation mapping (from repository):
 {json.dumps(impl_snippets or [], ensure_ascii=False, indent=2)[:2000]}

 Execution context:
 - Test file path: {test_file_path or ''}
 - Run command: {run_command or ''}

Your task: Fix the test code to make it runnable.

Common issues to fix:
1. Missing require() statements
2. Incorrect API endpoints or URLs
3. Wrong assertions
4. Missing error handling
5. Timeout issues
6. Module not found errors - use built-in modules only

Requirements:
1. Keep the test simple and standalone
2. Use ONLY built-in Node.js modules (assert, http, https, fs, etc.)
3. DO NOT use external packages (no axios, supertest, etc.) unless you mock them
4. If testing a real service, assume it's on http://localhost:3000
5. Do NOT invent or guess parameters/headers or change endpoint structures; if a parameter seems wrong, remove it rather than creating a new one
6. If you can't fix it properly, simplify to a basic logic test
7. Must be runnable with just 'node test.js'
8. Choose the Node module strictly based on URL scheme: use 'http' for 'http://', 'https' for 'https://'.

Output ONLY the fixed JavaScript code, no markdown, no explanation.
"""

    response = driver.chat(prompt, system="You are a debugging expert. Output only code. Do not invent parameters.", max_tokens=1000)
    
    if not response:
        log.warning("LLM returned empty response for repair")
        return ""
    
    # 提取代码块
    repaired_code = driver.extract_code_block(response, language="javascript")
    
    log.debug("Repaired test code (%d chars)", len(repaired_code))
    return repaired_code


def run_functional_verification(
    feature_analysis: Dict[str, Any],
    repo_root: str,
    auto_start_service: bool = True,
    test_qcoder: bool = True,
) -> Dict[str, Any]:
    """测试工作流入口（包含服务启动和关闭）
    
    Args:
        feature_analysis: 分析工作流的输出，包含 feature_analysis 列表
        repo_root: 代码库根目录
        auto_start_service: 是否自动启动服务（默认 True）
        
    Returns:
        验证结果字典，包含 functional_verification 列表
    """
    workflow_start = time.time()
    log.info("=" * 80)
    log.info("FUNCTIONAL VERIFICATION: Starting test workflow")
    log.info("=" * 80)
    
    driver = ModelDriver()
    verification_results = []
    service_proc = None
    service_started_by_us = False
    # 保留原始分析报告，最终只是在其基础上追加 functional_verification
    base_report: Dict[str, Any] = dict(feature_analysis or {})
    
    # 记录已测试的端点签名，避免不同需求生成重复用例
    seen_signatures: set[str] = set()

    try:
        # Step 1: 尝试启动服务（如果需要）
        if auto_start_service:
            from app.agents.service_starter import start_service_if_needed, stop_service
            
            log.info("Step 1: Checking if service needs to be started...")
            service_proc, service_started_by_us = start_service_if_needed(repo_root, driver, use_docker=False)
            
            if service_started_by_us:
                log.info("Service started by verification workflow")
            elif service_proc is None:
                log.info("Service not started (may already be running or not needed)")
        
        # Step 2: 获取并过滤功能列表
        features = feature_analysis.get("feature_analysis", [])
        if not features:
            log.warning("No features to test")
            base_report["functional_verification"] = []
            return base_report
        
        log.info("Total features from analysis: %d", len(features))
        
        # 先用 LLM 过滤出“开发功能/模块”，剔除部署/文档类特性
        llm_filtered_features = filter_features_by_llm(driver, features)

        
        testable_features = []
        for feature in llm_filtered_features:
            testable_features.append(feature)
        
        log.info("LLM-selected features: %d -> after heuristics: %d", len(llm_filtered_features), len(testable_features))
        
        if not testable_features:
            log.warning("No testable features found")
            base_report["functional_verification"] = []
            return base_report
        
        # Step 3-4: 为每个功能生成和执行测试（必须具备明确的端点信息，否则跳过）
        for i, feature in enumerate(testable_features[:5], 1):  # 最多测试5个功能
            feature_start = time.time()
            desc = feature.get("feature_description", "")
            log.info("=" * 80)
            log.info("处理特性 %d/%d: %s", i, len(testable_features[:5]), desc[:70])
            log.info("=" * 80)
            
            try:
                # 推断端点信息；若无法给出 URL/方法/参数提示，则在过滤阶段剔除
                endpoint_info = _infer_endpoint_info(feature, repo_root)
                # 若基本推断不足，尝试用 LLM 丰富细节（URL/方法/参数/期望）
                endpoint_info = _augment_endpoint_with_llm(driver, feature, repo_root, endpoint_info)
                if not endpoint_info or not endpoint_info.get("url") or not endpoint_info.get("method"):
                    log.info("Skip feature due to missing endpoint info: %s", desc)
                    continue

                # 去重：对相同端点的测试跳过，但对 GraphQL 使用功能描述区分
                try:
                    url_sig = str(endpoint_info.get("url", "")).strip()
                    method_sig = str(endpoint_info.get("method", "")).strip().upper()
                    
                    # 对于 GraphQL，使用功能描述作为区分（因为都是 POST /graphql）
                    if "graphql" in url_sig.lower():
                        # 使用功能描述的前50个字符作为签名的一部分
                        feature_sig = desc[:50].strip().replace(" ", "_")
                        signature = f"{method_sig}|{url_sig}|feature:{feature_sig}"
                    else:
                        # 对于 REST API，使用参数键
                        qp = endpoint_info.get("query_params") or {}
                        jb = endpoint_info.get("json_body") or {}
                        qp_keys = ",".join(sorted(list(qp.keys()))) if isinstance(qp, dict) else str(qp)[:50]
                        jb_keys = ",".join(sorted(list(jb.keys()))) if isinstance(jb, dict) else str(jb)[:50]
                        signature = f"{method_sig}|{url_sig}|qp:{qp_keys}|jb:{jb_keys}"
                    
                    if signature in seen_signatures:
                        log.info("Skip feature due to duplicate endpoint signature: %s", signature)
                        continue
                    seen_signatures.add(signature)
                except Exception:
                    pass

                # Step 3: 生成测试代码和运行命令（qcoder 由客户端决定，仅影响生成）
                test_result = generate_test_code(driver, feature, repo_root, endpoint_info=endpoint_info, qcoder=test_qcoder)
                test_code = test_result.get("test_code", "")
                run_command = test_result.get("run_command", "node test.js")
                
                if not test_code:
                    log.warning("Failed to generate test for: %s", desc)
                    verification_results.append({
                        "feature": desc,
                        "generated_test_code": "",
                        "run_command": "",
                        "execution_result": {
                            "tests_passed": False,
                            "log": "Failed to generate test code"
                        }
                    })
                    continue
                
             
                # Step 4: 执行测试（带自修复）
                exec_result = try_execute_with_repair(driver, test_code, desc, repo_root, run_command, feature)
                
                # 记录结果
                verification_results.append({
                    "feature": desc,
                    "generated_test_code": exec_result.get("final_code", test_code),
                    "run_command": run_command,
                    "execution_result": {
                        "tests_passed": exec_result.get("tests_passed", False),
                        "log": exec_result.get("log", ""),
                        "attempts": exec_result.get("attempt", 1)
                    }
                })
                
                feature_elapsed = time.time() - feature_start
                status = "通过" if exec_result.get("tests_passed") else "失败"
                log.info("特性 %d 结果: %s (耗时: %.2f秒)", i, status, feature_elapsed)
                
            except Exception as e:
                log.exception("Error processing feature: %s", desc)
                verification_results.append({
                    "feature": desc,
                    "generated_test_code": "",
                    "run_command": "",
                    "execution_result": {
                        "tests_passed": False,
                        "log": f"Error: {str(e)}"
                    }
                })
    
    finally:
        # Step 5: 关闭服务（如果是由我们启动的）
        if service_started_by_us and service_proc:
            log.info("Step 5: Shutting down service...")
            from app.agents.service_starter import stop_service
            stop_service(service_proc)
            log.info("Service shutdown complete")
    
    workflow_elapsed = time.time() - workflow_start
    log.info("=" * 80)
    log.info("验证完成: 测试了 %d 个特性", len(verification_results))
    passed = sum(1 for r in verification_results if r["execution_result"]["tests_passed"])
    log.info("结果: %d 通过, %d 失败", passed, len(verification_results) - passed)
    log.info("总耗时: %.2f 秒 (%.2f 分钟)", workflow_elapsed, workflow_elapsed / 60)
    log.info("=" * 80)
    
    base_report["functional_verification"] = verification_results
    return base_report

