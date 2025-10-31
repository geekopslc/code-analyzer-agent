from typing import Any, Dict, List, TypedDict, Tuple
import math
import json
import time
from collections import Counter, defaultdict

from langgraph.graph import END, StateGraph

from app.utils.code_parser import index_repository, build_execution_suggestion
from app.config.settings import (
	use_ollama,
	ollama_model,
	ollama_embedding_model,
	enable_verification,
)
from app.utils.logger import get_logger
from app.agents.verification_workflow import run_functional_verification
from app.agents.model_driver import ModelDriver

log = get_logger("graph")


class AgentState(TypedDict, total=False):
	problem_description: str
	repo_root: str
	test_qcoder: bool  # 是否使用 test_qcoder 生成测试代码
	code_index: Dict[str, List[Dict[str, object]]]
	filtered_files: List[str]
	candidates: List[Dict[str, object]]
	features: List[str]
	categories: List[Dict[str, str]]
	function_summaries: List[Dict[str, Any]]  # 函数的关键字和摘要
	vector_index: Dict[str, Any]
	mappings: List[Dict[str, object]]
	report: Dict[str, Any]
	verification: Dict[str, Any]  # optional test generation result


def _node_index(state: AgentState) -> AgentState:
	start_time = time.time()
	root = state["repo_root"]
	log.info("=" * 80)
	log.info("NODE: INDEX - 开始索引代码库")
	log.info("=" * 80)
	log.info("indexing repo: %s", root)
	index = index_repository(root)
	elapsed = time.time() - start_time
	log.info("index built: files=%d, 耗时: %.2f秒", len(index), elapsed)
	log.info("=" * 80)
	state["code_index"] = index
	return state


def _filter_files(state: AgentState) -> AgentState:
	"""Use Anthropic and heuristics to pre-filter files by names/paths only, excluding tests/mocks/etc."""
	start_time = time.time()
	import re
	driver = ModelDriver()
	desc = state.get("problem_description", "").strip()
	index = state.get("code_index", {})
	all_paths = list(index.keys())
	
	log.info("=" * 80)
	log.info("NODE: FILTER FILES - 开始过滤文件")
	log.info("=" * 80)
	log.info("过滤前: 总文件数=%d", len(all_paths))

	def heuristic_filter(paths: List[str]) -> List[str]:
		ban_tokens = [
			"/test/", "/tests/", "__tests__", "/spec/", ".spec.", ".e2e.",
			"/mock/", "/mocks/", "/fixture/", "/fixtures/", "/examples/", "/example/",
			".snap", "_test.", "test_", ".story.", ".stories.",
		]
		out: List[str] = []
		for p in paths:
			pl = p.lower()
			if any(t in pl for t in ban_tokens):
				continue
			out.append(p)
		return out

	filtered: List[str] = []
	if desc:
		# Prepare a compact listing
		max_list = 300
		file_list = "\n".join(all_paths[:max_list])
		prompt = (
			"You are a code analysis expert.\n\n"
			"Task: Based on the requirements and ONLY directory/file names (no code), select implementation files to analyze in the next stage.\n\n"
			"Requirements:\n" + desc + "\n\n"
			"Project files (relative paths, sample/trimmed):\n" + file_list + "\n\n"
			"Rules:\n"
			"- STRICTLY EXCLUDE tests, specs, e2e, mocks, fixtures, examples, stories, snapshots.\n"
			"- Prefer service/controller/resolver/handler/business logic, DTOs, schema definitions.\n"
			"- Include up to 120 files maximum.\n"
			"- Output ONLY JSON with 'include_files': string[] of exact paths.\n"
			"- No prose or markdown.\n\n"
			"Example:\n{\n  \"include_files\": [\n    \"src/channel/channel.service.ts\",\n    \"src/message/message.resolver.ts\"\n  ]\n}"
		)
		try:
			text = driver.chat(
				prompt=prompt,
				system="You are a code analysis expert. Follow instructions strictly; no explanations; no extra text.",
				max_tokens=1200
			)
			data = driver.extract_json(text)
			if data:
				cands = data.get("include_files") or []
				# Ensure they exist in index and apply heuristic filter again
				set_all = set(all_paths)
				filtered = heuristic_filter([p for p in cands if p in set_all])[:120]
		except Exception as e:
			log.warning("LLM file filter failed: %s", e)

	# Fallback or complement with heuristic
	if not filtered:
		filtered = heuristic_filter(all_paths)

	state["filtered_files"] = filtered
	elapsed = time.time() - start_time
	log.info("过滤后: 保留文件数=%d", len(filtered))
	log.info("被过滤掉的文件数: %d", len(all_paths) - len(filtered))
	if len(filtered) < len(all_paths):
		excluded = set(all_paths) - set(filtered)
		log.info("前10个被过滤的文件示例: %s", ", ".join(list(excluded)[:10]))
	log.info("NODE: FILTER FILES - 完成，耗时: %.2f秒", elapsed)
	log.info("=" * 80)
	return state


