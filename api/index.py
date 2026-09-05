from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
UA = "Shelfwright-Plus-Vercel/0.3"


def get(url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, timeout=25, **kwargs)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "mode": "vercel-public-catalog"})


@app.get("/api/author")
def author():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "empty author"}), 400
    r = get("https://openlibrary.org/search/authors.json", params={"q": q, "limit": 8})
    r.raise_for_status()
    out = []
    for row in r.json().get("docs", []):
        key = row.get("key") or ""
        if key and not key.startswith("/authors/"):
            key = "/authors/" + key
        out.append({"key": key, "name": row.get("name") or "",
                    "birth_date": row.get("birth_date") or "",
                    "top_work": row.get("top_work") or "",
                    "work_count": row.get("work_count") or 0})
    return jsonify({"authors": out})


@app.get("/api/works")
def works():
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "missing author key"}), 400
    if not key.startswith("/"):
        key = "/authors/" + key
    url = f"https://openlibrary.org{key}/works.json"
    offset, rows, seen = 0, [], set()
    while offset < 1000:
        r = get(url, params={"limit": 50, "offset": offset})
        r.raise_for_status()
        data = r.json()
        entries = data.get("entries", [])
        if not entries:
            break
        for w in entries:
            title = (w.get("title") or "").strip()
            n = norm(title)
            if not title or n in seen:
                continue
            seen.add(n)
            rows.append({"key": w.get("key") or "", "title": title,
                         "first_publish_date": w.get("first_publish_date") or ""})
        offset += len(entries)
        if len(entries) < 50:
            break
    return jsonify({"works": rows, "total": len(rows)})


@app.get("/api/archive")
def archive():
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    if not title:
        return jsonify({"error": "missing title"}), 400
    tq = title.replace('"', '')
    aq = author.replace('"', '')
    queries = []
    if aq:
        queries.append(f'title:("{tq}") AND (creator:("{aq}") OR contributor:("{aq}"))')
    queries += [f'title:("{tq}")', f'title:({tq})']
    found = {}
    for q in queries:
        params = {"q": q, "fl[]": ["identifier", "title", "creator", "date", "year", "language", "mediatype", "access-restricted-item", "downloads"], "rows": 25, "output": "json"}
        r = get("https://archive.org/advancedsearch.php", params=params)
        r.raise_for_status()
        for row in r.json().get("response", {}).get("docs", []):
            ident = str(row.get("identifier") or "")
            if not ident or ident in found:
                continue
            ct = str(row.get("title") or "")
            if sim(ct, title) < .50:
                continue
            restricted = str(row.get("access-restricted-item") or "").lower() in {"true", "1", "yes"}
            found[ident] = {"identifier": ident, "title": ct, "creator": row.get("creator") or "",
                            "year": row.get("year") or row.get("date") or "",
                            "language": row.get("language") or "", "restricted": restricted,
                            "downloads": row.get("downloads") or 0,
                            "url": f"https://archive.org/details/{quote(ident)}",
                            "score": sim(ct, title)}
        if len(found) >= 10:
            break
    rows = sorted(found.values(), key=lambda x: (x["score"], not x["restricted"], x["downloads"]), reverse=True)[:10]
    return jsonify({"results": rows})


@app.get("/api/archive/files")
def archive_files():
    ident = (request.args.get("id") or "").strip()
    if not ident:
        return jsonify({"error": "missing identifier"}), 400
    r = get(f"https://archive.org/metadata/{quote(ident, safe='')}")
    r.raise_for_status()
    data = r.json(); meta = data.get("metadata") or {}
    if str(meta.get("access-restricted-item") or "").lower() in {"true", "1", "yes"} or str(meta.get("is_dark") or "").lower() in {"true", "1", "yes"}:
        return jsonify({"files": []})
    pref = {"pdf": 120, "epub": 112, "djvu": 80, "txt": 55}
    out=[]
    for f in data.get("files") or []:
        name=str(f.get("name") or ""); low=name.lower(); ext=low.rsplit('.',1)[-1] if '.' in low else ''
        if ext not in pref or str(f.get("private") or "").lower() in {"true","1","yes"}: continue
        fmt=str(f.get("format") or ""); score=pref[ext]
        if "text pdf" in fmt.lower() or "searchable" in fmt.lower(): score+=16
        try: size=int(f.get("size") or 0)
        except: size=0
        out.append({"name":name,"extension":ext,"format":fmt,"size":size,"score":score,
                    "url":f"https://archive.org/download/{quote(ident,safe='')}/{quote(name)}"})
    out.sort(key=lambda x:(x["score"],x["size"]),reverse=True)
    return jsonify({"files":out})
