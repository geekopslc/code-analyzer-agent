# -*- coding: utf-8 -*-
import os

if __name__ == "__main__":
	import uvicorn
	host = os.environ.get("HOST", "0.0.0.0")
	port = int(os.environ.get("PORT", "8000"))
	
	uvicorn.run(
		"app.api.server:app", 
		host=host, 
		port=port,
		timeout_keep_alive=300,
		timeout_graceful_shutdown=30,
	)
