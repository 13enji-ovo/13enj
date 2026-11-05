#!/usr/bin/env python3
"""Sync shop/index.html PRODUCTS with live Weidian data (stdlib only).

Synced per product (rawUrl must contain itemID=...):
  cny      Weidian price in yuan (discount low price if present, else original)
  price    USD = round(cny * RATE)
  size     joined values of the 尺码 attribute ("S / M / L / XL")
  soldout  true if delisted / no result / stock 0; false when back
  wdTitle  last seen Weidian itemTitle (double spaces normalized)
  title.zh overwritten only when wdTitle changed; such ids are appended to
           tools/needs_translation.txt so en/ru/es can be refreshed.
Images are NOT synced. Network errors for an item skip that item.

Usage:  python3 tools/sync_weidian.py [--file shop/index.html] [--dry-run] [--push]
"""
import argparse, json, os, re, subprocess, sys, time, urllib.parse, urllib.request

RATE = 0.16295  # CNY -> USD, Lolobuy's rate (¥400 -> $65.18)
SHOP_ID = "1830824943"  # owner's Weidian shop (https://weidian.com/?userid=1830824943)

API = "https://thor.weidian.com/detail/getItemSkuInfo/1.0"
LIST_API = "https://thor.weidian.com/decorate/shopDetail.tab.getItemList/1.0"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(item_id, tries=3, url=API, param=None):
    q = urllib.parse.urlencode({"param": json.dumps(param or {"itemId": str(item_id)})})
    req = urllib.request.Request(url + "?" + q, headers={"Referer": "https://weidian.com/", "User-Agent": UA})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network / parse error
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("fetch failed: %s" % last)


def list_shop_items():
    """All on-sale items of SHOP_ID via the public shop H5 list endpoint."""
    items, offset = [], 0
    while offset < 2000:
        d = fetch(None, url=LIST_API, param={"shopId": SHOP_ID, "tabId": 0, "sortOrder": "desc",
                                             "offset": offset, "limit": 20, "from": "h5", "showItemTag": True})
        if (d.get("status") or {}).get("code") not in (0, "0"):
            raise RuntimeError("list status %s" % d.get("status"))
        page = ((d.get("result") or {}).get("itemList")) or []
        items += page
        if len(page) < 20:
            break
        offset += 20
    return items


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def find_products(text):
    m = re.search(r"var PRODUCTS = (\[.*?\n\]);", text, re.S)
    if not m:
        sys.exit("PRODUCTS array not found")
    return m, json.loads(m.group(1))


def object_spans(arr_text):
    """Yield (start, end) of each top-level {...} inside the array text."""
    depth, start, in_str, esc = 0, None, False, False
    for i, c in enumerate(arr_text):
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == "{":
            if depth == 0: start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: yield start, i + 1


def set_field(obj_text, key, value):
    """Replace a top-level `"key": <json scalar>` in place, or insert it before the closing brace."""
    js = json.dumps(value, ensure_ascii=False)
    pat = re.compile(r'(\n    "%s": )("(?:[^"\\]|\\.)*"|-?[\d.]+|true|false|null)' % re.escape(key))
    if pat.search(obj_text):
        return pat.sub(lambda m: m.group(1) + js, obj_text, count=1)
    body = obj_text.rstrip()[:-1].rstrip()
    return body + ',\n    "%s": %s\n  }' % (key, js)


def set_title_zh(obj_text, value):
    js = json.dumps(value, ensure_ascii=False)
    pat = re.compile(r'("title": \{[^{}]*?"zh": )("(?:[^"\\]|\\.)*")', re.S)
    if pat.search(obj_text):
        return pat.sub(lambda m: m.group(1) + js, obj_text, count=1)
    return set_field(obj_text, "title", value)  # plain-string title


