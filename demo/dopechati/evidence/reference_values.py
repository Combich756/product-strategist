"""Independent calculations on the two original CSVs; no project imports."""
import csv
import json
from fractions import Fraction
from pathlib import Path

base = Path(__file__).resolve().parents[1]
materials = base / 'materials'
with (materials/'experiment.csv').open(encoding='utf-8') as f:
    rows = {(r['format'],r['arm']): r for r in csv.DictReader(f)}
with (materials/'support.csv').open(encoding='utf-8') as f:
    minutes = {r['format']: int(r['extra_support_min_per_100']) for r in csv.DictReader(f)}
out = {}
for fmt, cost in minutes.items():
    offer, standard = rows[fmt,'offer'], rows[fmt,'standard']
    def per100(row):
        return 100*Fraction(int(row['first_pass']),int(row['eligible']))
    lift = per100(offer)-per100(standard)
    economics = 90*lift-10*cost
    out[fmt]={
       'standard_first_pass': f"{standard['first_pass']}/{standard['eligible']}",
       'offer_first_pass': f"{offer['first_pass']}/{offer['eligible']}",
       'offer_percent': float(per100(offer)), 'delta_percentage_points': float(lift),
       'opened_first_pass': f"{offer['first_pass_among_users']}/{offer['used_check']}",
       'opened_percent': round(100*int(offer['first_pass_among_users'])/int(offer['used_check']), 4),
       'minutes_per_100':cost,'variable_saving_rub_per_100':float(economics),
       'eligible_to_roll_out': per100(offer)>=75 and cost<=35,
    }
print(json.dumps(out,ensure_ascii=False,indent=2))
