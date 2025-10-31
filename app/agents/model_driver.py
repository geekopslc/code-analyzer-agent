
import json
from typing import Optional, Dict, Any, List, Tuple

from app.config.settings import (
    use_ollama,
    ollama_model,
    get_anthropic_client,
    anthropic_model,
    get_dashscope_client,
    dashscope_model,
    DASHSCOPE_API_KEY,
    DASHSCOPE_MODEL,
)
from app.utils.logger import get_logger

log = get_logger("model_driver")


class ModelDriver:
    
    def __init__(self):
        self.anthropic_client = get_anthropic_client()
        self.dashscope_client = get_dashscope_client()
        self.use_ollama_flag = use_ollama()
    
    def chat(self, prompt: str, system: Optional[str] = None, max_tokens: int = 4000, qcoder=True) -> str:
        """统一的聊天接口，优先使用 DashScope，失败则尝试 Anthropic，最后尝试 Ollama"""
        if qcoder:
            result = self._dashscope_chat(prompt, system, max_tokens)
            if result:
                return result
        else:
            result = self._anthropic_chat(prompt, system, max_tokens)
            if result:
                return result
        
        # 其次尝试 DashScope (Qwen Coder)
        # if self.dashscope_client is not None:
        #     result = self._dashscope_chat(prompt, system, max_tokens)
        #     if result:
        #         return result
        
        # 回退到 Ollama
        if self.use_ollama_flag:
            result = self._ollama_chat(prompt, system)
            if result:
                return result
        
        log.warning("All LLM backends failed, returning empty response")
        return ""
    
    def _anthropic_chat(
        self, 
        prompt: str, 
        system: Optional[str] = None, 
        max_tokens: int = 4000
    ) -> Optional[str]:
        """调用 Anthropic API"""
        if self.anthropic_client is None:
            return None
        
        try:
            log.debug("calling Anthropic API with prompt length: %d", len(prompt))
            
            resp = self.anthropic_client.messages.create(
                model=anthropic_model(),
                system=system or "You are a helpful AI assistant.",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            
            content = ""
            if resp and getattr(resp, "content", None):
                parts = []
                for block in resp.content:
                    val = getattr(block, "text", None)
                    if val:
                        parts.append(val)
                content = "\n".join(parts)
            
            log.debug("Anthropic response length: %d", len(content))
            return content.strip()
            
        except Exception as e:
            log.warning("Anthropic API call failed: %s", e)
            return None
    
    def _dashscope_chat(self, prompt: str, system: Optional[str] = None, max_tokens: int = 4000) -> Optional[str]:
        """调用 DashScope API（通过适配器与 Anthropic 接口对齐）"""
        if self.dashscope_client is None:
            return None
        try:
            log.debug("calling DashScope API with prompt length: %d", len(prompt))
            resp = self.dashscope_client.messages.create(
                model=dashscope_model(),
                system=system or "You are a helpful AI assistant.",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            content = ""
            if resp and getattr(resp, "content", None):
                parts = []
                for block in resp.content:
                    val = getattr(block, "text", None)
                    if val:
                        parts.append(val)
                content = "\n".join(parts)
            log.debug("DashScope response length: %d", len(content))
            return content.strip()
        except Exception as e:
            log.warning("DashScope API call failed: %s", e)
            return None

    def _ollama_chat(self, prompt: str, system: Optional[str] = None) -> Optional[str]:
        """调用 Ollama API"""
        try:
            import ollama
            
            log.debug("calling Ollama API with prompt length: %d", len(prompt))
            
            messages = [{"role": "user", "content": prompt}]
            if system:
                messages.insert(0, {"role": "system", "content": system})
            
            resp = ollama.chat(
                model=ollama_model(),
                messages=messages
            )
            
            content = resp.get("message", {}).get("content", "")
            log.debug("Ollama response length: %d", len(content))
            return content.strip()
            
        except Exception as e:
            log.warning("Ollama API call failed: %s", e)
            return None
    
    def extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 响应中提取 JSON 对象
        
        Args:
            text: LLM 响应文本
            
        Returns:
            解析后的 JSON 字典，失败返回 None
        """
        import re
        
        # 移除 markdown 代码块标记
        if "```" in text:
            segs = text.split("```")
            for s in segs:
                s = s.strip()
                if s.startswith("json"):
                    s = s[4:].strip()
                if s.startswith("{") or s.startswith("["):
                    text = s
                    break
        
        # 尝试直接解析
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        
        # 尝试提取 JSON 对象
        if not (text.startswith("{") or text.startswith("[")):
            # 使用正则提取
            if "{" in text:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    text = m.group(0)
            elif "[" in text:
                m = re.search(r"\[.*\]", text, re.DOTALL)
                if m:
                    text = m.group(0)
        
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("Failed to parse JSON: %s", e)
            return None
    
    def extract_code_block(self, text: str, language: str = "") -> str:
        """从 LLM 响应中提取代码块
        
        Args:
            text: LLM 响应文本
            language: 期望的代码语言（如 "javascript", "python"）
            
        Returns:
            提取的代码字符串
        """
        if "```" not in text:
            return text.strip()
        
        lines = text.split("\n")
        in_block = False
        code_lines = []
        target_lang = language.lower() if language else None
        current_lang = None
        
        for line in lines:
            if line.startswith("```"):
                if not in_block:
                    # 开始代码块
                    in_block = True
                    # 提取语言标识
                    lang_part = line[3:].strip().lower()
                    current_lang = lang_part if lang_part else None
                else:
                    # 结束代码块
                    if target_lang is None or current_lang == target_lang:
                        # 找到目标代码块，直接返回
                        return "\n".join(code_lines)
                    # 重置，继续查找
                    in_block = False
                    code_lines = []
                    current_lang = None
                continue
            
            if in_block:
                code_lines.append(line)
        
        # 如果还在代码块中（未闭合），返回已收集的内容
        if code_lines:
            return "\n".join(code_lines)
        
        return text.strip()
    
    def agent_chat_with_tools(
        self,
        initial_prompt: str,
        tools: List[Dict[str, Any]],
        tool_executor_func,
        system: Optional[str] = None,
        max_iterations: int = 10,
        max_tokens: int = 4000
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """使用工具调用进行Agent对话
        
        Args:
            initial_prompt: 初始提示
            tools: 工具定义列表（OpenAI/DashScope格式）
            tool_executor_func: 工具执行函数，接受(tool_name, arguments)返回结果字符串
            system: 系统提示
            max_iterations: 最大迭代次数
            max_tokens: 每次响应的最大token数
            
        Returns:
            (最终回答, 工具调用历史)
        """
        try:
            import dashscope
            from dashscope import Generation
        except ImportError:
            log.error("dashscope not installed, cannot use agent_chat_with_tools")
            return self.chat(initial_prompt, system, max_tokens), []
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": initial_prompt})
        
        tool_call_history = []
        
        for iteration in range(max_iterations):
            log.info(f"Agent iteration {iteration + 1}/{max_iterations}")
            
            try:
                # 调用DashScope API with tools
                response = Generation.call(
                    api_key=DASHSCOPE_API_KEY,
                    model=DASHSCOPE_MODEL,
                    messages=messages,
                    tools=tools,
                    result_format="message"
                )
                
                if not response or response.status_code != 200:
                    log.warning(f"DashScope API error: {response}")
                    break
                
                # 获取助手消息
                assistant_msg = response.output.choices[0].message
                
                # 检查是否有工具调用（兼容DashScope对象可能抛出KeyError的情况）
                tool_calls = None
                try:
                    # Some dashscope SDK objects raise KeyError on unknown attributes
                    tool_calls = assistant_msg.tool_calls  # type: ignore[attr-defined]
                except Exception:
                    try:
                        # If message behaves like dict
                        tool_calls = assistant_msg.get('tool_calls', None)  # type: ignore[attr-defined]
                    except Exception:
                        tool_calls = None
                
                if not tool_calls:
                    # 没有工具调用，返回最终答案
                    final_content = getattr(assistant_msg, 'content', '') if not isinstance(assistant_msg, dict) else assistant_msg.get('content', '')
                    log.info("Agent finished without tool calls")
                    return final_content, tool_call_history
                
                # 添加助手消息到历史（包含工具调用）
                msg_record = {
                    "role": "assistant",
                    "content": getattr(assistant_msg, 'content', '') if not isinstance(assistant_msg, dict) else assistant_msg.get('content', '')
                }
                if tool_calls:
                    msg_record["tool_calls"] = tool_calls
                messages.append(msg_record)
                
                # 执行每个工具调用
                for tool_call in tool_calls:
                    # DashScope返回的tool_call可能是dict或对象
                    if isinstance(tool_call, dict):
                        function_info = tool_call.get('function', {})
                        tool_name = function_info.get('name', '')
                        tool_args_str = function_info.get('arguments', '{}')
                    else:
                        tool_name = tool_call.function.name
                        tool_args_str = tool_call.function.arguments
                    
                    try:
                        tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                    except json.JSONDecodeError:
                        tool_args = {}
                    
                    log.info(f"Calling tool: {tool_name} with args: {tool_args}")
                    
                    # 执行工具
                    tool_result = tool_executor_func(tool_name, tool_args)
                    
                    # 记录工具调用
                    tool_call_history.append({
                        "tool": tool_name,
                        "arguments": tool_args,
                        "result": tool_result[:500]  # 只记录前500字符
                    })
                    
                    # 添加工具结果到消息历史
                    messages.append({
                        "role": "tool",
                        "content": tool_result,
                        "name": tool_name
                    })
                
            except Exception as e:
                log.exception(f"Error in agent iteration {iteration + 1}: {e}")
                break
        
        # 如果达到最大迭代次数，返回最后一次响应
        log.warning(f"Agent reached max iterations ({max_iterations})")
        
        # 尝试获取最终回答
        try:
            final_response = Generation.call(
                api_key=DASHSCOPE_API_KEY,
                model=DASHSCOPE_MODEL,
                messages=messages + [{"role": "user", "content": "请基于以上信息给出最终分析结果。"}],
                result_format="message"
            )
            final_content = final_response.output.choices[0].message.content
            return final_content, tool_call_history
        except:
            return "分析未完成（达到最大迭代次数）", tool_call_history


