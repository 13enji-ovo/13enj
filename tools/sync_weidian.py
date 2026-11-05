#!/usr/bin/env python3
"""Sync shop/index.html PRODUCTS with live Weidian data (stdlib + Pillow for images).

Synced per product (rawUrl must contain itemID=...):
  cny      Weidian price in yuan (discount low price if present, else original)
  price    USD = round(cny * RATE)
  size     joined values of the 尺码 attribute ("S / M / L / XL")
  soldout  true if delisted / no result / stock 0; false when back
  wdTitle  last seen Weidian itemTitle (double spaces normalized)
  title.zh overwritten only when wdTitle changed; such ids are appended to
           tools/needs_translation.txt so en/ru/es can be refreshed.
  wdImgs   ordered Weidian main-image source URLs actually used (after excludes)
  img/imgs local shop/img/wd<itemID>-N.jpg copies of those sources
Optional per-product wdImgsExclude: list of source URLs or "sha256:<hex>" content
hashes that must never be (re)added. Image fetch failures leave existing images
untouched.

Usage:  python3 tools/sync_weidian.py [--file shop/index.html] [--dry-run] [--push]
"""
import argparse, hashlib, html, json, os, re, subprocess, sys, time, urllib.parse, urllib.request

RATE = 0.16295  # CNY -> USD, Lolobuy's rate (¥400 -> $65.18)
SHOP_ID = "1830824943"  # owner's Weidian shop (https://weidian.com/?userid=1830824943)

API = "https://thor.weidian.com/detail/getItemSkuInfo/1.0"
LIST_API = "https://thor.weidian.com/decorate/shopDetail.tab.getItemList/1.0"
ITEM_PAGE = "https://weidian.com/item.html?itemID={}"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(ROOT, "shop", "img")
IMG_MAX = 1200
IMG_QUALITY = 85


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


def fetch_bytes(url, tries=3):
    req = urllib.request.Request(url, headers={"Referer": "https://weidian.com/", "User-Agent": UA})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("download failed: %s" % last)


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


def fetch_item_imgs(item_id):
    """Ordered Weidian main-image URLs from the public item HTML (`imgs` JSON)."""
    raw = fetch_bytes(ITEM_PAGE.format(item_id))
    text = html.unescape(raw.decode("utf-8", "replace"))
    m = re.search(r'"imgs"\s*:\s*(\[[^\]]+\])', text)
    if not m:
        raise RuntimeError("imgs array not found in item page")
    imgs = json.loads(m.group(1))
    out, seen = [], set()
    for u in imgs:
        if not isinstance(u, str):
            continue
        u = u.strip().split("?")[0]
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


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


def _json_literal(value):
    if isinstance(value, (list, dict)):
        js = json.dumps(value, ensure_ascii=False, indent=2)
        return js.replace("\n", "\n    ")
    return json.dumps(value, ensure_ascii=False)


def _skip_json_value(s, i):
    """Return index just past the JSON value starting at s[i]."""
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    if i >= len(s):
        raise ValueError("EOF")
    if s[i] == '"':
        i += 1
        esc = False
        while i < len(s):
            c = s[i]
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"':
                return i + 1
            i += 1
        raise ValueError("unterminated string")
    if s[i] in "{[":
        open_c, close_c = ("{", "}") if s[i] == "{" else ("[", "]")
        depth, in_str, esc = 0, False, False
        while i < len(s):
            c = s[i]
            if in_str:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': in_str = False
            else:
                if c == '"': in_str = True
                elif c == open_c: depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        return i + 1
            i += 1
        raise ValueError("unbalanced")
    # number / true / false / null
    m = re.match(r'-?[\d.]+([eE][+-]?\d+)?|true|false|null', s[i:])
    if not m:
        raise ValueError("bad value at %d" % i)
    return i + m.end()


