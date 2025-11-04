import os
import re
from typing import Dict, List, Tuple

from app.utils.logger import get_logger

log = get_logger("code_parser")

EXCLUDE_DIRS = {
	".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv", ".next", ".idea", ".vscode"
}

TEXT_EXTS = {
	".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt", ".rs", ".cs", ".php", ".rb", ".swift"
}


def walk_repository(root_dir: str) -> List[str]:
	files: List[str] = []
	for base, dirs, filenames in os.walk(root_dir):
		dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
		for name in filenames:
			files.append(os.path.join(base, name))
	log.info("walk_repository: files=%d", len(files))
	return files


def read_text(path: str, max_bytes: int = 2_000_000) -> str:
	try:
		with open(path, "rb") as f:
			data = f.read(max_bytes)
		return data.decode("utf-8", errors="ignore")
	except Exception:
		return ""


def guess_language(path: str) -> str:
	ext = os.path.splitext(path)[1].lower()
	return ext


def extract_functions(path: str, content: str) -> List[Dict[str, object]]:
	lines = content.splitlines()
	results: List[Dict[str, object]] = []
	lang = guess_language(path)

	def add_result(name: str, start: int, end: int, docstring: str = "", parameters: List[str] = None) -> None:
		results.append({
			"name": name,
			"start_line": start,
			"end_line": end,
			"docstring": docstring,
			"parameters": parameters or [],
			"language": lang[1:],  # Remove the dot
		})

	def find_block_end_brace(start_line: int) -> int:
		depth = 0
		for i in range(start_line - 1, len(lines)):
			depth += lines[i].count("{")
			depth -= lines[i].count("}")
			if i >= start_line - 1 and depth == 0:
				return i + 1
		return start_line

	def find_block_end_indent(start_line: int) -> int:
		start_indent = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
		for i in range(start_line, len(lines)):
			indent = len(lines[i]) - len(lines[i].lstrip())
			if lines[i].lstrip().startswith(("def ", "class ")) and indent <= start_indent:
				return i
		return len(lines)

	def extract_docstring(start_line: int) -> str:
		"""Extract docstring from function."""
		if start_line >= len(lines):
			return ""
		# Look for docstring in next few lines
		for i in range(start_line, min(start_line + 5, len(lines))):
			line = lines[i].strip()
			if line.startswith('"""') or line.startswith("'''"):
				# Find closing docstring
				quote = '"""' if line.startswith('"""') else "'''"
				if line.endswith(quote) and len(line) > 3:
					return line[3:-3].strip()
				# Multi-line docstring
				doc_lines = [line[3:]]
				for j in range(i + 1, min(i + 10, len(lines))):
					doc_line = lines[j]
					if quote in doc_line:
						doc_lines.append(doc_line.split(quote)[0])
						return "\n".join(doc_lines).strip()
					doc_lines.append(doc_line)
		return ""

	def extract_parameters(line: str) -> List[str]:
		"""Extract parameter names from function signature."""
		params = []
		# Simple parameter extraction
		if '(' in line and ')' in line:
			param_part = line[line.find('(')+1:line.rfind(')')]
			if param_part.strip():
				# Split by comma and extract parameter names
				for param in param_part.split(','):
					param = param.strip()
					# Remove type annotations and default values
					if ':' in param:
						param = param.split(':')[0].strip()
					if '=' in param:
						param = param.split('=')[0].strip()
					if param and param not in ['self', 'this']:
						params.append(param)
		return params

	# Python
	if lang == ".py":
		for i, line in enumerate(lines, start=1):
			m = re.match(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
			if m:
				name = m.group(1)
				end = find_block_end_indent(i)
				docstring = extract_docstring(i)
				params = extract_parameters(line)
				add_result(name, i, end, docstring, params)
	# JS/TS
	if lang in {".js", ".jsx", ".ts", ".tsx"}:
		for i, line in enumerate(lines, start=1):
			# Skip control-flow statements (avoid misidentifying as functions)
			if re.match(r"^\s*(if|for|while|switch|catch|try)\b", line):
				continue
			
			# function foo(...) {
			m1 = re.search(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
			if m1:
				name = m1.group(1)
				end = find_block_end_brace(i)
				docstring = extract_docstring(i)
				params = extract_parameters(line)
				add_result(name, i, end, docstring, params)
				continue
			
			# const foo = (...) => {
			m2 = re.search(r"(const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\([^)]*\)\s*=>\s*\{", line)
			if m2:
				name = m2.group(2)
				end = find_block_end_brace(i)
				docstring = extract_docstring(i)
				params = extract_parameters(line)
				add_result(name, i, end, docstring, params)
				continue
			
			# TypeScript/JS class methods: match method name followed by (
			# Handles: methodName(...) {  or  methodName(...): Type {
			# Also matches methods with decorators on previous lines
			m3 = re.match(r"^\s*(?:public|private|protected)?\s*(?:async)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
			if m3:
				name = m3.group(1)
				# filter out control keywords accidentally matched
				if name in {"if", "for", "while", "switch", "catch", "try", "async"}:
					continue
				end = find_block_end_brace(i)
				docstring = extract_docstring(i)
				params = extract_parameters(line)
				add_result(name, i, end, docstring, params)
	# Go
	if lang == ".go":
		for i, line in enumerate(lines, start=1):
			m = re.match(r"^func\s+(?:\(.*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
			if m:
				name = m.group(1)
				end = find_block_end_brace(i)
				add_result(name, i, end)
	# Java
	if lang == ".java":
		for i, line in enumerate(lines, start=1):
			m = re.search(r"(public|private|protected|static|final|synchronized|abstract|native)\s+[\w\<\>\[\]]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
			if m:
				name = m.group(2)
				end = find_block_end_brace(i)
				add_result(name, i, end)

	return results


def index_repository(root_dir: str) -> Dict[str, List[Dict[str, object]]]:
	index: Dict[str, List[Dict[str, object]]] = {}
	all_files = walk_repository(root_dir)
	for abs_path in all_files:
		rel_path = os.path.relpath(abs_path, root_dir)
		ext = os.path.splitext(rel_path)[1].lower()
		if ext in TEXT_EXTS:
			content = read_text(abs_path)
			funcs = extract_functions(rel_path, content)
			index[rel_path] = funcs
	log.info("index_repository: text_files=%d", len(index))
	return index


def get_code_snippets(root_dir: str, mappings: List[Dict[str, object]], max_lines: int = 500) -> str:
	"""
	Extract actual code snippets from files based on mappings.
	Returns formatted code snippets for LLM analysis.
	"""
	snippets = []
	total_lines = 0
	
	for mapping in mappings[:5]:  # Limit to top 5 features
		locations = mapping.get("implementation_location", [])
		for loc in locations:
			file_path = loc.get("file")
			if not file_path:
				continue
			
			# Build absolute path
			abs_path = os.path.join(root_dir, file_path)
			if not os.path.exists(abs_path):
				continue
			
			# Read file content
			content = read_text(abs_path)
			if not content:
				continue
			
			lines = content.splitlines()
			line_range = loc.get("lines", "")
			
			# Parse line range
			start_line, end_line = 1, len(lines)
			if "-" in line_range:
				try:
					parts = line_range.split("-")
					start_line = int(parts[0])
					end_line = int(parts[1])
				except ValueError:
					pass
			
			# Extract code snippet (with context)
			func_name = loc.get("function", "")
			snippet_lines = lines[max(0, start_line - 5):min(len(lines), end_line + 2)]
			
			# Format snippet
			feature = mapping.get("feature_description", "")
			snippet_text = f"\n# Feature: {feature}\n# File: {file_path}"
			if func_name:
				snippet_text += f"\n# Function: {func_name}"
			snippet_text += f"\n# Lines: {start_line}-{end_line}\n\n"
			snippet_text += "\n".join(snippet_lines)
			
			snippets.append(snippet_text)
			total_lines += len(snippet_lines)
			
			# Stop if too long
			if total_lines > max_lines:
				break
		if total_lines > max_lines:
			break
	
	return "\n\n".join(snippets)


def get_key_files_content(root_dir: str, max_files: int = 10, max_lines_per_file: int = 50) -> str:
	"""
	Extract content from key files (entry points, config files, etc.)
	for execution suggestion generation.
	"""
	files = list(os.path.relpath(p, root_dir) for p in walk_repository(root_dir))
	
	# Prioritize important files
	priority_files = []
	other_files = []
	
	for f in files:
		name = os.path.basename(f).lower()
		if any(name.endswith(ext) for ext in [".go", ".py", ".js", ".ts", ".java", ".rs", ".cpp"]):
			# Check for main files
			if any(pattern in f.lower() for pattern in ["main", "app", "server", "index"]):
				priority_files.append(f)
			else:
				other_files.append(f)
	
	# Take priority files first, then others
	selected_files = (priority_files + other_files)[:max_files]
	
	snippets = []
	total_lines = 0
	
	for f in selected_files:
		abs_path = os.path.join(root_dir, f)
		if not os.path.exists(abs_path):
			continue
		
		content = read_text(abs_path)
		if not content:
			continue
		
		lines = content.splitlines()
		snippet = lines[:max_lines_per_file]
		snippets.append(f"# File: {f}\n{chr(10).join(snippet)}")
		
		total_lines += len(snippet)
		if total_lines > 300:  # Hard limit
			break
	
	return "\n\n".join(snippets)


def build_execution_suggestion(root_dir: str) -> str:
	"""
	Generate execution suggestion by having LLM analyze project files.
	Always returns a non-empty string.
	"""
	from app.agents.model_driver import ModelDriver
	
	files = list(os.path.relpath(p, root_dir) for p in walk_repository(root_dir))

	# Use LLM to analyze and generate suggestion
	try:
		driver = ModelDriver()
		
		# Get actual code content from key files
		code_snippets = get_key_files_content(root_dir, max_files=15, max_lines_per_file=80)
		file_sample = files[:50]
		
		prompt = f"""You are an expert developer assistant analyzing a code repository.

Project file list:
{chr(10).join(file_sample)}

Key code files content:
{code_snippets}

Your task:
1. Identify the primary programming language and framework
2. Provide step-by-step commands to run this project
3. Include installation, build, and run steps
4. Specify the likely service URL/port if it's a web service

Output format (one line, semicolon-separated steps):
<install_deps>; <build_step>; <run_step>; <access_info>

Examples:
- Node.js GraphQL: "npm install; npm run start:dev; 该服务是一个 GraphQL API，可在 http://localhost:3000/graphql 访问"
- NestJS: "npm install; npm run start:dev; NestJS 应用，GraphQL playground 通常在 http://localhost:3000/graphql"
- Go service: "go mod download; go run main.go; Go 服务默认在 http://localhost:8080 访问"
- Python FastAPI: "pip install -r requirements.txt; uvicorn main:app --reload; 访问 http://localhost:8000/docs"
- Java Spring: "mvn clean install; mvn spring-boot:run; 访问 http://localhost:8080"

Requirements:
1. Output ONE LINE only, semicolon-separated
2. Include: dependencies install; build (if needed); run command; access info
3. Be specific about GraphQL/REST endpoint paths
4. No extra explanation or formatting

Output:"""
		
		suggestion = driver.chat(
			prompt=prompt,
			system="You are an expert developer assistant. Output only the execution plan, no extra text.",
			max_tokens=500,
			qcoder=True
		).strip()
		
		# Clean up if wrapped in quotes or extra formatting
		suggestion = suggestion.strip('"\'`')
		
		if suggestion and len(suggestion) > 10:
			log.info("LLM generated suggestion: %s", suggestion[:100])
			return suggestion
		else:
			log.warning("LLM returned empty/invalid suggestion")
	except Exception as e:
		log.warning("LLM suggestion generation failed: %s", e)
	
	# Fallback: simple heuristic-based suggestion (avoid Angular false-positives)
	log.info("using fallback suggestion generation")
	has_package_json = any("package.json" in f for f in files)
	has_go_mod = any("go.mod" in f for f in files)
	has_requirements = any("requirements.txt" in f for f in files)
	has_pom = any("pom.xml" in f for f in files)
	has_cargo = any("Cargo.toml" in f for f in files)
	# Detect Angular workspace
	is_angular = any("angular.json" in f for f in files)
	
	if has_package_json and not is_angular:
		# Detect GraphQL by file patterns
		has_graphql = any("graphql" in f.lower() or "gql" in f.lower() for f in files[:100])
		has_docker = any("dockerfile" in f.lower() or "docker-compose" in f.lower() for f in files[:50])
		
		cmd_parts = ["npm install"]
		# Try to detect dev command
		cmd_parts.append("npm run start:dev" if any("nest" in f.lower() for f in files[:50]) else "npm run start")
		
		# Build info message
		info = "Node.js/TypeScript 应用"
		if has_graphql:
			info += "，GraphQL API 通常在 http://localhost:3000/graphql"
		elif has_docker:
			info += "，已配置 Docker，可使用 docker-compose up 启动"
		else:
			info += "，默认端口通常是 3000 或 8080"
		
		return "; ".join(cmd_parts) + "; " + info
	elif has_package_json and is_angular:
		return "npm install; npm run start; Angular 前端应用，后端 API 请查看项目文档"
	elif has_go_mod:
		return "go mod download; go build; go run main.go; Go 服务默认可能在 http://localhost:8080 访问"
	elif has_requirements:
		return "pip install -r requirements.txt; python main.py; Python 服务启动后可在 http://localhost:5000 或 8000 访问"
	elif has_pom:
		return "mvn clean install; mvn spring-boot:run; Java 应用通常在 http://localhost:8080 访问"
	elif has_cargo:
		return "cargo build; cargo run; Rust 应用根据配置访问对应端口"
	else:
		return "查看项目 README 了解启动方式; 常见命令: npm start / python main.py / go run main.go"
