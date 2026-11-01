"""QQ-only edition: python qq_main.py"""
import os
from pathlib import Path

import uvicorn
from src.qq_app import create_app

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    uvicorn.run(create_app(), host="127.0.0.1", port=8090, log_level="info")