def _categorize_problem(state: AgentState) -> AgentState:
	"""步骤1: 根据原始需求直接生成功能类别和关键字。"""
	start_time = time.time()
	driver = ModelDriver()
	desc = (state.get("problem_description") or "").strip()
	log.info("=" * 80)
	log.info("NODE: CATEGORIZE - 开始分类需求并生成关键字")
	log.info("=" * 80)
	log.info("categorize: desc_len=%d", len(desc))
	
	if not desc:
		# Fallback
		defaults = [
			{"name": "General", "description": "", "summary": "", "keywords": [], "type": "business"}
		]
		state["categories"] = defaults
		return state

	prompt = (
		"You are a code analysis expert.\n\n"
		"Task: Based on the user's requirements, extract functional categories and generate semantic keywords for each category.\n\n"
		"User requirements:\n" + desc + "\n\n"
		"Rules:\n"
		"1) Extract functions that are explicitly mentioned or strongly implied in the requirements\n"
		"2) Generate semantic keywords for each function: include functional concepts, action verbs, domain terms\n"
		"3) Keywords should be semantic and matchable with function names, class names, and comments in code\n"
		"4) IMPORTANT: Include synonyms, related concepts, and semantically equivalent terms. For example:\n"
		"   - For pagination: include 'list', 'query', 'fetch', 'get', 'find', 'retrieve', 'page', 'limit', 'offset'\n"
		"   - For filtering: include 'search', 'filter', 'find', 'query', 'select', 'where'\n"
		"   - For creation: include 'create', 'add', 'new', 'insert', 'build', 'make'\n"
		"   - For listing: include 'list', 'all', 'get', 'fetch', 'retrieve', 'query', 'find'\n"
		"5) Consider related operations that might be used together (e.g., pagination often involves listing/querying)\n"
		"6) Generate 3-8 categories\n"
		"7) STRICTLY follow the user's original requirements, do NOT fabricate or imagine any features\n"
		"8) Do NOT omit ANY functionality mentioned in the original requirements\n"
		"9) Output JSON array, each element contains: name(function name), description(description), keywords(keywords array), type(type: business/infrastructure/quality)\n"
		"10) Output ONLY JSON, no other text\n\n"
		"Example:\n"
		"[\n"
		"  {\"name\": \"Create Channel\", \"description\": \"Channel creation functionality\", \"keywords\": [\"create\", \"channel\", \"new\", \"add\", \"establish\", \"insert\", \"build\"], \"type\": \"business\"},\n"
		"  {\"name\": \"Send Message\", \"description\": \"Message sending functionality\", \"keywords\": [\"message\", \"send\", \"create\", \"post\", \"publish\", \"deliver\", \"dispatch\"], \"type\": \"business\"},\n"
		"  {\"name\": \"Paginate Messages\", \"description\": \"Pagination for message listing\", \"keywords\": [\"paginate\", \"pagination\", \"page\", \"limit\", \"offset\", \"list\", \"query\", \"fetch\", \"get\", \"find\", \"retrieve\", \"messages\"], \"type\": \"business\"}\n"
		"]\n"
	)
	
	try:
		llm_start = time.time()
		text = driver.chat(
			prompt=prompt,
			system="You are a code analysis expert. Strictly follow instructions, output ONLY JSON, no extra text.",
			max_tokens=2000
		)
		llm_elapsed = time.time() - llm_start
		
		log.info("categorize LLM调用完成, 耗时: %.2f秒, response len=%d", llm_elapsed, len(text))
		
		cats_raw = driver.extract_json(text)
		if not cats_raw:
			raise ValueError("Failed to extract JSON")
		cats: List[Dict[str, Any]] = []
		for it in (cats_raw or [])[:8]:
			name = str(it.get("name", "")).strip()
			desc_text = str(it.get("description", "")).strip()
			keywords = it.get("keywords") or []
			type_ = str(it.get("type", "business")).strip()
			if not name:
				continue
			cats.append({
				"name": name,
				"description": desc_text,
				"summary": desc_text,
				"keywords": keywords if isinstance(keywords, list) else [str(keywords)],
				"type": type_,
			})
		
		if not cats:
			cats = [{"name": desc[:60] or "General", "description": desc[:200] or "", "summary": desc[:200] or "", "keywords": [], "type": "business"}]
		
		state["categories"] = cats
		elapsed = time.time() - start_time
		log.info("generated %d categories with keywords", len(cats))
		log.info("NODE: CATEGORIZE - 完成，耗时: %.2f秒", elapsed)
		log.info("=" * 80)
		
	except Exception as e:
		elapsed = time.time() - start_time
		log.warning("categorize failed: %s", e)
		fallback = [{"name": desc[:60] or "General", "description": desc[:200] or "", "summary": desc[:200] or "", "keywords": [], "type": "business"}]
		state["categories"] = fallback
		log.info("NODE: CATEGORIZE - 完成（失败回退），耗时: %.2f秒", elapsed)
		log.info("=" * 80)
	
	return state