def set_field(obj_text, key, value):
    """Replace a top-level `"key": <json>` in place, or insert it before the closing brace."""
    js = _json_literal(value)
    key_pat = re.compile(r'(\n    "%s": )' % re.escape(key))
    m = key_pat.search(obj_text)
    if m:
        start = m.end()
        end = _skip_json_value(obj_text, start)
        return obj_text[:m.start(1)] + m.group(1) + js + obj_text[end:]
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


def _exclude_match(url, blob, excludes):
    if not excludes:
        return False
    digest = "sha256:" + hashlib.sha256(blob).hexdigest() if blob is not None else None
    for ex in excludes:
        if not ex:
            continue
        if ex.startswith("sha256:"):
            if digest and ex.lower() == digest.lower():
                return True
        elif ex == url or ex.rstrip("/") == url.rstrip("/"):
            return True
    return False


def resize_jpeg(blob, dest_path):
    """Write blob as JPEG max IMG_MAX px, quality IMG_QUALITY. Needs Pillow."""
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(blob)).convert("RGB")
    w, h = im.size
    scale = min(1.0, float(IMG_MAX) / max(w, h))
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    im.save(dest_path, "JPEG", quality=IMG_QUALITY, optimize=True)


def sync_images(p, iid, dry_run=False):
    """Return (want_fields, diffs, error_or_None). want_fields may be empty on no-op/error."""
    excludes = list(p.get("wdImgsExclude") or [])
    try:
        remote = fetch_item_imgs(iid)
    except Exception as ex:
        return {}, [], "images SKIP (list): %s" % ex

    # Pre-filter by URL excludes; content-hash excludes applied after download.
    url_excludes = [e for e in excludes if e and not str(e).startswith("sha256:")]
    candidates = [u for u in remote if u not in url_excludes and u.rstrip("/") not in url_excludes]

    old_wd = list(p.get("wdImgs") or [])
    # Fast path: URL list already matches stored wdImgs (and img files exist)
    local_ok = True
    old_imgs = list(p.get("imgs") or ([p["img"]] if p.get("img") else []))
    if old_wd == candidates and old_imgs:
        for rel in old_imgs:
            if not os.path.isfile(os.path.join(ROOT, "shop", rel)):
                local_ok = False
                break
        if local_ok and (p.get("img") == old_imgs[0]):
            return {}, [], None

    # Need to (re)download to apply hash excludes and refresh files.
    kept_urls, blobs = [], []
    try:
        for u in candidates:
            blob = fetch_bytes(u)
            if _exclude_match(u, blob, excludes):
                continue
            kept_urls.append(u)
            blobs.append(blob)
    except Exception as ex:
        return {}, [], "images SKIP (download): %s" % ex

    if old_wd == kept_urls and local_ok and old_imgs and p.get("img") == old_imgs[0]:
        return {}, [], None

    if not kept_urls:
        return {}, [], "images SKIP (all excluded/empty)"

    new_rels = ["img/wd%s-%d.jpg" % (iid, i) for i in range(1, len(kept_urls) + 1)]
    want = {"wdImgs": kept_urls, "img": new_rels[0], "imgs": new_rels}
    diffs = []
    if old_wd != kept_urls:
        diffs.append("wdImgs: %d -> %d url(s)" % (len(old_wd), len(kept_urls)))
    if p.get("img") != want["img"] or list(p.get("imgs") or []) != new_rels:
        diffs.append("img/imgs -> %s (%d)" % (want["img"], len(new_rels)))

    if dry_run:
        return want, diffs, None

    os.makedirs(IMG_DIR, exist_ok=True)
    try:
        written = []
        for rel, blob in zip(new_rels, blobs):
            dest = os.path.join(ROOT, "shop", rel)
            resize_jpeg(blob, dest)
            written.append(rel)
    except Exception as ex:
        # Clean partial writes of this batch only
        for rel in written:
            try: os.remove(os.path.join(ROOT, "shop", rel))
            except OSError: pass
        return {}, [], "images SKIP (write): %s" % ex

    # Remove prior local imgs for this product that we no longer reference
    obsolete = set(old_imgs) - set(new_rels)
    for rel in obsolete:
        if not rel.startswith("img/"):
            continue
        # Only delete our stable wd<itemID>-* names or previously known locals for this item
        base = os.path.basename(rel)
        if base.startswith("wd%s-" % iid) or rel in old_imgs:
            fp = os.path.join(ROOT, "shop", rel)
            if os.path.isfile(fp):
                try: os.remove(fp)
                except OSError: pass
    return want, diffs, None


