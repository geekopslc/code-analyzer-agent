"""服务启动管理模块 - 自动检测和启动后端服务"""
import os
import re
import subprocess
import time
import socket
import threading
from typing import Optional, Dict, Any
import json
from app.utils.logger import get_logger
from app.utils.code_parser import read_text
from app.agents.model_driver import ModelDriver

log = get_logger("service_starter")

# 默认检测端口
DEFAULT_PORT = 3000
# 默认 Docker 镜像（可通过环境变量 SERVICE_STARTER_DOCKER_IMAGE 覆盖）
DEFAULT_DOCKER_IMAGE = "lcax200000/python-node-nest:0.1"
# 端口检测重试次数
PORT_CHECK_RETRIES = 20
# 每次重试等待时间（秒）
PORT_CHECK_WAIT = 2


def detect_service_startup(repo_root: str, driver: ModelDriver) -> Optional[str]:
    """扫描部署文档并由 LLM 生成启动命令（不包含依赖安装）
    
    Args:
        repo_root: 代码库根目录
        driver: LLM 驱动器
        
    Returns:
        启动命令字符串（只包含服务启动命令，不包含依赖安装），
        如果无法检测则返回 None。
        
    示例:
        - "npm start"
        - "python app.py"
        - "yarn run dev"
    """
    log.info("Scanning repository for startup documentation...")
    
    # 候选文档文件（递归搜索）
    candidates = []
    
    # 需要搜索的文件名模式
    doc_patterns = [
        "README.md", "readme.md", "ReadMe.txt", "README.txt", "README",
        "start.sh", "run.sh", "startup.sh", "start.bat"
    ]
    
    # 需要忽略的目录
    ignore_dirs = {
        "node_modules", ".git", "dist", "build", "__pycache__", 
        ".venv", "venv", "env", "target", ".next", ".nuxt",
        "coverage", ".pytest_cache", ".mypy_cache"
    }
    
    # 递归搜索文档文件（限制深度为 3 层）
    max_depth = 3
    found_files = []
    
    for root, dirs, files in os.walk(repo_root):
        # 计算当前深度
        depth = root[len(repo_root):].count(os.sep)
        if depth >= max_depth:
            dirs.clear()  # 不再深入子目录
            continue
        
        # 过滤掉需要忽略的目录
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        
        # 查找匹配的文件
        for filename in files:
            if filename in doc_patterns:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, repo_root)
                found_files.append((file_path, rel_path))
                log.debug("Found documentation file: %s", rel_path)
    
    # 优先级排序：根目录 > docs 目录 > 其他目录
    def get_priority(path_tuple):
        rel_path = path_tuple[1]
        filename = os.path.basename(rel_path)
        
        # README 文件优先级最高
        if filename.lower().startswith("readme"):
            if os.sep not in rel_path:  # 根目录
                return 0
            elif rel_path.startswith("docs"):  # docs 目录
                return 1
            else:
                return 2
        # package.json 和 Makefile 优先级次之
        elif filename in ["package.json", "Makefile"]:
            return 3
        # docker-compose 和启动脚本
        else:
            return 4
    
    found_files.sort(key=get_priority)
    
    # 读取文件内容（最多 10 个文件）
    for file_path, rel_path in found_files[:10]:
        content = read_text(file_path, max_bytes=20000)
        if content:
            candidates.append(f"=== {rel_path} ===\n{content}")
            log.debug("Loaded documentation file: %s", rel_path)

    if not candidates:
        log.warning("No deployment documentation found")
        return None

    # 限制文档长度，避免超出 LLM context
    joined_docs = "\n\n".join(candidates)[:8000]

    # 构建 prompt - 只获取启动命令，不包含依赖安装
    prompt = f"""You are a deployment assistant. Read the following documentation snippets and extract the **startup command** to run the service.

Documentation:
{joined_docs}

Rules:
1. Extract ONLY the startup command, DO NOT include dependency installation (npm install, pip install, etc.):
   - For Node.js: 'npm start', 'npm run dev', 'yarn start', 'pnpm start', or 'node server.js'
   - For Python: 'python app.py', 'python main.py', 'uvicorn main:app', or 'python -m module'
   - For Docker: 'docker-compose up -d'
2. Output ONLY the startup command string, no explanation, no markdown, no dependency installation.

Example outputs (startup commands only):
- npm start
- yarn run dev
- pnpm start
- python app.py
- uvicorn main:app --port 3000
- node server.js
"""

    response = driver.chat(prompt, system="You are an ops engineer. Output only the startup command, no dependency installation.")
    
    if not response:
        log.warning("LLM returned empty response for startup command detection")
        return None

    # 清理响应，提取命令
    response = response.strip()
    
    # 移除 markdown 代码块
    if response.startswith("```"):
        lines = response.split("\n")
        response = "\n".join(lines[1:-1] if len(lines) > 2 else lines)
    
    # 提取命令（只包含启动命令，不包含依赖安装）
    cmd = response.strip().split("\n")[0].strip()
    
    # 移除可能包含的依赖安装命令（确保只返回启动命令）
    if "&&" in cmd:
        # 如果有多个命令，取最后一个（启动命令）
        parts = [p.strip() for p in cmd.split("&&")]
        # 找到第一个非install命令
        for part in parts:
            if "install" not in part.lower():
                cmd = part
                break
    
    if not cmd:
        log.warning("Could not extract valid startup command")
        return None
    
    log.info("Detected startup command (without dependency installation): %s", cmd)
    return cmd


