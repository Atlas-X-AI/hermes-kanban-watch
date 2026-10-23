import hashlib
from pathlib import Path
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "tests").glob("test_*.py")):
        print(hashlib.sha256(path.read_bytes()).hexdigest(), path.name, flush=True)
    source = (root / "plugin/workflows/kanban-watch.js").read_text()
    prelude, marker, body = source.partition("const A = args || {}")
    assert marker and "export const meta" in prelude
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "kanban-watch.mjs"
        path.write_text(prelude + "export async function run(args, agent, log) {\n" + marker + body + "\n}\n")
        result = subprocess.run(["node", "--check", str(path)], check=True)
        print(f"node --check: exit {result.returncode}; full workflow body and metadata retained", flush=True)


if __name__ == "__main__":
    main()