def _summarize_functions(state: AgentState) -> AgentState:
	"""步骤2: 为每个代码函数生成关键字和摘要"""
	start_time = time.time()
	driver = ModelDriver()
	index = state.get("code_index", {})
	allowed = set(state.get("filtered_files", []) or index.keys())
	
	log.info("=" * 80)
	log.info("NODE: SUMMARIZE FUNCTIONS - 开始处理函数并生成摘要")
	log.info("=" * 80)
	log.info("允许的文件数量: %d", len(allowed))
	
	# 收集所有函数信息（过滤前）
	all_functions_before: List[Dict[str, Any]] = []
	for file_path, funcs in index.items():
		if file_path not in allowed:
			continue
		for fn in funcs or []:
			name = str(fn.get("name", ""))
			all_functions_before.append({
				"file": file_path,
				"function": name,
			})
	
	log.info("过滤前: 文件=%d, 函数总数=%d", len(allowed), len(all_functions_before))
	
	# 应用过滤器：只跳过包含过滤关键字的函数，其他函数保留
	skip_names = {"constructor", "getHello", "bootstrap", "__init__"}
	functions_to_summarize: List[Dict[str, Any]] = []
	filtered_out: List[str] = []
	files_completely_filtered: List[str] = []
	
	for file_path, funcs in index.items():
		if file_path not in allowed:
			continue
		
		# 对文件内的每个函数单独判断
		file_has_valid_functions = False
		
		for fn in funcs or []:
			name = str(fn.get("name", ""))
			
			# 记录被过滤的函数
			if not name:
				filtered_out.append(f"{file_path}#{name}")
				continue
			
			# 如果函数名包含过滤关键字，则跳过该函数（但不是跳过整个文件）
			name_lower = name.lower()
			should_skip = False
			for skip_name in skip_names:
				if skip_name.lower() in name_lower:
					should_skip = True
					break
			
			if should_skip:
				filtered_out.append(f"{file_path}#{name}")
				continue
			
			# 保留该函数
			file_has_valid_functions = True
			docstring = str(fn.get("docstring", ""))
			params = fn.get("parameters", [])
			start = int(fn.get("start_line", 1))
			end = int(fn.get("end_line", start))
			
			functions_to_summarize.append({
				"file": file_path,
				"function": name,
				"docstring": docstring,
				"parameters": params,
				"lines": f"{start}-{end}",
			})
		
		# 如果该文件的所有函数都被过滤了，记录该文件
		if not file_has_valid_functions and funcs:
			files_completely_filtered.append(file_path)
	
	log.info("过滤后: 保留函数=%d", len(functions_to_summarize))
	if filtered_out:
		log.info("被过滤掉的函数 (%d个): %s", len(filtered_out), ", ".join(filtered_out[:10]))
	if files_completely_filtered:
		log.info("完全被过滤的文件 (%d个): %s", len(files_completely_filtered), ", ".join(files_completely_filtered[:10]))
	log.info("=" * 80)
	
	# 分批处理函数，使用LLM生成关键字和摘要
	function_summaries: List[Dict[str, Any]] = []
	
	if functions_to_summarize:
		# 每批处理20个函数
		batch_size = 20
		for i in range(0, min(len(functions_to_summarize), 100), batch_size):  # 最多处理100个
			batch = functions_to_summarize[i:i+batch_size]
			
			# 构建函数列表文本
			func_list = []
			for idx, f in enumerate(batch):
				params_str = ", ".join([str(p) for p in f.get("parameters", [])])
				func_list.append(
					f"{idx+1}. {f['function']}({params_str})\n"
					f"   文件: {f['file']}\n"
					f"   文档: {f['docstring'][:100] if f['docstring'] else '无'}"
				)
			func_text = "\n\n".join(func_list)
			
			prompt = (
				"You are a code analysis expert.\n\n"
				"Task: Generate semantic keywords and brief summaries for each function.\n\n"
				"Function list:\n"
				f"{func_text}\n\n"
				"Rules:\n"
				"1) Extract semantic keywords for each function: include action verbs, operation objects, domain concepts\n"
				"2) Keywords should be relevant to function name, parameters, and docstrings\n"
				"3) IMPORTANT: Include synonyms and related operation terms. For example:\n"
				"   - For list/query functions: include 'list', 'query', 'fetch', 'get', 'find', 'retrieve', 'all', 'search'\n"
				"   - For pagination-related functions: include 'paginate', 'page', 'limit', 'offset', 'list', 'query'\n"
				"   - For create functions: include 'create', 'add', 'new', 'insert', 'build', 'make'\n"
				"   - For update functions: include 'update', 'modify', 'change', 'edit', 'set', 'patch'\n"
				"   - For delete functions: include 'delete', 'remove', 'drop', 'clear', 'destroy'\n"
				"4) Consider what operations this function might be related to semantically (e.g., a function that lists items might be related to pagination)\n"
				"5) Summary should describe function purpose in one sentence\n"
				"6) Output JSON array, each element: {\"index\": number, \"keywords\": [keywords], \"summary\": \"summary\"}\n"
				"7) Output ONLY JSON, no other text\n\n"
				"Example:\n"
				"[\n"
				"  {\"index\": 1, \"keywords\": [\"create\", \"channel\", \"add\", \"new\", \"insert\", \"build\"], \"summary\": \"Create a new channel\"},\n"
				"  {\"index\": 2, \"keywords\": [\"send\", \"message\", \"post\", \"publish\", \"deliver\"], \"summary\": \"Send a message to channel\"},\n"
				"  {\"index\": 3, \"keywords\": [\"find\", \"all\", \"channels\", \"list\", \"query\", \"get\", \"fetch\", \"retrieve\"], \"summary\": \"Find all channels\"}\n"
				"]\n"
		)
		
		try:
			batch_start = time.time()
			text = driver.chat(
				prompt=prompt,
				system="You are a code analysis expert. Strictly follow instructions, output ONLY JSON.",
				max_tokens=3000
			)
			batch_elapsed = time.time() - batch_start
			
			log.info("batch %d-%d LLM调用完成, 耗时: %.2f秒", i, i+len(batch), batch_elapsed)
			
			summaries = driver.extract_json(text)
			if summaries:
				for summary_item in summaries:
					idx = int(summary_item.get("index", 0)) - 1
					if 0 <= idx < len(batch):
						func_info = batch[idx].copy()
						func_info["keywords"] = summary_item.get("keywords", [])
						func_info["summary"] = summary_item.get("summary", "")
						function_summaries.append(func_info)
			else:
				raise ValueError("Failed to extract JSON")
			
			log.info("summarized batch %d-%d, total: %d", i, i+len(batch), len(function_summaries))
			
		except Exception as e:
			log.warning("batch %d summarization failed: %s", i, e)
			# Fallback: 使用函数名作为关键字
			for f in batch:
				func_info = f.copy()
				func_info["keywords"] = [f["function"].lower()]
				func_info["summary"] = f.get("docstring", "")[:100]
				function_summaries.append(func_info)
	
	state["function_summaries"] = function_summaries
	elapsed = time.time() - start_time
	log.info("function summarization complete: %d functions", len(function_summaries))
	log.info("NODE: SUMMARIZE FUNCTIONS - 完成，耗时: %.2f秒", elapsed)
	log.info("=" * 80)
	return state



