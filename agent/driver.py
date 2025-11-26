import json
import logging
import os
import ssl
import urllib3
from typing import Any, Dict, List, Optional, Tuple
from openai import OpenAI
# 禁用 SSL 证书验证和警告
os.environ["PYTHONHTTPSVERIFY"] = "0"
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 全局禁用可能触发的 Triton/FP8 路径，需在导入 transformers 之前设置
# os.environ.setdefault("TRANSFORMERS_NO_TRITON", "1")
# os.environ.setdefault("DISABLE_TRITON", "1")
# os.environ.setdefault("USE_FP8", "0")
# os.environ.setdefault("DISABLE_FP8", "1")
# os.environ.setdefault("DISABLE_FLASH_ATTN", "1")
# os.environ.setdefault("DISABLE_FINEGRAINED_FP8", "1")
# os.environ.setdefault("ENABLE_FINEGRAINED_FP8", "0")

# 创建未验证的 SSL 上下文
_ssl_context = ssl._create_unverified_context()
ssl._create_default_https_context = ssl._create_unverified_context

# 配置 urllib3 使用未验证的 SSL
try:
    urllib3.util.ssl_.DEFAULT_CIPHERS += ':HIGH:!DH:!aNULL'
except AttributeError:
    pass
try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except (ImportError, AttributeError):
    pass

# 配置日志
logger = logging.getLogger("agent.driver")
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

# 全局模型缓存：避免重复加载模型
_model_cache: Dict[Tuple[str, int], 'LocalQwenAdapter'] = {}

def _shield_finegrained_fp8() -> None:
    """按需屏蔽 finegrained_fp8/Triton 路径：默认不屏蔽（以获得更高性能）。
    如需屏蔽（兼容性优先），设置 LLM_DISABLE_TRITON=1。
    必须在任何 transformers 导入之前调用。
    """
    disable_triton = os.getenv("LLM_DISABLE_TRITON", "0") == "1"
    if disable_triton:
        os.environ["TRANSFORMERS_NO_TRITON"] = "1"
        os.environ["DISABLE_TRITON"] = "1"
        os.environ["USE_FP8"] = "0"
        os.environ["DISABLE_FP8"] = "1"
        os.environ["DISABLE_FINEGRAINED_FP8"] = "1"
        os.environ["ENABLE_FINEGRAINED_FP8"] = "0"
        os.environ.setdefault("TRANSFORMERS_FP8_EXECUTION", "0")


class DashscopeClientAdapter:
    def __init__(self, api_key: str, base_url: Optional[str] = None, model: str = "qwen3-coder-plus"):
        import dashscope  # type: ignore
        
        self._ds = dashscope
        if base_url:
            self._ds.base_http_api_url = base_url  # type: ignore[attr-defined]
        self.api_key = api_key
        self.model = model

    def call_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_retries: int = 3) -> Dict[str, Any]:
        """调用 API，带重试机制"""
        last_error = None
        for attempt in range(max_retries):
            try:
                resp = self._ds.Generation.call(
                    api_key=self.api_key,
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    result_format="message",
                )
                return resp
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                # 如果是 SSL 相关错误，重试
                if 'ssl' in error_str or 'sslerror' in error_str or 'unexpected_eof' in error_str:
                    if attempt < max_retries - 1:
                        logger.warning(f"SSL 错误，重试 {attempt + 1}/{max_retries}: {e}")
                        import time
                        time.sleep(0.5 * (attempt + 1))  # 指数退避
                        continue
                # 其他错误直接抛出
                raise
        # 所有重试都失败
        raise last_error or Exception("API 调用失败")


