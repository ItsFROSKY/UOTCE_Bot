# crawls the whole course drive once and caches it in redis
# browsing/search come from here so its instant, and its also the security check:
# if a file id isnt inside the course folder it doesnt exist for the bot
import json
import logging
import re
import time

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE = "application/vnd.google-apps."

INDEX_KEY = "miniapp:index:v1"
INDEX_TTL = 30 * 60          # rebuild at most every 30 minutes
MAX_FOLDERS = 3000           # safety limit for shortcut loops
TG_LIMIT = 50 * 1024 * 1024  # telegram bot upload limit

_memory = {"index": None, "at": 0.0}



def _list_children(drive, folder_id):
    items, token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, "
                   "createdTime, webViewLink, shortcutDetails)",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return items


def build_index(drive, root_id, root_name="الملفات"):
    nodes = {root_id: {"id": root_id, "name": root_name, "kind": "folder", "parent": None}}
    children = {}
    queue, seen = [root_id], {root_id}

    while queue:
        if len(seen) > MAX_FOLDERS:
            logging.warning("drive index: folder limit reached, index is partial")
            break
        folder_id = queue.pop(0)
        kids = []
        for item in _list_children(drive, folder_id):
            mime = item.get("mimeType", "")
            item_id = item["id"]
            if mime == SHORTCUT:
                details = item.get("shortcutDetails") or {}
                item_id = details.get("targetId", item_id)
                mime = details.get("targetMimeType", mime)
            if item_id in nodes:  # shortcut to something we already have
                continue

            node = {
                "id": item_id,
                "name": item.get("name", ""),
                "parent": folder_id,
                "modified": item.get("modifiedTime") or item.get("createdTime"),
            }
            if mime == FOLDER:
                node["kind"] = "folder"
                if item_id not in seen:
                    seen.add(item_id)
                    queue.append(item_id)
            else:
                node["kind"] = "file"
                node["mime"] = mime
                node["size"] = int(item["size"]) if item.get("size") else None
                node["link"] = item.get("webViewLink")
                if mime.startswith(GOOGLE_NATIVE):
                    node["native"] = True  # google docs cant be downloaded directly
            nodes[item_id] = node
            kids.append(item_id)
        children[folder_id] = kids

    return {"root": root_id, "built": time.time(), "nodes": nodes, "children": children}


def get_index(drive, redis, root_id, force=False):
    now = time.time()
    cached = _memory["index"]
    if not force and cached and cached["root"] == root_id and now - _memory["at"] < 300:  # 5 min, saves redis reads
        return cached

    if not force:
        try:
            raw = redis.get(INDEX_KEY)
            if raw:
                idx = json.loads(raw)
                if idx.get("root") == root_id and now - idx.get("built", 0) < INDEX_TTL:
                    _memory.update(index=idx, at=now)
                    return idx
        except Exception:
            logging.exception("drive index: redis read failed")

    idx = build_index(drive, root_id)
    try:
        redis.set(INDEX_KEY, json.dumps(idx, ensure_ascii=False, separators=(",", ":")),
                  ex=INDEX_TTL * 4)
    except Exception:
        logging.exception("drive index: redis write failed")
    _memory.update(index=idx, at=now)
    return idx



def contains(idx, item_id):
    return item_id in idx["nodes"]


def public(node, idx=None, with_path=False):
    out = {k: node.get(k) for k in ("id", "name", "kind", "size", "modified", "mime", "link")
           if node.get(k) is not None}
    if node.get("native"):
        out["native"] = True
    if node["kind"] == "folder" and idx is not None:
        out["count"] = len(idx["children"].get(node["id"], []))
    if with_path and idx is not None:
        out["path"] = " / ".join(n["name"] for n in breadcrumb(idx, node.get("parent"))[1:])
    return out


def breadcrumb(idx, item_id):
    trail, nodes = [], idx["nodes"]
    while item_id and item_id in nodes and len(trail) < 50:
        node = nodes[item_id]
        trail.append({"id": node["id"], "name": node["name"]})
        item_id = node.get("parent")
    return list(reversed(trail))


# so الأولى/الثانية/الثالثة sort in order and not alphabetically
_ORDINALS = {"الاولي": 1, "الاول": 1, "الثانيه": 2, "الثاني": 2, "الثالثه": 3, "الثالث": 3,
             "الرابعه": 4, "الرابع": 4, "الخامسه": 5, "الخامس": 5, "السادسه": 6, "السادس": 6,
             "السابعه": 7, "الثامنه": 8, "التاسعه": 9, "العاشره": 10}


def _sort_key(node):
    # folders first, and "Lec 2" before "Lec 10"
    words = [str(_ORDINALS.get(w, w)) for w in normalize(node["name"]).split()]
    parts = re.split(r"(\d+)", " ".join(words))
    return (node["kind"] != "folder", [int(p) if p.isdigit() else p for p in parts])


def list_folder(idx, folder_id):
    kids = [idx["nodes"][i] for i in idx["children"].get(folder_id, []) if i in idx["nodes"]]
    kids.sort(key=_sort_key)
    return [public(k, idx) for k in kids]


_DIACRITICS = re.compile(r"[ؗ-ًؚ-ْـ]")


def normalize(text):
    # أ/إ/آ -> ا , ى -> ي , ة -> ه and remove tashkeel so arabic search isnt picky
    text = _DIACRITICS.sub("", (text or "").lower())
    text = re.sub("[أإآٱ]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[_\-.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def search(idx, query, limit=60):
    terms = normalize(query).split()
    if not terms:
        return []
    scored = []
    for node in idx["nodes"].values():
        if node["parent"] is None:
            continue
        name = normalize(node["name"])
        path = normalize(" ".join(n["name"] for n in breadcrumb(idx, node["parent"])))
        hay = f"{name} {path}"
        if all(t in hay for t in terms):
            score = sum(3 if t in name else 1 for t in terms)
            if name.startswith(terms[0]):
                score += 2
            scored.append((score, node))
    scored.sort(key=lambda s: (-s[0], _sort_key(s[1])))
    return [public(n, idx, with_path=True) for _, n in scored[:limit]]


def recent(idx, limit=12):
    files = [n for n in idx["nodes"].values() if n["kind"] == "file" and n.get("modified")]
    files.sort(key=lambda n: n["modified"], reverse=True)
    return [public(n, idx, with_path=True) for n in files[:limit]]
