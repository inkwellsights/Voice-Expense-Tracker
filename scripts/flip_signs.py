"""One-shot migration: flip positive amounts to negative.

ExpenseOwl uses sign to distinguish income (positive) from expense (negative).
Bot originally sent positive — every entry got recorded as income. This script
finds positive-amount entries and rewrites them with the negated value via
PUT /expense/edit?id=<id>.

Safe to re-run: skips entries that are already negative.
"""
import json
import urllib.request

BASE = "http://localhost:5006"


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main():
    expenses = http("GET", "/expenses")
    print(f"Found {len(expenses)} expenses")

    fixed = 0
    skipped = 0
    for e in expenses:
        amt = float(e.get("amount") or 0)
        if amt <= 0:
            skipped += 1
            continue
        eid = e["id"]
        payload = {
            "name": e.get("name", ""),
            "category": e.get("category", "Other"),
            "amount": -abs(amt),
            "date": e.get("date"),
            "tags": e.get("tags") or [],
        }
        try:
            http("PUT", f"/expense/edit?id={eid}", payload)
            fixed += 1
            print(f"  flipped {eid[:8]}  {e.get('name','?'):<20}  {amt:>8} -> {-amt:>8}")
        except Exception as ex:
            print(f"  FAIL {eid}: {ex}")

    print(f"\nFlipped: {fixed}   Already-negative (skipped): {skipped}")


if __name__ == "__main__":
    main()