def _match_categories_with_functions(state: AgentState) -> AgentState:
	"""步骤3: 匹配功能类别的关键字和函数的关键字，找到相关代码"""
	start_time = time.time()
	categories = state.get("categories", [])
	function_summaries = state.get("function_summaries", [])
	
	log.info("=" * 80)
	log.info("NODE: MATCH CATEGORIES - 开始匹配功能类别和函数")
	log.info("=" * 80)
	log.info("matching: cats=%d functions=%d", len(categories), len(function_summaries))
	
	if not categories or not function_summaries:
		state["mappings"] = []
		return state
	
	driver = ModelDriver()
	
	def normalize_keyword(kw: str) -> str:
		"""标准化关键字，去除大小写和特殊字符"""
		import re
		return re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', kw.lower())
	
	def keyword_similarity(cat_keywords: List[str], func_keywords: List[str]) -> float:
		"""计算两组关键字的字符串相似度（快速路径）"""
		if not cat_keywords or not func_keywords:
			return 0.0
		
		cat_set = set(normalize_keyword(kw) for kw in cat_keywords)
		func_set = set(normalize_keyword(kw) for kw in func_keywords)
		
		# 计算交集
		intersection = cat_set & func_set
		if not intersection:
			# 尝试部分匹配
			partial_match = 0
			for ck in cat_set:
				for fk in func_set:
					if ck in fk or fk in ck:
						partial_match += 1
						break
			return partial_match / max(len(cat_set), len(func_set)) * 0.5
		
		# Jaccard相似度
		union = cat_set | func_set
		return len(intersection) / len(union)
	
	def semantic_similarity_llm(
		cat_name: str,
		cat_desc: str,
		cat_keywords: List[str],
		func_name: str,
		func_summary: str,
		func_keywords: List[str]
	) -> float:
		"""使用 LLM 评估语义相似度"""
		prompt = (
			"You are a code analysis expert. Evaluate the semantic similarity between a feature requirement and a function.\n\n"
			"Feature Requirement:\n"
			f"  Name: {cat_name}\n"
			f"  Description: {cat_desc}\n"
			f"  Keywords: {', '.join(cat_keywords)}\n\n"
			"Function:\n"
			f"  Name: {func_name}\n"
			f"  Summary: {func_summary}\n"
			f"  Keywords: {', '.join(func_keywords)}\n\n"
			"Task: Determine if this function is semantically related to the feature requirement.\n"
			"Consider:\n"
			"- Semantic relationships (e.g., 'pagination' is related to 'list', 'query', 'find', 'get')\n"
			"- Functional relationships (e.g., pagination often involves listing/querying operations)\n"
			"- Synonyms and related concepts\n"
			"- Whether the function could be used to implement or support the feature\n\n"
			"Output ONLY a JSON object with a single field 'similarity' containing a float between 0.0 and 1.0:\n"
			"- 0.0-0.3: Not related\n"
			"- 0.3-0.6: Somewhat related\n"
			"- 0.6-0.8: Related\n"
			"- 0.8-1.0: Highly related\n\n"
			"Example: {\"similarity\": 0.75}\n"
		)
		
		try:
			sem_start = time.time()
			text = driver.chat(
				prompt=prompt,
				system="You are a code analysis expert. Output ONLY valid JSON, no explanations.",
				max_tokens=100
			)
			sem_elapsed = time.time() - sem_start
			result = driver.extract_json(text)
			if result and isinstance(result.get("similarity"), (int, float)):
				similarity = float(result["similarity"])
				log.debug("LLM语义相似度评估: %s -> %s = %.2f (耗时: %.2f秒)", 
					cat_name, func_name, similarity, sem_elapsed)
				return similarity
		except Exception as e:
			log.debug("LLM semantic similarity evaluation failed: %s", e)
		
		return 0.0
	
	mappings: List[Dict[str, Any]] = []
	llm_calls_count = 0
	llm_total_time = 0.0
	
	for cat_idx, cat in enumerate(categories, 1):
		cat_name = cat.get("name", "")
		cat_desc = cat.get("description", "")
		cat_keywords = cat.get("keywords", [])
		
		if not cat_keywords:
			log.warning("category '%s' has no keywords, using name for matching", cat_name)
			# 使用功能名称作为关键字
			cat_keywords = [cat_name]
		
		# 对每个函数计算相似度
		scored_functions: List[Tuple[Dict[str, Any], float]] = []
		
		for func in function_summaries:
			func_keywords = func.get("keywords", [])
			func_summary = func.get("summary", "")
			file_path = func.get("file", "")
			
			# 过滤测试文件
			path_low = file_path.lower()
			if any(t in path_low for t in ["/test/", "tests", ".spec.", ".e2e.", "test_", "_test."]):
				continue
			
			# 计算关键字字符串相似度（快速路径）
			sim_score = keyword_similarity(cat_keywords, func_keywords)
			
			# 额外加分：如果函数名或文件名包含类别关键字
			func_name = func.get("function", "")
			func_name_lower = func_name.lower()
			bonus = 0.0
			for kw in cat_keywords:
				kw_norm = normalize_keyword(kw)
				if kw_norm in func_name_lower or kw_norm in path_low:
					bonus += 0.2
			
			total_score = sim_score + bonus
			
			# 如果字符串匹配得分较低，使用 LLM 进行语义相似度评估
			if total_score < 0.3:
				sem_call_start = time.time()
				semantic_score = semantic_similarity_llm(
					cat_name=cat_name,
					cat_desc=cat_desc,
					cat_keywords=cat_keywords,
					func_name=func_name,
					func_summary=func_summary,
					func_keywords=func_keywords
				)
				sem_call_elapsed = time.time() - sem_call_start
				llm_calls_count += 1
				llm_total_time += sem_call_elapsed
				# 使用语义相似度作为补充，取两者最大值
				total_score = max(total_score, semantic_score * 0.8)  # 稍微降低 LLM 分数的权重
			
			if total_score > 0.1:  # 只保留有一定相关性的
				scored_functions.append((func, total_score))
		
		# 排序并取top 5
		scored_functions.sort(key=lambda x: x[1], reverse=True)
		top_functions = scored_functions[:5]
		
		log.info("category %d/%d '%s': 找到 %d 个候选函数, top 5 分数: %s", 
			cat_idx, len(categories), cat_name, len(scored_functions),
			[f"{score:.2f}" for _, score in top_functions[:5]])
		
		locs: List[Dict[str, Any]] = []
		for func, score in top_functions:
			log.debug("matched %s -> %s (score: %.4f)", cat_name, func.get("function"), score)
			# 只输出必要字段，不包含 summary 和 score
			locs.append({
				"file": func.get("file"),
				"function": func.get("function"),
				"lines": func.get("lines"),
			})
		
		if locs:
			mappings.append({
				"feature_description": cat_name,
				"feature_summary": cat_desc,
				"implementation_location": locs,
			})
		else:
			log.info("no match found for category: %s", cat_name)
			mappings.append({
				"feature_description": cat_name,
				"feature_summary": cat_desc,
				"implementation_location": [],
			})
	
	state["mappings"] = mappings
	elapsed = time.time() - start_time
	log.info("matching complete: %d mappings", len(mappings))
	if llm_calls_count > 0:
		log.info("LLM语义相似度评估: 调用次数=%d, 总耗时=%.2f秒, 平均耗时=%.2f秒", 
			llm_calls_count, llm_total_time, llm_total_time / llm_calls_count)
	log.info("NODE: MATCH CATEGORIES - 完成，耗时: %.2f秒", elapsed)
	log.info("=" * 80)
	return state




