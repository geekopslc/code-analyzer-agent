"""JavaScript test executor - runs generated test code in Node.js subprocess"""
import subprocess
import tempfile
import os
from typing import Dict, Any

from app.utils.logger import get_logger

log = get_logger("js_runner")


def execute_js_test(
    test_code: str,
    cwd: str = ".",
    timeout: int = 30,
    run_command: str = None,
    persist_path: str | None = None,
) -> Dict[str, Any]:
    """执行一段 JS 测试代码并捕获输出
    
    Args:
        test_code: JavaScript test code to execute
        cwd: Working directory for execution
        timeout: Execution timeout in seconds
        run_command: 自定义运行命令（如 "node test.js" 或 "npm test"）
                    如果为 None，默认使用 "node {temp_file}"
        
    Returns:
        Dict with keys:
            - tests_passed: bool, whether tests passed
            - log: str, combined stdout/stderr output
            - returncode: int, process return code
    """
    log.info("executing JS test code (len=%d) in cwd=%s", len(test_code), cwd)
    if run_command:
        log.info("using custom run command: %s", run_command)
    
    # Decide file location: persist to provided path or a temporary file
    tmpdir_ctx = None
    test_path: str
    try:
        if persist_path:
            os.makedirs(os.path.dirname(persist_path), exist_ok=True)
            test_path = persist_path
        else:
            tmpdir_ctx = tempfile.TemporaryDirectory()
            test_path = os.path.join(tmpdir_ctx.name, "autotest.js")

        # Write test code
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(test_code)

        log.info("test file written to: %s", test_path)

        # Build execution command
        if run_command:
            cmd = run_command.replace("test.js", test_path)
            if "npm" in cmd or "yarn" in cmd or "npx" in cmd:
                use_shell = True
                exec_cmd = cmd
            else:
                use_shell = False
                exec_cmd = cmd.split()
        else:
            use_shell = False
            exec_cmd = ["node", test_path]

        log.info("executing command: %s (shell=%s)", exec_cmd, use_shell)

        result = subprocess.run(
            exec_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=use_shell,
        )

        success = result.returncode == 0
        output = (result.stdout + "\n" + result.stderr).strip()

        log.info("test execution finished: success=%s returncode=%d", success, result.returncode)
        if output:
            log.debug("test output:\n%s", output[:1000])

        return {
            "tests_passed": success,
            "log": output,
            "returncode": result.returncode,
            "test_path": test_path,
        }

    except subprocess.TimeoutExpired:
        log.warning("test execution timeout after %ds", timeout)
        return {
            "tests_passed": False,
            "log": f"Timeout after {timeout}s",
            "returncode": -1,
            "test_path": test_path if 'test_path' in locals() else "",
        }
    except FileNotFoundError:
        log.error("Node.js not found - please install Node.js")
        return {
            "tests_passed": False,
            "log": "Error: node command not found. Please install Node.js.",
            "returncode": -1,
            "test_path": test_path if 'test_path' in locals() else "",
        }
    except Exception as e:
        log.exception("test execution failed: %s", e)
        return {
            "tests_passed": False,
            "log": f"Execution failed: {str(e)}",
            "returncode": -1,
            "test_path": test_path if 'test_path' in locals() else "",
        }
    finally:
        if tmpdir_ctx is not None:
            tmpdir_ctx.cleanup()