class OllamaAdapter:
    def __init__(self, model_name: str, base_url: str = "http://localhost:11434", max_output_len: int = 9999, timeout: int = 1800):
        """初始化 Ollama 适配器
        
        Args:
            model_name: Ollama 模型名称（例如 "qwen3-coder-30b"）
            base_url: Ollama API 基础 URL，默认 "http://localhost:11434"
            max_output_len: 最大输出长度
            timeout: 请求超时时间（秒），默认 1800（30 分钟）
        """
        import requests  # type: ignore
        import base64
        
        self.model_name = model_name
        self.base_url = base_url.rstrip('/')
        self.max_output_len = max_output_len
        try:
            self.timeout = int(os.getenv("OLLAMA_TIMEOUT", str(timeout)))
        except Exception:
            self.timeout = timeout
        self._requests = requests
        logger.info(f"初始化 Ollama 适配器: model={model_name}, base_url={base_url}, timeout={self.timeout}s")
        
        # 可选：从环境变量载入鉴权/自定义请求头，兼容多种命名
        # 优先级：OLLAMA_HEADERS_JSON > OLLAMA_AUTH_HEADER > OLLAMA_AUTH_TOKEN/OLLAMA_API_KEY/OLLAMA_BEARER_TOKEN > OLLAMA_BASIC_AUTH
        self._headers: Dict[str, str] = {}
        try:
            extra_headers_json = os.getenv("OLLAMA_HEADERS_JSON")
            if extra_headers_json:
                try:
                    self._headers.update(json.loads(extra_headers_json))
                    logger.info("Ollama: 已从 OLLAMA_HEADERS_JSON 载入自定义请求头")
                except Exception as e:
                    logger.warning(f"Ollama: 解析 OLLAMA_HEADERS_JSON 失败: {e}")
            
            # 形如 "Authorization: Bearer xxx" 或 "X-Api-Key: xxx"
            auth_header_kv = os.getenv("OLLAMA_AUTH_HEADER")
            if auth_header_kv and ":" in auth_header_kv:
                k, v = auth_header_kv.split(":", 1)
                self._headers[k.strip()] = v.strip()
                logger.info(f"Ollama: 已启用自定义鉴权头: {k.strip()}")
            
            bearer = os.getenv("OLLAMA_AUTH_TOKEN") or os.getenv("OLLAMA_API_KEY") or os.getenv("OLLAMA_BEARER_TOKEN")
            if bearer and "Authorization" not in self._headers:
                self._headers["Authorization"] = f"Bearer {bearer}"
                logger.info("Ollama: 已启用 Bearer 鉴权头")
            
            # 形如 "user:password"
            basic = os.getenv("OLLAMA_BASIC_AUTH")
            if basic and "Authorization" not in self._headers:
                try:
                    token = base64.b64encode(basic.encode("utf-8")).decode("utf-8")
                    self._headers["Authorization"] = f"Basic {token}"
                    logger.info("Ollama: 已启用 Basic 鉴权头")
                except Exception:
                    logger.warning("Ollama: 处理 OLLAMA_BASIC_AUTH 失败")
        except Exception:
            # 忽略 header 组装失败，按无鉴权继续
            pass
        
        # 输出将附带的请求头键名，避免泄露敏感值
        try:
            if self._headers:
                safe_keys = ", ".join(sorted(self._headers.keys()))
                logger.info(f"Ollama: 本次请求将附带自定义请求头键: [{safe_keys}]")
            else:
                logger.info("Ollama: 未配置任何自定义请求头，将直接请求（可能导致 401）")
        except Exception:
            pass

    def call_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_retries: int = 3) -> Dict[str, Any]:
        """调用 Ollama API，带重试机制
        
        Note: Ollama 可能不支持原生的 tools 参数，所以我们将 tools 信息包含在 system message 中
        """
        last_error = None
        
        # 将 messages 转换为 Ollama 格式
        ollama_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Ollama 使用 "system", "user", "assistant" 角色
            if role == "tool":
                # tool 消息转换为 user 消息
                ollama_messages.append({
                    "role": "user",
                    "content": f"[工具执行结果]\n{content}"
                })
            else:
                ollama_messages.append({
                    "role": role if role in ["system", "user", "assistant"] else "user",
                    "content": content
                })
        
        # 如果有 tools，将 tools 信息添加到最后一个 system 或 user 消息中
        if tools:
            tools_json = json.dumps(tools, ensure_ascii=False, indent=2)
            tools_prompt = f"\n\n【可用工具列表】\n{tools_json}\n\n请根据需要使用上述工具。工具调用格式：\n【TOOL_CALL】{{\"name\": \"工具名称\", \"arguments\": {{参数}}}}【/TOOL_CALL】"
            
            # 查找最后一个 system 或 user 消息，添加 tools 信息
            added = False
            for i in range(len(ollama_messages) - 1, -1, -1):
                if ollama_messages[i]["role"] in ["system", "user"]:
                    ollama_messages[i]["content"] += tools_prompt
                    added = True
                    break
            
            if not added and ollama_messages:
                # 如果没有找到合适的消息，在最后添加一个 user 消息
                ollama_messages.append({
                    "role": "user",
                    "content": tools_prompt
                })
        
        for attempt in range(max_retries):
            try:
                # 调用 Ollama API
                api_url = f"{self.base_url}/api/chat"
                payload = {
                    "model": self.model_name,
                    "messages": ollama_messages,
                    "stream": False,
                    "options": {
                        "num_predict": self.max_output_len,
                        "temperature": 0.2,
                    }
                }
                
                logger.debug(f"调用 Ollama API: {api_url}, model={self.model_name}")
                response = self._requests.post(api_url, json=payload, headers=(self._headers or None), timeout=self.timeout)
                
                response.raise_for_status()
                
                result = response.json()
                
                # 提取生成的文本
                if "message" in result and "content" in result["message"]:
                    generated_text = result["message"]["content"]
                else:
                    generated_text = result.get("response", "")
                
                # 构造与 DashScope 相似的响应对象
                response_obj = type("Resp", (), {})()
                response_obj.output = type("Output", (), {})()
                response_obj.output.choices = [type("Choice", (), {})()]
                response_obj.output.choices[0].message = {
                    "content": generated_text,
                    "tool_calls": [],
                }
                
                return response_obj
                
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                if attempt < max_retries - 1:
                    logger.warning(f"Ollama API 调用失败，重试 {attempt + 1}/{max_retries}: {e}")
                    import time
                    time.sleep(0.5 * (attempt + 1))  # 指数退避
                    continue
                raise
        
        # 所有重试都失败
        raise last_error or Exception("Ollama API 调用失败")