def _build_install_command(repo_root: str, startup_cmd: str) -> Optional[str]:
    """根据项目类型生成依赖安装命令（使用中国镜像源）
    
    Args:
        repo_root: 代码库根目录
        startup_cmd: 启动命令
        
    Returns:
        带依赖安装的完整命令字符串（包含依赖安装 + 启动），如果无法检测则返回 None
        
    示例:
        - "npm cache clean --force && npm install --registry=https://registry.npmmirror.com && npm install sqlite3 --build-from-source --registry=https://registry.npmmirror.com && npm run build && npm start"
        - "pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple && python app.py"
    """
    install_cmd = None
    
    # Node.js 项目
    if os.path.exists(os.path.join(repo_root, "package.json")):
        pm = _detect_package_manager(repo_root)
        registry = "https://registry.npmmirror.com"
        install_cmd = f"npm cache clean --force && {pm} install --registry={registry} && npm install sqlite3 --build-from-source --registry={registry} && npm run build"
        log.info(f"Detected Node.js project, will use: {install_cmd}")
    
    # Python pip 项目
    elif os.path.exists(os.path.join(repo_root, "requirements.txt")):
        mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
        install_cmd = f"pip install -r requirements.txt -i {mirror}"
        log.info(f"Detected Python pip project, will use: {install_cmd}")
    
    # Python poetry 项目
    elif os.path.exists(os.path.join(repo_root, "pyproject.toml")):
        mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
        install_cmd = f"poetry config pypi-mirror.url {mirror} && poetry install"
        log.info(f"Detected Python poetry project, will use: {install_cmd}")
    
    if install_cmd and startup_cmd:
        return f"{install_cmd} && {startup_cmd}"
    
    return None


def _log_process_output(proc: subprocess.Popen, prefix: str = "Service"):
    """持续读取并打印进程输出到日志（在后台线程中运行）
    
    Args:
        proc: 进程对象
        prefix: 日志前缀
    """
    try:
        if proc.stdout:
            for line in iter(proc.stdout.readline, ''):
                if line:
                    line = line.rstrip()
                    if line:
                        log.info("%s: %s", prefix, line)
                # 如果进程已经结束，退出循环
                if proc.poll() is not None:
                    break
    except Exception as e:
        log.debug("Error reading process output: %s", e)


