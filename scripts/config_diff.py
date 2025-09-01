import sys, yaml
from collections.abc import Mapping

def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, Mapping):
            out.update(flat(v, key))
        else:
            out[key] = v
    return out

a_path, b_path = sys.argv[1], sys.argv[2]
a = yaml.safe_load(open(a_path))
b = yaml.safe_load(open(b_path))
af, bf = flat(a), flat(b)

keys = sorted(set(af) | set(bf))
missing_in_b = [k for k in keys if k in af and k not in bf]
missing_in_a = [k for k in keys if k in bf and k not in af]
diff_vals = [k for k in keys if k in af and k in bf and af[k] != bf[k]]

print("Missing in B:", missing_in_b)
print("Missing in A:", missing_in_a)
print("Different values:")
for k in diff_vals:
    print(f"  {k}: A={af[k]!r} | B={bf[k]!r}")
