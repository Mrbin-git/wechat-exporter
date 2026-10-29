import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))

from wexport.core import media  # noqa: E402

ACC = Path(r"D:\WeChat\xwechat_files\wxid_example_9f3a")
attach = ACC / "msg" / "attach"
dats = sorted(attach.rglob("*.dat"))
print("dat files:", len(dats))
codes = media.find_codes()
print("codes:", codes)
keys = media.resolve_keys(ACC, "wxid_example")
for k in keys:
    print("  key code=", k.code, "aes=", k.aes_key, "xor=", hex(k.xor_key))
out = ROOT / "probe" / "media_out"
ok = bad = 0
for p in dats:
    fmt = media.detect_format(p)
    r = media.decrypt_media(p, out, keys)
    if r:
        ok += 1
        print(f"OK  {p.name[:40]:42s} fmt={fmt} -> {r.name} {r.stat().st_size}B")
    else:
        bad += 1
        print(f"BAD {p.name[:40]:42s} fmt={fmt}")
print(f"summary: {ok}/{len(dats)} ok, {bad} failed")