class LocalQwenAdapter:
    def __init__(self, model_dir: str, max_output_len: int = 9999):
        # 在导入 torch / 加载模型之前，限制可见设备数量
        # 性能说明：
        # - 使用1张卡：如果模型能放入单卡显存，推理速度通常更快（无跨卡通信开销）
        # - 使用多张卡：仅在模型无法放入单卡时必需，但推理时跨卡通信会带来开销，可能更慢
        # 优先读取 CUDA_VISIBLE_DEVICES；否则读取 LLM_VISIBLE_DEVICES；都未设置则默认 "0"（单卡）
        visible_devices = os.getenv("CUDA_VISIBLE_DEVICES", None)
        if not visible_devices:
            llm_visible = os.getenv("LLM_VISIBLE_DEVICES", None)
            if llm_visible:
                os.environ["CUDA_VISIBLE_DEVICES"] = llm_visible
                visible_devices = llm_visible
            else:
                # 默认使用单卡以获得更好的推理性能（如果模型能放入单卡显存）
                # 如需使用多卡，可通过环境变量设置：CUDA_VISIBLE_DEVICES="0,1,2,3,4,5,6,7" 或 LLM_VISIBLE_DEVICES="0,1,2,3,4,5,6,7"
                os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
                visible_devices = os.environ["CUDA_VISIBLE_DEVICES"]
        logger.info(f"可见GPU设备设置: CUDA_VISIBLE_DEVICES={visible_devices}")

        # 检查缓存，如果已存在则复用（纳入可见设备以避免复用到不同设备配置的实例）
        cache_key = (model_dir, max_output_len, visible_devices)
        if cache_key in _model_cache:
            cached = _model_cache[cache_key]
            self.model = cached.model
            self.tokenizer = cached.tokenizer
            self.device = cached.device
            self.model_dir = cached.model_dir
            self.max_output_len = cached.max_output_len
            logger.info(f"复用已缓存的模型: {model_dir} (max_output_len={max_output_len}, devices={visible_devices})")
            return
        
        # 使用 transformers 直接加载本地模型，避免 modelscope 依赖
        # 确保禁用项在导入 transformers 之前已设置
        # os.environ.setdefault("TRANSFORMERS_NO_TRITON", "1")
        # os.environ.setdefault("DISABLE_TRITON", "1")
        # os.environ.setdefault("USE_FP8", "0")
        # os.environ.setdefault("DISABLE_FP8", "1")
        # os.environ.setdefault("DISABLE_FLASH_ATTN", "1")
        # 在导入任何 transformers 前，彻底屏蔽 finegrained_fp8
        try:
            _shield_finegrained_fp8()
        except Exception:
            pass

        # 优先使用 Qwen 官方推理库，其次回退到 transformers
        using_official = False
        try:
            from qwen_vl_utils import AutoModelForCausalLM, AutoTokenizer  # type: ignore
            using_official = True
            logger.info("使用 Qwen 官方推理库加载模型 (qwen_vl_utils)")
        except Exception:
            try:
                from qwen import AutoModelForCausalLM, AutoTokenizer  # type: ignore
                using_official = True
                logger.info("使用 Qwen 官方推理库加载模型 (qwen)")
            except Exception:
                from transformers import AutoTokenizer, AutoModelForCausalLM  # type: ignore
                logger.info("未找到官方库，回退到 transformers 加载模型")

        import torch  # type: ignore

        logger.info(f"正在加载本地模型: {model_dir} (devices={visible_devices})")
        
        # 检查CUDA是否可用
        if not torch.cuda.is_available():
            logger.error("❌ CUDA不可用！推理将使用CPU，速度会很慢。")
            logger.error("请检查：1) GPU驱动是否正确安装 2) PyTorch是否支持CUDA 3) CUDA环境变量是否正确")
        else:
            gpu_count = torch.cuda.device_count()
            logger.info(f"✅ 检测到 {gpu_count} 个GPU设备")
            for i in range(gpu_count):
                logger.info(f"  GPU {i}: {torch.cuda.get_device_name(i)}")

        # 某些环境下需要禁用 flash attention，避免不兼容报错
        # os.environ.setdefault("DISABLE_FLASH_ATTN", "1")
        # # 禁用 FP8 量化以避免 Triton 编译问题（FP8 需要 CUDA 开发库）
        # os.environ.setdefault("TRITON_DISABLE_LINE_INFO", "1")
        # # 禁用 FP8 量化，使用标准精度
        # os.environ.setdefault("USE_FP8", "0")
        # os.environ.setdefault("DISABLE_FP8", "1")

        # _shield_finegrained_fp8 已处理，这里无需重复 monkey patch

        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=True)
        
        # 先尝试 float16（节省显存），失败则回退到自动检测或 float32
        try:
            self.model = AutoModelForCausalLM.from_pretrained(
                model_dir,
                device_map="auto",
                torch_dtype=torch.float16,
                trust_remote_code=True,
            )
            logger.info("模型已使用 float16 精度加载")
        except Exception as e:
            logger.warning(f"float16 加载失败，尝试自动检测: {e}")
            try:
                # 不指定 torch_dtype，让 transformers 根据模型配置自动选择
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_dir,
                    device_map="auto",
                    trust_remote_code=True,
                )
                logger.info("模型已使用自动检测的精度加载")
            except Exception as e2:
                logger.warning(f"自动检测失败，回退到 float32: {e2}")
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_dir,
                    device_map="auto",
                    torch_dtype=torch.float32,
                    trust_remote_code=True,
                )
                logger.info("模型已使用 float32 精度加载")

        # 获取模型输入层（embedding层）所在的设备，这是输入数据应该放置的设备
        try:
            if hasattr(self.model, 'model') and hasattr(self.model.model, 'embed_tokens'):
                self.device = self.model.model.embed_tokens.weight.device
                logger.info(f"✅ 从embedding层获取设备: {self.device}")
            elif hasattr(self.model, 'get_input_embeddings'):
                emb = self.model.get_input_embeddings()
                self.device = next(emb.parameters()).device
                logger.info(f"✅ 从get_input_embeddings获取设备: {self.device}")
            else:
                # 回退：获取第一个参数所在的设备
                self.device = next(self.model.parameters()).device
                logger.info(f"⚠️ 从第一个参数获取设备: {self.device}")
        except Exception as e:
            logger.warning(f"获取设备失败，使用默认方法: {e}")
            self.device = next(self.model.parameters()).device
        
        self.model_dir = model_dir
        self.max_output_len = max_output_len
        
        # 详细日志：检查设备类型
        if str(self.device).startswith('cuda'):
            logger.info(f"✅ 模型已加载到GPU设备: {self.device}")
            if torch.cuda.is_available():
                try:
                    device_idx = self.device.index if hasattr(self.device, 'index') else int(str(self.device).split(':')[1])
                    logger.info(f"   GPU名称: {torch.cuda.get_device_name(device_idx)}")
                    logger.info(f"   GPU显存: {torch.cuda.get_device_properties(device_idx).total_memory / 1024**3:.2f} GB")
                except Exception:
                    pass
        else:
            logger.warning(f"⚠️ 模型已加载到CPU设备: {self.device}")
            if torch.cuda.is_available():
                logger.error("❌ CUDA可用但模型加载到CPU！这可能是device_map='auto'的问题。")
                logger.error("   建议：检查模型文件或尝试显式指定device_map='cuda:0'")
            else:
                logger.warning("   推理速度会很慢！请检查CUDA环境配置。")
        
        # 缓存模型实例
        _model_cache[cache_key] = self
        logger.info(f"模型已缓存: {model_dir} (max_output_len={max_output_len}, devices={visible_devices})")

    def call_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_retries: int = 3) -> Dict[str, Any]:
        """模拟 DashScope 的响应对象结构，便于复用现有流程。"""
        # 将多轮消息拼接为 prompt
        prompt_lines: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            prompt_lines.append(f"{role}: {content}")
        prompt = "\n".join(prompt_lines)

        import torch  # type: ignore
        last_err: Optional[Exception] = None
        generated_text = ""
        
        # 读取本地生成的输入上限/解码策略/是否允许 Triton
        import os
        max_input_tokens = int(os.getenv("LOCAL_LLM_MAX_INPUT_TOKENS", "8192"))
        greedy_decode = os.getenv("LOCAL_LLM_GREEDY", "0") == "1"
        # 不再强制禁用 Triton，遵循 LLM_DISABLE_TRITON
        original_disable_triton = os.environ.get("DISABLE_TRITON", None)
        if os.getenv("LLM_DISABLE_TRITON", "0") == "1":
            os.environ["DISABLE_TRITON"] = "1"
        else:
            if original_disable_triton is not None:
                # 维持原值
                os.environ["DISABLE_TRITON"] = original_disable_triton
            else:
                os.environ.pop("DISABLE_TRITON", None)
        
        try:
            for attempt in range(max_retries):
                try:
                    # 将输入数据移动到正确的设备
                    inputs = self.tokenizer(
                        prompt,
                        return_tensors="pt",
                        truncation=True,
                        max_length=max_input_tokens,
                    )
                    # 确保所有输入张量都在正确的设备上
                    inputs = {k: v.to(self.device) for k, v in inputs.items()}
                    # 验证输入设备
                    input_device = inputs['input_ids'].device
                    logger.debug(f"输入数据已移动到设备: {input_device} (模型设备: {self.device})")
                    if str(input_device) != str(self.device):
                        logger.warning(f"⚠️ 输入设备({input_device})与模型设备({self.device})不匹配！")
                    if not str(input_device).startswith('cuda') and torch.cuda.is_available():
                        logger.error(f"❌ 推理将使用CPU({input_device})，但CUDA可用！这会导致速度极慢。")
                    # 生成前的通用安全/性能配置
                    try:
                        if getattr(self.model.config, "pad_token_id", None) is None and getattr(self.tokenizer, "eos_token_id", None) is not None:
                            self.model.config.pad_token_id = self.tokenizer.eos_token_id
                    except Exception:
                        pass
                    try:
                        self.model.generation_config.use_cache = True  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    try:
                        torch.backends.cuda.matmul.allow_tf32 = True  # type: ignore[attr-defined]
                    except Exception:
                        pass

                    with torch.no_grad():
                        try:
                            # 优先启用 flash attention（若可用）
                            sdp_ctx = torch.backends.cuda.sdp_kernel(
                                enable_flash=True,
                                enable_math=True,
                                enable_mem_efficient=True
                            )  # type: ignore[attr-defined]
                        except Exception:
                            from contextlib import nullcontext
                            sdp_ctx = nullcontext()
                        with sdp_ctx:
                            outputs = self.model.generate(
                                **inputs,
                                max_new_tokens=self.max_output_len,
                                do_sample=(not greedy_decode),
                                temperature=0.2 if not greedy_decode else 0.0,
                                top_p=0.9 if not greedy_decode else 1.0,
                            )
                    # 仅取新生成部分
                    input_len = inputs['input_ids'].shape[1]
                    generated_ids = outputs[0][input_len:]
                    generated_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(f"生成失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(0.5 * (attempt + 1))
                        continue
        finally:
            # 恢复原始环境变量
            if original_disable_triton is None:
                os.environ.pop("DISABLE_TRITON", None)
            else:
                os.environ["DISABLE_TRITON"] = original_disable_triton

        if not generated_text and last_err:
            raise last_err

        # 构造与 DashScope 相似的响应对象（属性访问）
        response = type("Resp", (), {})()
        response.output = type("Output", (), {})()
        response.output.choices = [type("Choice", (), {})()]
        response.output.choices[0].message = {
            "content": generated_text,
            "tool_calls": [],
        }
        return response

        
class VllmOpenAIAdapter:
    """符合 vLLM 规范的 OpenAI 接口适配器"""

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "EMPTY",
        model_name: str = "/data/Qwen3-Coder-30B-A3B-Instruct",
        temperature: float = 0.7,
        top_p: float = 0.8,
        max_tokens: int = 2048,
        repetition_penalty: float = 1.05,
        enable_thinking: bool = False,
        timeout: int = 600,
        tool_choice: str = "auto",  # vLLM要求：auto, required, 或指定函数名
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model_name = model_name
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.repetition_penalty = repetition_penalty
        self.enable_thinking = enable_thinking
        self.timeout = timeout
        self.tool_choice = tool_choice  # vLLM工具选择策略

        logger.info(f"vLLM OpenAI Adapter 初始化完成: model={model_name}, base_url={base_url}, tool_choice={tool_choice}")

    def call_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] = None, max_retries: int = 3):
        """符合 vLLM 规范的工具调用实现"""
        tools = tools or []
        last_error = None

        # 确保工具格式符合 OpenAI 规范
        openai_tools = []
        for tool in tools:
            # 如果工具已经符合 OpenAI 规范（有 type 和 function 字段），则直接使用
            if "type" in tool and "function" in tool:
                openai_tools.append(tool)
            # 否则转换为 OpenAI 规范格式
            else:
                openai_tools.append({
                    "type": "function",
                    "function": tool
                })

        for attempt in range(max_retries):
            try:
                # 构建符合 vLLM 规范的 API 调用参数
                api_params = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "max_tokens": self.max_tokens,
                    "timeout": self.timeout,
                }

                # 添加工具相关参数（vLLM 要求必须指定 tool_choice）
                if openai_tools:
                    api_params["tools"] = openai_tools
                    # vLLM 强制要求 tool_choice 参数
                    api_params["tool_choice"] = self.tool_choice
                
                # 添加 vLLM 特有参数到 extra_body
                extra_body = {}
                if hasattr(self, 'repetition_penalty') and self.repetition_penalty != 1.0:
                    extra_body["repetition_penalty"] = self.repetition_penalty
                
                # 对于 Qwen3 等模型，支持 enable_thinking
                if hasattr(self, 'enable_thinking') and self.enable_thinking:
                    extra_body["chat_template_kwargs"] = {"enable_thinking": True}
                
                if extra_body:
                    api_params["extra_body"] = extra_body

                logger.debug(f"[vLLM] API调用参数: {list(api_params.keys())}")
                
                response = self.client.chat.completions.create(**api_params)

                # === 构造 DashScope 风格返回对象 ===
                msg = response.choices[0].message
                content = getattr(msg, "content", None)
                tool_calls = getattr(msg, "tool_calls", None)

                # 确保工具调用格式符合规范
                if tool_calls:
                    normalized_tool_calls = []
                    for tool_call in tool_calls:
                        # 确保每个 tool_call 都有 id, type, function 字段
                        normalized_call = {
                            "id": getattr(tool_call, "id", f"call_{len(normalized_tool_calls)+1}"),
                            "type": getattr(tool_call, "type", "function"),
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments
                            }
                        }
                        normalized_tool_calls.append(normalized_call)
                    tool_calls = normalized_tool_calls

                response_obj = type("Resp", (), {})()
                response_obj.output = type("Output", (), {})()
                response_obj.output.choices = [type("Choice", (), {})()]
                response_obj.output.choices[0].message = {
                    "content": content,
                    "tool_calls": tool_calls,
                }

                logger.debug(f"[vLLM] 响应: content_length={len(content or '')}, tool_calls_count={len(tool_calls or [])}")
                return response_obj

            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                logger.warning(f"[vLLM] 调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                
                # 对于网络错误进行重试
                if attempt < max_retries - 1 and ('connection' in error_str or 'timeout' in error_str or 'network' in error_str):
                    import time
                    time.sleep(0.5 * (attempt + 1))  # 指数退避
                    continue
                # 对于其他错误不重试，直接抛出
                break

        raise last_error or Exception("vLLM API 调用失败")

        
def _parse_tool_calls_from_text(assistant_content: str) -> List[Dict[str, Any]]:
    """从模型文本中解析工具调用标记，返回规范化的工具调用结构列表。

    期望文本片段格式：
    【TOOL_CALL】{"name": "save_memory", "arguments": {"file": "test.md"}}【/TOOL_CALL】
    返回项形如：{"function": {"name": "save_memory", "arguments": "{...}"}}
    """
    if not assistant_content:
        return []
    import re
    import json as _json

    matches = re.findall(r'【TOOL_CALL】(.*?)【/TOOL_CALL】', assistant_content, re.DOTALL)
    normalized: List[Dict[str, Any]] = []
    for idx, raw in enumerate(matches):
        try:
            obj = _json.loads(raw)
            name = obj.get("name", "")
            args = obj.get("arguments", {})
            normalized.append({
                "id": f"local-{idx+1}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": _json.dumps(args, ensure_ascii=False),
                },
            })
        except Exception:
            continue
    return normalized

