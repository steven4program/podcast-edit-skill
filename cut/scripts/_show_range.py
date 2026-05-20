import json, sys
path, lo, hi = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
d = json.load(open(path, encoding='utf-8'))
cand = d['candidates']
print(f'total in file: {len(cand)}')
print(f'--- {lo}-{hi}s ---')
for c in cand:
    if lo <= c['start'] <= hi:
        print(f'  {c["start"]:>6.2f}-{c["end"]:>6.2f}  cent={c.get("centroid_hz",0):>4.0f}  rolloff={c.get("rolloff_hz",0):>4.0f}  zcr={c.get("zcr",0):.3f}  rms={c.get("rms_peak",0):.3f}  score={c.get("score",0):.2f}')