def is_port_open(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> bool:
    """检查端口是否可连接
    
    Args:
        port: 端口号
        host: 主机地址
        
    Returns:
        True 如果端口可连接，否则 False
    """
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except (OSError, socket.timeout):
        return False


def start_service(command: str, cwd: str, port: int = DEFAULT_PORT) -> Optional[subprocess.Popen]:
    """启动服务进程（支持多步骤命令）
    
    Args:
        command: 启动命令（可以包含 && 连接的多个步骤，如 "npm install && npm start"）
        cwd: 工作目录
        port: 服务端口（用于检测）
        
    Returns:
        服务进程对象，如果启动失败则返回 None
        
    注意:
        - 命令使用 shell=True 执行，支持 && 操作符
        - 只有前一个命令成功时，后续命令才会执行
        - 依赖安装失败会导致整个命令失败，服务不会启动
    """
    log.info("Starting service with command: %s", command)
    log.info("Working directory: %s", cwd)
    
    try:
        # 启动服务进程
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1  # 行缓冲，确保输出及时打印
        )
        
        log.info("正在安装依赖和启动服务进程 (PID: %d)，等待端口 %d...，请稍后", proc.pid, port)
        
        # 启动后台线程持续打印进程输出
        output_thread = threading.Thread(
            target=_log_process_output,
            args=(proc, "Service"),
            daemon=True
        )
        output_thread.start()
        
        # 等待端口开放
        for i in range(PORT_CHECK_RETRIES):
            # 检查进程是否意外退出
            if proc.poll() is not None:
                log.error("Service process exited unexpectedly (code: %d)", proc.returncode)
                # 等待输出线程完成（最多2秒）
                output_thread.join(timeout=2)
                return None
            
            if is_port_open(port):
                log.info("Service started successfully on port %d", port)
                return proc
            
            log.debug("Port %d not open yet, waiting... (attempt %d/%d)", port, i+1, PORT_CHECK_RETRIES)
            time.sleep(PORT_CHECK_WAIT)
        
        # 超时后检查进程状态
        if proc.poll() is None:
            log.warning("Service process is running but port %d is not open after %d seconds", 
                       port, PORT_CHECK_RETRIES * PORT_CHECK_WAIT)
            log.warning("Service may not have started correctly, but continuing anyway...")
            return proc
        else:
            log.error("Service process exited during startup")
            # 等待输出线程完成（最多2秒）
            output_thread.join(timeout=2)
            return None
            
    except Exception as e:
        log.exception("Failed to start service: %s", e)
        return None


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
        return True
    except Exception:
        return False


