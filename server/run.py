"""Start the Fukidashi server: python run.py"""

import uvicorn

from fukidashi.config import load_settings

if __name__ == "__main__":
    s = load_settings()
    uvicorn.run("fukidashi.app:app", host=s.host, port=s.port, log_level="info")