def _run_verification_workflow(state: AgentState) -> AgentState:
	start_time = time.time()
	
	try:
		log.info("=" * 80)
		log.info("NODE: VERIFY - 开始验证工作流")
		log.info("=" * 80)
		
		# 从报告中获取功能分析结果
		report = state.get("report", {})
		if not report:
			log.warning("No report available for verification")
			state["verification"] = {}
			return state
		
		# 运行验证工作流（仅 generate_test_code 受 test_qcoder 影响）
		test_qcoder = bool(state.get("test_qcoder", True))
		verification_result = run_functional_verification(report, state.get("repo_root", ""), test_qcoder=test_qcoder)
		state["verification"] = verification_result
		
		# 打印验证摘要
		verifications = verification_result.get("functional_verification", [])
		elapsed = time.time() - start_time
		if verifications:
			log.info("=" * 80)
			log.info("Verification Summary:")
			for i, v in enumerate(verifications, 1):
				status = "PASSED" if v["execution_result"]["tests_passed"] else "FAILED"
				log.info("%d. %s - %s", i, v["feature"][:60], status)
			log.info("NODE: VERIFY - 完成，耗时: %.2f秒", elapsed)
			log.info("=" * 80)
		else:
			log.info("NODE: VERIFY - 完成（无验证结果），耗时: %.2f秒", elapsed)
			log.info("=" * 80)
		
	except Exception as e:
		elapsed = time.time() - start_time
		log.exception("Verification workflow failed (non-fatal): %s", e)
		state["verification"] = {}
		log.info("NODE: VERIFY - 完成（失败），耗时: %.2f秒", elapsed)
		log.info("=" * 80)
	
	return state