def agent_chat_with_tools(
    api_key: str,
    client: Any,
    system: Optional[str],
    initial_prompt: str,
    tools: List[Dict[str, Any]],
    tool_executor,
    model: str = "qwen3-coder-plus",
    base_url: Optional[str] = None,
    max_iterations: int = 55,
    repo_root: Optional[str] = None,
    memory_file_path: str = "analysis_memory.md",
    local_model_dir: Optional[str] = None,
    local_max_output_len: int = 9999,
) -> Tuple[str, List[Dict[str, Any]]]:
    logger.info("开始分析代码库")
    
    def _has_open_todos(repo_root_dir: Optional[str]) -> bool:
        """检查任务清单是否存在未完成项。"""
        try:
            if not repo_root_dir:
                return False
            todo_path = os.path.join(repo_root_dir, "memory", "todolist.json")
            if not os.path.exists(todo_path):
                return False
            with open(todo_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict):
                # 新格式：包含sections和tasks
                if "sections" in data and "tasks" in data:
                    for task in data.get("tasks", []):
                        status = str(task.get("status", "")).lower()
                        if status not in ("completed", "cancelled"):
                            return True
                    return False
                
                # 兼容旧格式：只有tasks
                elif "tasks" in data:
                    for task in data.get("tasks", []):
                        status = str(task.get("status", "")).lower()
                        if status not in ("completed", "cancelled"):
                            return True
                    return False

            if isinstance(data, list):
                for it in data:
                    status = str(it.get("status", "")).lower()
                    if status in ("pending", "doing"):
                        return True
                return False

            return False
        except Exception:
            return False
    
    # 读取记忆文件内容（如果存在）
    memory_content = ""
    if repo_root:
        memory_file_abs_path = os.path.join(repo_root, memory_file_path)
        print(f"memory_file_abs_path: {memory_file_abs_path}")
        if os.path.exists(memory_file_abs_path) and os.path.isfile(memory_file_abs_path):
            try:
                with open(memory_file_abs_path, "r", encoding="utf-8") as f:
                    memory_content = f.read()
                logger.info(f"已加载记忆文件: {memory_file_path} ({len(memory_content)} 字符)")
            except Exception as e:
                logger.warning(f"读取记忆文件失败: {e}")
    
    # 优先使用 Ollama，其次本地模型，最后 DashScope
    # use_ollama = os.getenv("USE_OLLAMA", "1") == "1"  # 默认使用 Ollama
    # ollama_model_name = os.getenv("OLLAMA_MODEL_NAME", None)
    # ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://192.168.1.5:11434")
    
    
    messages: List[Dict[str, Any]] = []
    
    # 构建工具提示词
    tools_rules_text = ""
    try:
        tools_json_text = json.dumps(tools, ensure_ascii=False, indent=2)
        tools_rules_text = (
            "\n\n【可用工具列表（严禁使用未提供的工具名）】\n"
            f"{tools_json_text}\n\n"
            "【工具调用格式（必须严格遵守）】\n"
            "【TOOL_CALL】{\"name\": \"<tool_name>\", \"arguments\": { ... 严格JSON ... }}【/TOOL_CALL】\n"
            "- 仅限使用上面列出的工具名；若名称不在列表中，一律视为非法，禁止使用；\n"
            "- arguments 必须是严格 JSON（双引号，布尔/数字类型正确，禁止尾逗号）；\n"
            "- 如需连续调用多个工具，可在一次回复中给出多段连续的 TOOL_CALL 标记；\n"
            "- 收到工具结果后，若仍需探索请继续输出 TOOL_CALL；若已满足输出条件，再输出最终 JSON 结果。"
        )
    except Exception as e:
        logger.warning(f"构建工具提示词失败: {e}")
    
    # 合并系统提示词和工具提示词
    system_content = system if system else ""
    if tools_rules_text:
        system_content = (system_content + tools_rules_text).strip()
    
    if system_content:
        messages.append({"role": "system", "content": system_content})
    
    # 构建用户消息，包含记忆文件内容（如果存在）
    user_message_content = initial_prompt
    if memory_content:
        user_message_content = f"""【已有记忆文件内容】
以下是从 `{memory_file_path}` 中读取的之前记录的分析内容，请仔细阅读并充分利用这些信息：

---
{memory_content}
---

【当前任务】
{initial_prompt}

【重要提示】
1. 上述记忆文件中的信息是之前分析的记录，包含了已经分析过的函数信息、心得和理解
2. 请充分利用这些记忆信息，避免重复分析已经记录过的函数
3. 基于已有的发现进行深入的总结和归纳
4. 在分析过程中，继续使用 save_memory 工具更新和补充记忆文件
5. 在最终输出时，确保与记忆文件中的信息一致，并充分利用记忆文件辅助总结
"""
    
    messages.append({"role": "user", "content": user_message_content})

    tool_call_history: List[Dict[str, Any]] = []
    
    # 跟踪已读文件列表（用于引导模型自主决定）
    read_files: List[str] = []

    for iteration in range(max_iterations):
        logger.info(f"[迭代 {iteration + 1}/{max_iterations}]")
        try:
            response = client.call_with_tools(messages=messages, tools=tools)
        except Exception as e:
            error_str = str(e).lower()
            if 'ssl' in error_str or 'sslerror' in error_str:
                logger.error(f"SSL 连接错误: {e}")
                return f"错误: SSL 连接失败 - {str(e)}", tool_call_history
            raise
        
        # 检查响应是否有效
        if not response or not hasattr(response, 'output') or response.output is None:
            error_msg = f"API 响应无效: response={response}"
            if hasattr(response, 'message'):
                error_msg += f", message={response.message}"
            logger.error(error_msg)
            return "错误: API 响应无效，未获取到有效输出", tool_call_history
        
        if not hasattr(response.output, 'choices') or not response.output.choices:
            error_msg = f"API 响应中没有 choices: response.output={response.output}"
            logger.error(error_msg)
            return "错误: API 响应中没有 choices", tool_call_history
        
        # 正常化取第一条消息
        assistant_msg = response.output.choices[0].message  # type: ignore[attr-defined]
        if isinstance(assistant_msg, dict):
            assistant_content = assistant_msg.get("content", "") or ""
        else:
            assistant_content = getattr(assistant_msg, "content", None) or ""
        
        # 确保 assistant_content 是字符串类型
        if assistant_content is None:
            assistant_content = ""
        else:
            assistant_content = str(assistant_content)
        
        # 检测模型是否表示完成分析（第一阶段完成）
        # 检测多种可能的完成标记
        completion_markers = [
            "【ANALYSIS_COMPLETE】",
            "【代码分析完成】",
            "【分析完成】",
            "ANALYSIS_COMPLETE",
            "代码分析完成",
            "分析完成，准备进入第二阶段",
            "已完成代码库分析",
        ]
        is_complete = False
        for marker in completion_markers:
            if marker in assistant_content:
                is_complete = True
                logger.info(f"检测到完成标记: {marker}，准备退出迭代分析阶段")
                break
        
        # 如果没有工具调用且没有完成标记，也检查是否明确表示完成
        if not is_complete and assistant_content:
            completion_keywords = [
                "已完成所有代码分析",
                "代码库分析已完成",
                "所有文件已分析完毕",
                "准备进入下一阶段",
                "可以开始分类和输出",
            ]
            for keyword in completion_keywords:
                if keyword in assistant_content:
                    is_complete = True
                    logger.info(f"检测到完成关键词: {keyword}，准备退出迭代分析阶段")
                    break

        # 工具调用字段兼容（对象或字典）
        tool_calls = None
        try:
            tool_calls = assistant_msg.tool_calls  # type: ignore[attr-defined]
        except Exception:
            try:
                tool_calls = assistant_msg.get("tool_calls")  # type: ignore
            except Exception:
                tool_calls = None
        # 额外：从文本中解析工具调用标记，合并到 tool_calls
        parsed_from_text = _parse_tool_calls_from_text(assistant_content)
        if parsed_from_text:
            if not tool_calls:
                tool_calls = parsed_from_text
            else:
                # 如果已有，则合并
                try:
                    tool_calls.extend(parsed_from_text)
                except Exception:
                    tool_calls = parsed_from_text

        if not tool_calls:
            # 若没有新的工具调用，检查是否已完成分析
            if is_complete:
                logger.info("模型表示已完成代码库分析，退出迭代循环")
                # 返回当前助手消息内容，表示第一阶段完成
                return assistant_content, tool_call_history
            
            # 若没有完成标记，根据已读文件列表引导模型继续或退出
            read_files_info = ""
            if read_files:
                read_files_info = f"\n\n【已读文件列表（共 {len(read_files)} 个）】\n" + "\n".join(f"- {f}" for f in read_files[:50])  # 最多显示50个
                if len(read_files) > 50:
                    read_files_info += f"\n... 还有 {len(read_files) - 50} 个文件"
                read_files_info += "\n\n请基于已读文件列表，自主决定：\n"
                read_files_info += "1. 是否还需要读取其他文件来完成分析？\n"
                read_files_info += "2. 如果已读文件足够完成分析，请明确输出【ANALYSIS_COMPLETE】标记表示完成第一阶段。\n"
                read_files_info += "3. 如果还需要继续分析，请继续使用工具读取文件。"
            else:
                read_files_info = "\n\n【提示】当前尚未读取任何文件，请开始使用 read_file 工具读取相关代码文件进行分析。"
            
            messages.append({
                "role": "user",
                "content": (
                    "当前没有工具调用。" + read_files_info + 
                    "\n\n请自主决定下一步：继续分析（使用工具）或完成分析（输出【ANALYSIS_COMPLETE】标记）。"
                )
            })
            continue
        
        # 添加助手消息到历史（必须包含工具调用）
        msg_record = {
            "role": "assistant",
            "content": assistant_content
        }
        if tool_calls:
            msg_record["tool_calls"] = tool_calls
        messages.append(msg_record)

        # 只执行第一个工具调用，而不是所有工具调用
        # 这样可以避免vLLM模型不支持一次处理多个工具调用的问题
        if tool_calls:
            tool_call = tool_calls[0]  # 只取第一个工具调用
            # DashScope返回的tool_call可能是dict或对象
            if isinstance(tool_call, dict):
                tool_call_id = tool_call.get("id", "")
                function_info = tool_call.get("function", {})
                tool_name = function_info.get("name", "")
                tool_args_str = function_info.get("arguments", "{}")
            else:
                tool_call_id = getattr(tool_call, "id", "")
                tool_name = tool_call.function.name
                tool_args_str = tool_call.function.arguments

            try:
                if isinstance(tool_args_str, str):
                    # 清理可能的前缀字符（如 {}{...}）
                    cleaned_str = tool_args_str.strip()
                    
                    # 如果以 {}{ 开头，需要找到第二个（实际有效的）JSON 对象
                    if cleaned_str.startswith("{}{"):
                        # 跳过第一个空的 {}
                        first_brace_end = cleaned_str.find('}')
                        if first_brace_end >= 0:
                            cleaned_str = cleaned_str[first_brace_end + 1:].strip()
                            # 如果还有 {，这就是我们要的 JSON 对象
                            if cleaned_str.startswith('{'):
                                # 找到完整的 JSON 对象
                                brace_count = 0
                                json_end = -1
                                for i, char in enumerate(cleaned_str):
                                    if char == '{':
                                        brace_count += 1
                                    elif char == '}':
                                        brace_count -= 1
                                        if brace_count == 0:
                                            json_end = i + 1
                                            break
                                if json_end > 0:
                                    cleaned_str = cleaned_str[:json_end]
                    
                    tool_args = json.loads(cleaned_str)
                else:
                    tool_args = tool_args_str
            except json.JSONDecodeError as e:
                logger.warning(f"解析工具参数JSON失败: {e}")
                # 尝试提取 JSON 对象
                try:
                    import re
                    json_matches = list(re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', tool_args_str, re.DOTALL))
                    if json_matches:
                        last_match = json_matches[-1]
                        try:
                            tool_args = json.loads(last_match.group(0))
                        except json.JSONDecodeError:
                            for match in reversed(json_matches):
                                try:
                                    tool_args = json.loads(match.group(0))
                                    break
                                except json.JSONDecodeError:
                                    continue
                            else:
                                tool_args = {}
                    else:
                        tool_args = {}
                except Exception:
                    tool_args = {}

            # 准备步骤信息
            step_info = {
                "iteration": iteration,
                "total_steps": max_iterations,
                "tool_index": 1,  # 只处理一个工具调用
                "total_tools_in_iteration": 1  # 只有一个工具调用
            }
            
            # 调用 tool_executor，传递步骤信息（如果支持）
            try:
                import inspect
                sig = inspect.signature(tool_executor)
                if 'step_info' in sig.parameters:
                    tool_result = tool_executor(tool_name, tool_args, step_info=step_info)
                else:
                    tool_result = tool_executor(tool_name, tool_args)
            except Exception:
                # 如果检查签名失败，尝试直接调用（兼容旧版本）
                try:
                    tool_result = tool_executor(tool_name, tool_args, step_info=step_info)
                except TypeError:
                    tool_result = tool_executor(tool_name, tool_args)
            
            # 跟踪已读文件（用于引导模型自主决定）
            if tool_name == "read_file" and isinstance(tool_args, dict):
                file_path = tool_args.get("file_path", "")
                if file_path and file_path not in read_files:
                    read_files.append(file_path)
                    logger.info(f"已记录读取文件: {file_path} (总计: {len(read_files)} 个文件)")
            
            tool_call_history.append({
                "tool": tool_name,
                "arguments": tool_args,
                "result": str(tool_result)
            })

            # 添加工具结果到消息历史（必须包含 tool_call_id）
            messages.append({
                "role": "tool",
                "content": str(tool_result),
                "tool_call_id": tool_call_id or "local-1",
            })
            
            # 在工具结果后，添加已读文件列表信息（帮助模型自主决定）
            if read_files:
                read_files_summary = f"\n\n【📋 已读文件统计】当前已读取 {len(read_files)} 个文件。"
                if len(read_files) <= 10:
                    read_files_summary += "\n已读文件列表：\n" + "\n".join(f"  - {f}" for f in read_files)
                else:
                    read_files_summary += f"\n最近读取的10个文件：\n" + "\n".join(f"  - {f}" for f in read_files[-10:])
                    read_files_summary += f"\n... 还有 {len(read_files) - 10} 个文件已读"
                read_files_summary += "\n\n请基于已读文件情况，自主决定是否需要继续读取其他文件，或已完成分析（输出【ANALYSIS_COMPLETE】标记）。"
                
                # 只在每次迭代开始时添加一次，避免重复
                # 检查上一条消息是否已经是已读文件信息
                if messages and messages[-1].get("role") == "tool":
                    # 在工具结果后添加已读文件信息
                    messages.append({
                        "role": "user",
                        "content": read_files_summary
                    })
            
            # 继续下一次迭代，让模型处理工具调用的结果
            continue

    logger.warning(f"达到最大迭代次数 ({max_iterations})，工具调用 {len(tool_call_history)} 次")
    return "达到最大迭代次数，未给出最终答案", tool_call_history


def simple_chat(api_key: str, client: Any, prompt: str, system: Optional[str] = None, model: str = "/data/Qwen3-Coder-30B-A3B-Instruct", base_url: Optional[str] = None, local_model_dir: Optional[str] = None, local_max_output_len: int = 1024) -> str:
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.call_with_tools(
            messages=messages,
            tools=[],
        )
        
        if not hasattr(response.output, 'choices') or not response.output.choices:
            error_msg = f"API 响应中没有 choices: response.output={response.output}"
            logger.error(error_msg)
            return f"错误: API 响应中没有 choices"
        
        assistant_msg = response.output.choices[0].message  # type: ignore[attr-defined]
        if isinstance(assistant_msg, dict):
            content = assistant_msg.get("content", "") or ""
        else:
            content = getattr(assistant_msg, "content", None) or ""
        
        # 确保 content 是字符串类型
        if content is None:
            content = ""
        else:
            content = str(content)
        
        return content.strip()
    except Exception as e:
        logger.error(f"simple_chat 调用失败: {str(e)}", exc_info=True)
        return f"错误: {str(e)}"