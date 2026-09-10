"""Convenience launcher: python run.py [--host 0.0.0.0] [--port 8000]"""

import argparse

import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port,
                reload=args.reload)