def _assemble_report(state: AgentState) -> AgentState:
	"""组装最终报告，包括功能分析和执行计划"""
	start_time = time.time()
	log.info("=" * 80)
	log.info("NODE: ASSEMBLE - 开始组装报告")
	log.info("=" * 80)
	categories = state.get("categories", [])
	mappings = state.get("mappings", [])
	
	# 构建简洁的文本摘要
	lines: List[str] = []
	for m in mappings[:6]:
		feat = m.get("feature_description", "")
		locs = m.get("implementation_location", [])
		if locs:
			loc0 = locs[0]
			lines.append(f"- {feat}: {loc0.get('file','')}#{loc0.get('function') or ''}")
		else:
			lines.append(f"- {feat}: 未找到匹配实现")
	summary = "\n".join(lines)
	
	# 生成执行计划建议
	execution_plan = build_execution_suggestion(state.get("repo_root", ""))
	
	# 清理 mappings，移除 feature_summary 字段（保持输出格式一致）
	clean_mappings = []
	for m in mappings:
		clean_mappings.append({
			"feature_description": m.get("feature_description", ""),
			"implementation_location": m.get("implementation_location", [])
		})
	
	report = {
		"categories": categories,
		"feature_analysis": clean_mappings,
		"summary": summary,
		"execution_plan_suggestion": execution_plan,
	}
	state["report"] = report
	elapsed = time.time() - start_time
	log.info("report assembled: %d features, exec_plan_len=%d", len(clean_mappings), len(execution_plan))
	
	# 打印最终 report 的具体内容
	import json
	log.info("=== 最终 Report 内容 ===")
	log.info(json.dumps(report, ensure_ascii=False, indent=2))
	log.info("NODE: ASSEMBLE - 完成，耗时: %.2f秒", elapsed)
	log.info("=" * 80)
	
	return state


