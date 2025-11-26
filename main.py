import logging
import os
import shutil
import tempfile
import zipfile
from typing import Any, Dict

from agent.analyzer import analyze_repository

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger("main")
logger.info("=" * 80)
logger.info("启动 Code Analyzer Agent")
logger.info("=" * 80)

try:
    # 可选引入 FastAPI，用于 HTTP 模式
    from fastapi import FastAPI, UploadFile, File, Form
    from fastapi.responses import JSONResponse
except ImportError:  # FastAPI 非必需，仅在 HTTP 模式使用
    FastAPI = None  # type: ignore
    UploadFile = File = Form = JSONResponse = None  # type: ignore


def _unzip(zip_path: str, dest_dir: str) -> None:
    """解压ZIP文件到指定目录"""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def analyze_requirements_direct():
    """直接执行需求分析（本地调试模式）"""
    # 写死的参数
    problem_description = """Create a multi-channel forum api. Can use any stack, but must use typescript, be deployable, and of production quality.
Try using graphql or grpc for fun, but REST is ok too.
Try using docker containers for fun if you want.
Show how you would like to write documentation and testing if possible.
Channel Model: { id, name }
Message Model: { id, title, content, channel, createdAt }
The API should have these features.
- create a channel
- write messages in a channel
- list messages in a channel and order by descending (pagination is a extra credit)
Show how a production level project would look. (documentation, testing, error handling, etc ...)
Send the repository link of the project by email when finished.
"""
    
    code_zip_path = "/home/ubuntu/independent_qcoder_agent/nestjs-channel-messenger-demo-main.zip"
    
    logger.info(f"使用需求描述长度: {len(problem_description)} 字符")
    logger.info(f"需求描述预览: {problem_description[:200]}...")
    logger.info(f"代码库ZIP文件路径: {code_zip_path}")
    
    # 检查文件是否存在
    if not os.path.exists(code_zip_path):
        logger.error(f"代码库ZIP文件不存在: {code_zip_path}")
        return
    
    # 创建临时目录
    temp_dir = tempfile.mkdtemp(dir="/tmp", prefix="indep_qcoder_direct_")
    extract_dir = os.path.join(temp_dir, "repo")
    
    logger.info(f"临时目录: {temp_dir}")
    logger.info(f"解压目录: {extract_dir}")

    try:
        logger.info("解压代码库文件...")
        _unzip(code_zip_path, extract_dir)
        logger.info(f"文件已解压到: {extract_dir}")

        # 确定代码库根目录
        repo_root = extract_dir
        items = os.listdir(extract_dir)
        logger.info(f"解压目录内容: {items}")
        if len(items) == 1:
            maybe_root = os.path.join(extract_dir, items[0])
            if os.path.isdir(maybe_root):
                repo_root = maybe_root
                logger.info(f"检测到单层目录，使用子目录作为根: {repo_root}")

        logger.info(f"代码库根目录: {repo_root}")
        logger.info("")
        logger.info("开始分析代码库...")
        logger.info("")

        # 执行分析
        result: Dict[str, Any] = analyze_repository(
            problem_description=problem_description,
            repo_root=repo_root,
        )
        
        logger.info("")
        logger.info("分析完成")
        logger.info(f"结果包含: {len(result.get('categories', []))} 个分类, {len(result.get('feature_analysis', []))} 个功能分析")
        
        # 保存分析结果到文件
        import json
        result_file = os.path.join("/home/ubuntu/independent_qcoder_agent", "analysis_result.json")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"分析结果已保存到: {result_file}")
        
        logger.info("")
        logger.info("=" * 80)
        logger.info("需求分析完成!")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.exception("分析过程中发生错误")
        logger.error(f"错误详情: {str(e)}")
    finally:
        logger.info(f"清理临时目录: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)


def create_app() -> "FastAPI":
    """
    创建 FastAPI 应用（HTTP 模式）
    
    提供 /analyze 接口：
    - POST /analyze
      - form 字段: description (str)
      - form 文件: file (zip 格式代码库)
    """
    if FastAPI is None:
        raise RuntimeError("FastAPI 未安装，无法启动 HTTP 模式。请先安装 fastapi 和 uvicorn。")
    
    app = FastAPI(title="Code Analyzer Agent", version="1.0.0")
    
    @app.post("/analyze")
    async def analyze_endpoint(
        description: str = Form(...),
        file: UploadFile = File(...),
    ):
        temp_dir = tempfile.mkdtemp(dir="/tmp", prefix="indep_qcoder_http_")
        extract_dir = os.path.join(temp_dir, "repo")
        try:
            # 保存上传的 ZIP
            upload_name = file.filename or "repo.zip"
            upload_path = os.path.join(temp_dir, upload_name)
            with open(upload_path, "wb") as f:
                f.write(await file.read())
            
            logger.info(f"收到上传代码库: {upload_name}, 大小约 {os.path.getsize(upload_path)} 字节")
            logger.info(f"HTTP 模式临时目录: {temp_dir}")
            logger.info(f"HTTP 模式解压目录: {extract_dir}")
            
            # 解压并确定仓库根目录
            _unzip(upload_path, extract_dir)
            logger.info(f"文件已解压到: {extract_dir}")
            
            repo_root = extract_dir
            items = os.listdir(extract_dir)
            logger.info(f"解压目录内容: {items}")
            if len(items) == 1:
                maybe_root = os.path.join(extract_dir, items[0])
                if os.path.isdir(maybe_root):
                    repo_root = maybe_root
                    logger.info(f"检测到单层目录，使用子目录作为根: {repo_root}")
            
            # 调用核心分析逻辑（统一走 VLLM）
            result: Dict[str, Any] = analyze_repository(
                problem_description=description,
                repo_root=repo_root,
            )
            return JSONResponse(result)
        except Exception as e:
            logger.exception("HTTP 分析过程中发生错误")
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            logger.info(f"HTTP 模式清理临时目录: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return app


# 如果安装了 FastAPI，则提供全局 app 方便 `uvicorn main:app` 直接启动
if FastAPI is not None:
    app = create_app()
else:
    app = None  # type: ignore


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Code Analyzer Agent 启动入口")
    parser.add_argument(
        "--mode",
        choices=["direct", "api"],
        default="api",
        help="运行模式：direct=本地直接解压分析（当前行为），api=启动 FastAPI HTTP 服务",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="HTTP 模式监听地址（仅 mode=api 有效）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="HTTP 模式端口（仅 mode=api 有效）",
    )
    args = parser.parse_args()
    
    if args.mode == "direct":
        logger.info("以本地直接分析模式运行（便于调试）")
        analyze_requirements_direct()
    else:
        if FastAPI is None:
            raise RuntimeError("FastAPI 未安装，无法启动 HTTP 模式。请先安装 fastapi 和 uvicorn。")
        logger.info(f"以 HTTP 模式运行 FastAPI 服务，监听 {args.host}:{args.port}")
        import uvicorn
        uvicorn.run("main:app", host=args.host, port=args.port, reload=False)
