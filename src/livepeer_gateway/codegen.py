from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import httpx


PROTO_URL = (
    "https://raw.githubusercontent.com/livepeer/go-livepeer/"
    "refs/heads/master/net/lp_rpc.proto"
)

def main() -> None:
    root = Path(__file__).resolve().parents[2]
    src_dir = root / "src/livepeer_gateway"

    proto_path = root / "lp_rpc.proto"

    print(f"[codegen] Downloading {PROTO_URL}")
    resp = httpx.get(PROTO_URL)
    resp.raise_for_status()
    proto_path.write_bytes(resp.content)

    print("[codegen] Running grpc_tools.protoc")
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{root}",
            f"--python_out={src_dir}",
            f"--grpc_python_out={src_dir}",
            str(proto_path),
        ]
    )

    patch_grpc_imports(src_dir / "lp_rpc_pb2_grpc.py")

    print("[codegen] Done")
    print("[codegen] Generated:")
    print(f"  {src_dir / 'lp_rpc_pb2.py'}")
    print(f"  {src_dir / 'lp_rpc_pb2_grpc.py'}")

def patch_grpc_imports(grpc_file: Path) -> None:
    txt = grpc_file.read_text(encoding="utf-8")
    patched, n = re.subn(
        r"^import\s+lp_rpc_pb2\s+as\s+lp__rpc__pb2\s*$",
        "from . import lp_rpc_pb2 as lp__rpc__pb2",
        txt,
        flags=re.MULTILINE,
    )
    if n == 0:
        raise RuntimeError(f"Expected import not found in {grpc_file}")
    grpc_file.write_text(patched, encoding="utf-8")
