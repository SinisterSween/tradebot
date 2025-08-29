# tools/summarize_repo.py
import ast, os, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

for dirpath, _, filenames in os.walk(ROOT):
    for fn in sorted(f for f in filenames if f.endswith(".py")):
        path = os.path.join(dirpath, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                tree = ast.parse(f.read(), filename=path)
            classes, funcs = [], []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = [getattr(b, "id", getattr(getattr(b, "attr", None), "id", "")) for b in node.bases]
                    classes.append((node.name, bases))
                elif isinstance(node, ast.FunctionDef):
                    funcs.append(node.name)
            if classes or funcs:
                rel = os.path.relpath(path, ROOT)
                print(f"\n# {rel}")
                if classes:
                    print("Classes:")
                    for n, b in classes:
                        print(f"  - {n}({', '.join(b)})")
                if funcs:
                    print("Functions:")
                    for n in funcs:
                        print(f"  - {n}")
        except Exception as e:
            print(f"\n# {path}\n! Parse error: {e}")
