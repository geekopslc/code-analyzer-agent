import os
import shutil
import tempfile
from typing import Any, Dict

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import JSONResponse

from app.utils.zip_handler import save_upload_to_path, unzip_to_dir
from app.agents.graph_definition import run_analysis
from app.utils.logger import get_logger

app = FastAPI(title="Code Analyzer Agent", version="1.0.0")
log = get_logger("api")


@app.post("/analyze")
async def analyze(
	problem_description: str = Form(...),
	code_zip: UploadFile = File(...),
	qcoder: str = Form(None),
) -> JSONResponse:
	log.info("/analyze request received: filename=%s size=? desc_len=%d qcoder=%s", getattr(code_zip, "filename", "<none>"), len(problem_description or ""), qcoder or "None")
	if not code_zip or not code_zip.filename:
		log.warning("missing code_zip")
		raise HTTPException(status_code=400, detail="code_zip is required")

	if not code_zip.filename.lower().endswith(".zip"):
		log.warning("invalid extension: %s", code_zip.filename)
		raise HTTPException(status_code=400, detail="code_zip must be a .zip file")

	temp_dir = tempfile.mkdtemp(prefix="code_analyze_")
	zip_path = os.path.join(temp_dir, code_zip.filename)
	extract_dir = os.path.join(temp_dir, "repo")
	log.info("workdir prepared: %s", temp_dir)

	try:
		await save_upload_to_path(code_zip, zip_path)
		log.info("zip saved: %s", zip_path)
		unzip_to_dir(zip_path, extract_dir)
		log.info("zip extracted to: %s", extract_dir)

		repo_root = extract_dir
		items = os.listdir(extract_dir)
		if len(items) == 1:
			potential_root = os.path.join(extract_dir, items[0])
			if os.path.isdir(potential_root):
				repo_root = potential_root
				log.info("detected single subdirectory, using as repo root: %s", items[0])
		
		log.info("using repo_root: %s", repo_root)

		result: Dict[str, Any] = run_analysis(
			problem_description=problem_description,
			repo_root=repo_root,
			test_qcoder=qcoder.lower() not in ["false", "0", "no"] if qcoder else True,
		)
		log.info("analysis finished: features=%d", len(result.get("feature_analysis", [])))
		return JSONResponse(content=result)
	except Exception as e:
		log.exception("analysis failed: %s", e)
		raise
	finally:
		# Ensure cleanup regardless of success/failure
		shutil.rmtree(temp_dir, ignore_errors=True)
		log.info("cleanup done: %s", temp_dir)