def build_graph():
	"""构建新的分析工作流（带可选的验证工作流）：
	
	分析工作流（主流程）：
	1. index - 索引代码库
	2. filter - 过滤文件
	3. categorize - 根据原始需求直接生成功能类别和关键字
	4. summarize_functions - 为每个函数生成关键字和摘要
	5. match_categories - 匹配功能类别和函数
	6. assemble - 生成报告
	
	验证工作流（可选）：
	7. verify - 基于分析结果生成和执行测试代码（带自修复）
	"""
	builder = StateGraph(AgentState)
	
	# 分析工作流节点
	builder.add_node("index", _node_index)
	builder.add_node("filter", _filter_files)
	builder.add_node("categorize", _categorize_problem)
	builder.add_node("summarize_functions", _summarize_functions)
	builder.add_node("match_categories", _match_categories_with_functions)
	builder.add_node("assemble", _assemble_report)
	
	# 验证工作流节点（可选）
	builder.add_node("verify", _run_verification_workflow)
	
	# 构建分析工作流
	builder.set_entry_point("index")
	builder.add_edge("index", "filter")
	builder.add_edge("filter", "categorize")
	builder.add_edge("categorize", "summarize_functions")
	builder.add_edge("summarize_functions", "match_categories")
	builder.add_edge("match_categories", "assemble")
	
	# 验证工作流在报告生成后运行（如果启用）
	builder.add_edge("assemble", "verify")
	builder.add_edge("verify", END)
	
	return builder.compile()