LINKTREE = "https://linktr.ee/13enji.shop"


def fetch_linktree():
    """Public Linktree links (excluding the shop itself) as [{label, url}]."""
    html_ = subprocess.run(["curl", "-sL", "--max-time", "40", "-A", "Mozilla/5.0", LINKTREE],
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_, re.S)
    acc = json.loads(m.group(1))["props"]["pageProps"]["account"]
    out = []
    for l in acc.get("links") or []:
        url, title = (l.get("url") or "").strip(), norm(l.get("title"))
        if not url or not title or "13enji-ovo.github.io" in url:
            continue
        out.append({"label": title, "url": url})
    for l in acc.get("socialLinks") or []:
        url = (l.get("url") or "").strip()
        if url and all(url != o["url"] for o in out):
            out.append({"label": (l.get("type") or "Link").title(), "url": url})
    return out


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
    pending_img_cleanup = []  # (old_rels, new_rels) applied after successful write
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

            # --- images ---
            if status != "error":
                img_want, img_diffs, img_err = sync_images(p, iid, dry_run=a.dry_run)
                if img_err:
                    print("[%s] %s" % (p["id"], img_err))
                elif img_diffs:
                    for k, v in img_want.items():
                        obj = set_field(obj, k, v)
                        # keep in-memory product in sync for later comparisons
                        p[k] = v
                    diffs.extend(img_diffs)
                else:
                    pass  # no image change

            if status != "error":
                print("[%s] %s cny=%s price=$%s size=%r soldout=%s%s" % (
                    p["id"], status, want.get("cny", p.get("cny")), want.get("price", p.get("price")),
                    want.get("size", p.get("size")), want.get("soldout", p.get("soldout")),
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

    out = text[:m.start(1)] + new_arr + text[m.end(1):] if changes else text
    if changes:
        json.loads(new_arr)  # sanity
    # --- Linktree contact links -> MY_LINKS ---
    try:
        links = fetch_linktree()
        if links:
            js = "var MY_LINKS = " + json.dumps(links, ensure_ascii=False, indent=2).replace("\n", "\n      ") + ";"
            out2, k = re.subn(r"var MY_LINKS = \[.*?\];", lambda _m: js, out, count=1, flags=re.S)
            if k and out2 != out:
                out = out2
                changes.append("MY_LINKS <- Linktree: " + ", ".join(l["label"] for l in links))
            print("Linktree: %d contact link(s)%s" % (len(links), "" if out2 == out and k else ""))
    except Exception as ex:
        print("Linktree SKIP: %s" % ex)

    if not changes:
        print("No changes.%s" % (" (%d item errors)" % errors if errors else ""))
        return
    for c in changes: print("  CHANGE: " + c)
    if a.dry_run:
        print("Dry run: %d change(s), file not written." % len(changes)); return
    open(a.file, "w", encoding="utf-8").write(out)
    print("Wrote %d change(s) to %s" % (len(changes), a.file))
    if a.push:
        g = ["git", "-C", ROOT]
        subprocess.run(g + ["add", "shop/index.html", "shop/img", "tools"], check=True)
        subprocess.run(g + ["-c", "user.name=13enji-ovo", "-c", "user.email=13enji-ovo@users.noreply.github.com",
                            "commit", "-qm", "自动同步微店/Linktree"], check=True)
        subprocess.run(g + ["push", "-q"], check=True)
        print("Pushed.")


if __name__ == "__main__":
    main()