def compute(p, data):
    """Return dict of desired field values, or None to skip."""
    st = (data or {}).get("status") or {}
    res = (data or {}).get("result")
    want = {}
    if st.get("code") not in (0, "0") or not res:
        want["soldout"] = True
        return want, "delisted/unavailable (status %s)" % st.get("code")
    fen = res.get("itemDiscountLowPrice") or res.get("itemOriginalLowPrice")
    if not fen and not res.get("itemTitle"):  # deleted items return an almost empty result
        want["soldout"] = True
        return want, "delisted/unavailable (empty result)"
    if fen:
        cny = fen / 100
        cny = int(cny) if cny == int(cny) else round(cny, 2)
        want["cny"] = cny
        want["price"] = round(cny * RATE)
    for a in res.get("attrList") or []:
        if norm(a.get("attrTitle")) == "尺码":
            vals = [norm(v.get("attrValue")) for v in a.get("attrValues") or [] if v.get("attrValue")]
            if vals: want["size"] = " / ".join(vals)
    stock = res.get("itemStock")
    want["soldout"] = bool(res.get("offlineItem")) or (stock is not None and int(stock) <= 0)
    if res.get("itemTitle"):
        want["wdTitle"] = norm(res["itemTitle"])
    return want, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(ROOT, "shop", "index.html"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true", help="git commit & push if changed (box fallback)")
    a = ap.parse_args()

    if a.push:
        subprocess.run(["git", "-C", ROOT, "pull", "-q", "--rebase"], check=True)
    text = open(a.file, encoding="utf-8").read()
    m, products = find_products(text)
    arr = m.group(1)
    spans = list(object_spans(arr))
    if len(spans) != len(products):
        sys.exit("could not map PRODUCTS objects")

    new_arr, last, changes, retitled, errors = "", 0, [], [], 0
    for (s, e), p in zip(spans, products):
        obj = arr[s:e]
        mid = re.search(r"itemI[Dd]=(\d+)", p.get("rawUrl") or p.get("url") or "")
        if mid:
            iid = mid.group(1)
            try:
                want, status = compute(p, fetch(iid))
            except Exception as ex:
                print("[%s] SKIP: %s" % (p["id"], ex)); errors += 1
                want, status = {}, "error"
            diffs = []
            for k, v in want.items():
                old = p.get(k, False if k == "soldout" else None)
                if old != v:
                    diffs.append("%s: %r -> %r" % (k, old, v))
                    obj = set_field(obj, k, v)
            if "wdTitle" in want and p.get("wdTitle") != want["wdTitle"]:
                if p.get("wdTitle") is not None:  # real Weidian title change (not first sync)
                    obj = set_title_zh(obj, want["wdTitle"])
                    diffs.append("title.zh -> %r" % want["wdTitle"])
                    retitled.append(p["id"])
            if status != "error":
                print("[%s] %s cny=%s price=$%s size=%r soldout=%s%s" % (
                    p["id"], status, want.get("cny", p.get("cny")), want.get("price", p.get("price")),
                    want.get("size", p.get("size")), want.get("soldout"),
                    ("\n   " + "\n   ".join(diffs)) if diffs else "  (no change)"))
            changes += [p["id"] + " " + d for d in diffs]
        new_arr += arr[last:s] + obj
        last = e
    new_arr += arr[last:]

    # --- new item detection (never auto-published) ---
    known = set()
    for p in products:
        for u in (p.get("rawUrl"), p.get("url"), p.get("id")):
            known.update(re.findall(r"(\d{6,})", u or ""))
    try:
        listed = list_shop_items()
        new = [i for i in listed if str(i.get("itemId")) not in known]
        lines = ["%s\t%s\t¥%s\t%s" % (i.get("itemId"), norm(i.get("itemName")), i.get("price"), i.get("itemImg"))
                 for i in new]
        path = os.path.join(ROOT, "tools", "new_items.txt")
        content = ("# itemId\ttitle\tCNY\tmainPic  (review, then add to PRODUCTS by hand)\n" + "\n".join(lines) + "\n") if lines else ""
        old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if content != old and not a.dry_run:
            if content: open(path, "w", encoding="utf-8").write(content)
            elif os.path.exists(path): os.remove(path)
        print("Shop list: %d item(s) on Weidian, %d new" % (len(listed), len(new)))
        for l in lines: print("  NEW: " + l)
    except Exception as ex:
        print("Shop list SKIP: %s" % ex)

    if retitled:
        path = os.path.join(ROOT, "tools", "needs_translation.txt")
        existing = set(open(path).read().split()) if os.path.exists(path) else set()
        with open(path, "a") as f:
            for i in retitled:
                if i not in existing: f.write(i + "\n")
        print("NEEDS TRANSLATION (title.zh changed):", " ".join(retitled))

    if not changes:
        print("No changes.%s" % (" (%d item errors)" % errors if errors else ""))
        return
    out = text[:m.start(1)] + new_arr + text[m.end(1):]
    json.loads(new_arr)  # sanity
    if a.dry_run:
        print("Dry run: %d change(s), file not written." % len(changes)); return
    open(a.file, "w", encoding="utf-8").write(out)
    print("Wrote %d change(s) to %s" % (len(changes), a.file))
    if a.push:
        g = ["git", "-C", ROOT]
        subprocess.run(g + ["add", "shop/index.html", "tools"], check=True)
        subprocess.run(g + ["-c", "user.name=13enji-ovo", "-c", "user.email=13enji-ovo@users.noreply.github.com",
                            "commit", "-qm", "自动同步微店价格"], check=True)
        subprocess.run(g + ["push", "-q"], check=True)
        print("Pushed.")


if __name__ == "__main__":
    main()