def start_service_in_docker(
    repo_root: str,
    startup_cmd: str,
    port: int = DEFAULT_PORT,
    image: str = DEFAULT_DOCKER_IMAGE
) -> Optional[Dict[str, str]]:
    """在 Docker 容器中启动服务，返回 {"container_id": id} 用于后续清理"""
    if not _has_docker():
        log.warning("docker not available, fallback to host start")
        return None

    # -d 后台运行，用 sh -lc 保持与用户命令一致性
    run_cmd = (
        f"docker run --rm -d -v {repo_root}:/app -w /app -p {port}:{port} "
        f"{image} sh -lc \"{startup_cmd}\""
    )
    log.info("Starting service in docker: %s", run_cmd)

    try:
        result = subprocess.run(run_cmd, shell=True, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log.error("docker run failed: %s", (result.stdout + "\n" + result.stderr).strip()[:1000])
            return None
        container_id = (result.stdout or "").strip()
        if not container_id:
            log.error("docker run did not return container id")
            return None

        # 等待端口开放
        for i in range(PORT_CHECK_RETRIES):
            if is_port_open(port):
                log.info("Service started in docker (container: %s) on port %d", container_id[:12], port)
                return {"container_id": container_id}
            time.sleep(PORT_CHECK_WAIT)

        # 端口未开放，输出日志辅助定位
        logs = subprocess.run(
            f"docker logs --tail 200 {container_id}", shell=True, capture_output=True, text=True, timeout=20
        )
        log.error("docker service port not open, recent logs:\n%s", (logs.stdout + "\n" + logs.stderr).strip()[:2000])
        # 停止容器
        subprocess.run(f"docker rm -f {container_id}", shell=True, capture_output=True, text=True, timeout=20)
        return None
    except Exception as e:
        log.exception("failed to start service in docker: %s", e)
        return None


def _detect_package_manager(repo_root: str) -> str:
    """基于锁文件检测包管理器: pnpm > yarn > npm"""
    if os.path.exists(os.path.join(repo_root, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(repo_root, "yarn.lock")):
        return "yarn"
    return "npm"


def _run_step(cmd: str, cwd: str, timeout: int = 600) -> tuple[bool, str]:
    """在子进程中执行一步命令，返回 (success, output)"""
    try:
        result = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        success = result.returncode == 0
        if success:
            log.info("step ok: %s", cmd)
        else:
            log.warning("step failed (%d): %s", result.returncode, cmd)
            if output:
                log.debug("step output:\n%s", output[:1000])
        return success, output.strip()
    except subprocess.TimeoutExpired:
        log.warning("step timeout: %s", cmd)
        return False, f"Timeout running: {cmd}"
    except Exception as e:
        log.exception("step exception: %s", e)
        return False, str(e)


def _read_package_json(repo_root: str) -> Dict[str, Any]:
    pkg_path = os.path.join(repo_root, "package.json")
    if not os.path.exists(pkg_path):
        return {}
    try:
        text = read_text(pkg_path, max_bytes=100000)
        return json.loads(text or "{}") if text else {}
    except Exception:
        return {}


def _prepare_node_project(repo_root: str, startup_cmd: str) -> None:
    """在启动前为 Node 项目做准备：install 与 build（使用中国镜像源）"""
    pkg = _read_package_json(repo_root)
    if not pkg:
        return
    pm = _detect_package_manager(repo_root)
    scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
    has_build = "build" in scripts
    node_modules_dir = os.path.join(repo_root, "node_modules")
    dist_dir = os.path.join(repo_root, "dist")
    
    # 中国镜像源
    china_registry = "https://registry.npmmirror.com"

    # 1) 安装依赖（如果 node_modules 不存在或为空）使用中国镜像源
    need_install = (not os.path.exists(node_modules_dir)) or (not os.listdir(node_modules_dir))
    if need_install:
        log.info("node_modules missing, installing dependencies via %s with China mirror...", pm)
        if pm == "pnpm":
            _run_step(f"pnpm install --registry={china_registry} --frozen-lockfile || pnpm install --registry={china_registry}", repo_root, timeout=1200)
        elif pm == "yarn":
            _run_step(f"yarn install --registry={china_registry} --immutable || yarn install --registry={china_registry}", repo_root, timeout=1200)
        else:
            _run_step(f"npm ci --registry={china_registry} --ignore-scripts || npm install --registry={china_registry} --no-audit --no-fund", repo_root, timeout=1200)

    # 2) 构建产物（如果命令是 start:prod 或 dist 不存在 且 存在 build 脚本）
    requires_prod_build = bool(re.search(r"start:prod", startup_cmd))
    need_build = (requires_prod_build or (not os.path.exists(dist_dir))) and has_build
    if need_build:
        log.info("building project via %s run build...", pm)
        if pm == "pnpm":
            _run_step("pnpm build", repo_root, timeout=1200)
        elif pm == "yarn":
            _run_step("yarn build", repo_root, timeout=1200)
        else:
            _run_step("npm run build", repo_root, timeout=1200)


def _wait_for_service_ready(port: int, max_wait_seconds: int = 300, check_interval: int = 5) -> bool:
    """等待服务完全就绪，通过定期检测端口状态
    
    Args:
        port: 服务端口
        max_wait_seconds: 最大等待时间（秒），默认300秒（5分钟）
        check_interval: 检测间隔（秒），默认5秒
        
    Returns:
        True 如果服务就绪，False 如果超时
    """
    max_checks = max_wait_seconds // check_interval
    log.info(f"Waiting for service to fully initialize on port {port}...")
    log.info(f"Will check every {check_interval} seconds for up to {max_wait_seconds} seconds")
    
    for i in range(max_checks):
        if is_port_open(port):
            log.info(f"Service started successfully on port {port}")
            return True
        
        elapsed = (i + 1) * check_interval
        log.debug(f"Service not ready yet, waiting... ({elapsed}/{max_wait_seconds} seconds)")
        time.sleep(check_interval)
    
    log.warning(f"Service did not become ready within {max_wait_seconds} seconds")
    return False


def _build_fallback_commands(repo_root: str, startup_cmd: str) -> list[str]:
    """基于 package.json 与包管理器构造备用启动命令"""
    pm = _detect_package_manager(repo_root)
    pkg = _read_package_json(repo_root)
    scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}

    def pm_run(script: str) -> Optional[str]:
        if script not in scripts:
            return None
        if pm == "pnpm":
            return f"pnpm {script}" if script in ["start", "start:dev", "start:prod"] else f"pnpm run {script}"
        if pm == "yarn":
            return f"yarn {script}" if script in ["start", "start:dev", "start:prod"] else f"yarn {script}"
        return f"npm run {script}"

    fallbacks: list[str] = []
    # 若原命令是 start:prod，优先尝试 build 后再启动
    if re.search(r"start:prod", startup_cmd):
        build_cmd = pm_run("build")
        prod_cmd = pm_run("start:prod") or pm_run("start")
        if build_cmd and prod_cmd:
            fallbacks.append(f"{build_cmd} && {prod_cmd}")
    
    # 常规备用：start、start:dev
    for s in ["start", "start:dev", "start:prod"]:
        cmd = pm_run(s)
        if cmd and cmd != startup_cmd:
            fallbacks.append(cmd)

    # 如果还有 scripts.server 或 app 等也尝试
    for s in ["serve", "server", "dev"]:
        cmd = pm_run(s)
        if cmd:
            fallbacks.append(cmd)

    # 去重保持顺序
    seen = set()
    unique = []
    for c in fallbacks:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def stop_service(proc_or_container: Optional[Any]):
    """终止服务（支持本地进程或 Docker 容器）
    
    Args:
        proc_or_container: subprocess.Popen 或 {"container_id": str}
    """
    if not proc_or_container:
        log.debug("No service process/container to stop")
        return

    # Docker 容器
    if isinstance(proc_or_container, dict) and proc_or_container.get("container_id"):
        container_id = proc_or_container.get("container_id")
        log.info("Stopping docker container: %s", container_id[:12])
        try:
            subprocess.run(f"docker rm -f {container_id}", shell=True, capture_output=True, text=True, timeout=20)
            log.info("Docker container stopped")
        except Exception as e:
            log.exception("Error stopping docker container: %s", e)
        return

    # 本地进程
    proc = proc_or_container
    try:
        if proc.poll() is not None:
            log.info("Service process already terminated (code: %d)", proc.returncode)
            return
    except Exception:
        # 如果对象不具备 poll 接口，忽略
        return
    
    log.info("Stopping service process (PID: %d)...", getattr(proc, "pid", -1))
    
    try:
        # 尝试优雅终止
        proc.terminate()
        try:
            proc.wait(timeout=5)
            log.info("Service stopped gracefully")
        except subprocess.TimeoutExpired:
            # 强制杀死
            log.warning("Service did not stop gracefully, forcing kill...")
            proc.kill()
            proc.wait(timeout=2)
            log.info("Service killed")
    except Exception as e:
        log.exception("Error stopping service: %s", e)


def start_service_if_needed(
    repo_root: str, 
    driver: ModelDriver,
    port: int = DEFAULT_PORT,
    force_start: bool = False,
    use_docker: bool = False,
    docker_image: Optional[str] = None
) -> tuple[Optional[Any], bool]:
    """智能启动服务（如果需要）
    
    Args:
        repo_root: 代码库根目录
        driver: LLM 驱动器
        port: 服务端口
        force_start: 是否强制启动（即使端口已开放）
        
    Returns:
        (进程对象, 是否由本函数启动的布尔值)
    """
  
    # 尝试检测启动命令
    startup_cmd = detect_service_startup(repo_root, driver)
    
    if not startup_cmd:
        log.warning("No startup command detected, assuming service is externally managed")
        return None, False
    
    # 优先尝试使用 Docker 启动
    if use_docker and _has_docker():
        image = docker_image or DEFAULT_DOCKER_IMAGE
        log.info(f"Attempting to start service with Docker (image: {image}, port: {port})")
        container_info = start_service_in_docker(repo_root, startup_cmd, port=port, image=image)
        if container_info:
            time.sleep(3)
            log.info("Service initialization complete (docker)")
            return container_info, True
        else:
            log.warning("Docker startup failed, falling back to local startup")

    # 回退到本地直接启动（首次不安装依赖，但 Node.js 需要特殊处理 sqlite3）
    first_attempt_cmd = startup_cmd
    
    # Node.js 项目需要先处理 sqlite3
    if os.path.exists(os.path.join(repo_root, "package.json")):
        registry = "https://registry.npmmirror.com"
        first_attempt_cmd = f"npm cache clean --force && npm install sqlite3 --build-from-source --registry={registry} && npm run build && {startup_cmd}"
        log.info(f"Node.js project detected, adding sqlite3 build-from-source install and build")
    
    log.info(f"Starting service locally with command (without installing dependencies): {first_attempt_cmd}")
    proc = start_service(first_attempt_cmd, repo_root, port)
    
    if proc:
        # 等待服务就绪，每5秒检测一次，最多等待5分钟
        _wait_for_service_ready(port, max_wait_seconds=300, check_interval=5)
        log.info(f"Service initialization complete (local process, port: {port})")
        return proc, True
    else:
        log.warning("First attempt failed (without dependency installation)")
        log.info("Retrying with dependency installation...")
        
        # 第二次尝试：先安装依赖，再启动
        install_and_start_cmd = _build_install_command(repo_root, startup_cmd)
        if install_and_start_cmd:
            log.info(f"Attempting with dependencies: {install_and_start_cmd}")
            proc = start_service(install_and_start_cmd, repo_root, port)
            
            if proc:
                # 等待服务就绪，每5秒检测一次，最多等待5分钟
                _wait_for_service_ready(port, max_wait_seconds=300, check_interval=5)
                log.info(f"Service started successfully after installing dependencies (port: {port})")
                return proc, True
            else:
                log.error("Failed even after installing dependencies, trying fallbacks...")
        else:
            log.error("Could not generate install command, trying fallbacks...")

        # 第三次尝试：使用备用命令
        fallbacks = _build_fallback_commands(repo_root, startup_cmd)
        is_node_project = os.path.exists(os.path.join(repo_root, "package.json"))
        
        for alt_cmd in fallbacks[:4]:  # 最多尝试 4 个
            log.info("Trying fallback command: %s", alt_cmd)
            
            # 先尝试不安装依赖（但 Node.js 需要特殊处理 sqlite3）
            fallback_first_cmd = alt_cmd
            if is_node_project:
                registry = "https://registry.npmmirror.com"
                fallback_first_cmd = f"npm cache clean --force && npm install sqlite3 --build-from-source --registry={registry} && npm run build && {alt_cmd}"
            
            proc = start_service(fallback_first_cmd, repo_root, port)
            if proc:
                # 等待服务就绪，每5秒检测一次，最多等待5分钟
                _wait_for_service_ready(port, max_wait_seconds=300, check_interval=5)
                log.info(f"Service started with fallback command: {alt_cmd} (port: {port})")
                return proc, True
            
            # 如果失败，尝试先安装依赖再启动
            log.info(f"Fallback failed, retrying with dependency installation...")
            install_and_alt_cmd = _build_install_command(repo_root, alt_cmd)
            if install_and_alt_cmd:
                proc = start_service(install_and_alt_cmd, repo_root, port)
                if proc:
                    # 等待服务就绪，每5秒检测一次，最多等待5分钟
                    _wait_for_service_ready(port, max_wait_seconds=300, check_interval=5)
                    log.info(f"Service started with fallback + install: {install_and_alt_cmd} (port: {port})")
                    return proc, True

        log.error("All fallback attempts failed")
        return None, False