_GRAPH = build_graph()


def run_analysis(problem_description: str, repo_root: str, test_qcoder: bool = True) -> Dict[str, Any]:
	"""运行完整的代码分析流程（包括可选的验证工作流）
	
	Args:
		problem_description: 用户需求描述
		repo_root: 代码库根目录
		
	Returns:
		包含 feature_analysis、execution_plan_suggestion 和可选的 functional_verification 的报告
	"""
	total_start_time = time.time()
	log.info("=" * 80)
	log.info("开始运行代码分析流程")
	log.info("=" * 80)
	log.info("run_analysis start: repo=%s verification_enabled=%s", repo_root, enable_verification())
	initial: AgentState = {
		"problem_description": problem_description,
		"repo_root": repo_root,
		"test_qcoder": test_qcoder,
	}
	final_state = _GRAPH.invoke(initial)
	report = final_state.get("report", {})
	
	# 确保返回格式正确
	if not report:
		log.warning("report is empty, creating default")
		report = {
			"feature_analysis": [],
			"execution_plan_suggestion": ""
		}
	
	# 添加验证结果（如果有）
	verification = final_state.get("verification", {})
	if verification and verification.get("functional_verification"):
		report["functional_verification"] = verification["functional_verification"]
	
	# 打印结果内容（包含 categories，仅用于日志）
	log.info("run_analysis done: features=%d", len(report.get("feature_analysis", [])))
	log.info("=" * 80)
	log.info("分析结果 (包含 categories):")
	log.info("=" * 80)
	
	import json
	log.info(json.dumps(report, ensure_ascii=False, indent=2))
	
	log.info("=" * 80)
	log.info("功能摘要:")
	for i, feature in enumerate(report.get("feature_analysis", []), 1):
		log.info(f"{i}. {feature.get('feature_description', '')}")
		for loc in feature.get('implementation_location', []):
			log.info(f"   - {loc.get('file', '')}#{loc.get('function', '')} ({loc.get('lines', '')})")
	log.info("=" * 80)
	
	# 打印 categories（仅日志）
	if "categories" in report:
		log.info("=" * 80)
		log.info("生成的功能分类 (categories - 仅日志，不返回给客户端):")
		log.info("=" * 80)
		for i, cat in enumerate(report.get("categories", []), 1):
			log.info(f"{i}. {cat.get('name', '')} ({cat.get('type', 'business')})")
			log.info(f"   描述: {cat.get('description', '')}")
			log.info(f"   关键字: {', '.join(cat.get('keywords', []))}")
		log.info("=" * 80)
	
	# 打印验证摘要
	if "functional_verification" in report:
		log.info("=" * 80)
		log.info("验证结果摘要:")
		log.info("=" * 80)
		for i, v in enumerate(report["functional_verification"], 1):
			status = "PASSED" if v["execution_result"]["tests_passed"] else "FAILED"
			attempts = v["execution_result"].get("attempts", 1)
			log.info(f"{i}. {v['feature'][:60]} - {status} (attempts: {attempts})")
		log.info("=" * 80)
	
	# 从返回给客户端的响应中移除 categories（仅保留在日志中）
	client_report = {k: v for k, v in report.items() if k not in ("categories", "summary")}
	# 进一步确保不返回 feature_analysis 中的 summary/feature_summary 字段
	# fa = client_report.get("feature_analysis")
	# if isinstance(fa, list):
	# 	clean_fa = []
	# 	for item in fa:
	# 		if isinstance(item, dict):
	# 			clean_item = {ik: iv for ik, iv in item.items() if ik not in ("summary", "feature_summary")}
	# 			clean_fa.append(clean_item)
	# 		else:
	# 			clean_fa.append(item)
	# 	client_report["feature_analysis"] = clean_fa
	# log.info("categories 与 summary 已从客户端响应中移除，且已清理 feature_analysis 内的 summary 字段")
	
	total_elapsed = time.time() - total_start_time
	log.info("=" * 80)
	log.info("代码分析流程完成，总耗时: %.2f秒 (%.2f分钟)", total_elapsed, total_elapsed / 60)
	log.info("=" * 80)
	
	return client_report
