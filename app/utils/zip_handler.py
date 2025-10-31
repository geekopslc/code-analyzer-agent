import os
import zipfile
from fastapi import UploadFile


async def save_upload_to_path(upload: UploadFile, dest_path: str) -> None:
	os.makedirs(os.path.dirname(dest_path), exist_ok=True)
	with open(dest_path, "wb") as f:
		while True:
			chunk = await upload.read(1024 * 1024)
			if not chunk:
				break
			f.write(chunk)


def unzip_to_dir(zip_path: str, dest_dir: str) -> None:
	os.makedirs(dest_dir, exist_ok=True)
	with zipfile.ZipFile(zip_path, 'r') as zf:
		zf.extractall(dest_dir)
