# القعدة: Subnet Rush game + leaderboard + batch polls
# the server makes the questions and checks the answers so nobody can fake a score
import ipaddress
import json
import random
import time
import uuid
from datetime import timedelta

import campus as cp
from config import redis

ROUND_KEY = "game:subnet:{rid}"
LB_WEEK = "lb:subnet:w:{week}"
LB_ALL = "lb:subnet:all"
LB_NAMES = "lb:names"
POLLS_KEY = "miniapp:polls"
VOTES_KEY = "miniapp:poll:{pid}:votes"

QUESTIONS = 10
SECONDS = 20
GRACE = 5  # slow phones / network

TITLES = [(0, "توه بادي 🐣"), (400, "يدبّرها 👌"), (900, "سبع الشبكات 🦁"),
          (1400, "أسطورة الراوتر 🔥"), (1800, "عمو الـ Subnet 👑")]
ASK = {"network": "شنو الـ Network Address؟", "broadcast": "شنو الـ Broadcast Address؟",
       "hosts": "كم جهاز (Usable Hosts) يصير بهالشبكة؟", "mask": "شنو الـ Subnet Mask؟"}


def title_for(score):
    t = TITLES[0][1]
    for need, name in TITLES:
        if score >= need:
            t = name
    return t


def week_key(d=None):
    d = d or cp.now().date()
    return (d - timedelta(days=cp.day_index(d))).isoformat()  # week starts sunday


# subnet questions

def _block_info(net):
    p = net.prefixlen
    octet = 4 if p >= 24 else 3 if p >= 16 else 2
    block = 2 ** ((octet * 8) - p)
    return octet, block


def explain(net):
    octet, block = _block_info(net)
    return (f"الماسك {net.netmask} ← البلوك {block} بالأوكتيت {octet}\n"
            f"الشبكة {net.network_address} ، البث {net.broadcast_address} ، الأجهزة {net.num_addresses - 2}")


def make_question(level, rng=random):
    if level < 3:
        prefix = rng.randint(25, 30)
    elif level < 6:
        prefix = rng.randint(21, 30)
    else:
        prefix = rng.randint(17, 29)
    first = rng.choice([(192, 168), (10, rng.randint(0, 255)), (172, rng.randint(16, 31))])
    ip = ipaddress.IPv4Address(f"{first[0]}.{first[1]}.{rng.randint(0, 255)}.{rng.randint(1, 254)}")
    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    kind = rng.choice(["network", "network", "broadcast", "broadcast", "hosts", "mask"])
    size = net.num_addresses

    def addr(x):
        try:
            return str(ipaddress.IPv4Address(int(x)))
        except ValueError:
            return None

    if kind == "network":
        n = int(net.network_address)
        answer = str(net.network_address)
        wrong = [addr(n + size), addr(n - size), str(net.broadcast_address), addr(int(ip) & 0xFFFFFF00), addr(n + 1)]
    elif kind == "broadcast":
        b = int(net.broadcast_address)
        answer = str(net.broadcast_address)
        wrong = [addr(b + size), addr(b - size), addr(b - 1), str(net.network_address), addr(int(ip) | 0xFF)]
    elif kind == "hosts":
        answer = str(size - 2)
        wrong = [str(size), str(size - 1), str(size * 2 - 2), str(max(2, size // 2 - 2)), str(size + 2)]
    else:
        answer = str(net.netmask)
        wrong = [str(ipaddress.IPv4Network(f"0.0.0.0/{p}").netmask) for p in (prefix - 1, prefix + 1, prefix - 2, prefix + 2)
                 if 8 <= p <= 32]
    opts = [answer]
    for w in wrong:
        if w and w not in opts and len(opts) < 4:
            opts.append(w)
    while len(opts) < 4:  # rare, fill with neighbours
        extra = addr(int(net.network_address) + size * rng.randint(2, 6)) if kind != "hosts" else str(rng.randint(3, 500))
        if extra not in opts:
            opts.append(extra)
    rng.shuffle(opts)
    return {"ip": str(ip), "prefix": prefix, "kind": kind, "text": ASK[kind], "options": opts,
            "answer": opts.index(answer), "explain": explain(net)}


def _public_q(q, i):
    return {"i": i, "total": QUESTIONS, "ip": q["ip"], "prefix": q["prefix"], "text": q["text"],
            "options": q["options"], "seconds": SECONDS}


def start_round(uid, name):
    rid = uuid.uuid4().hex[:12]
    q = make_question(0)
    q["asked"] = time.time()
    state = {"uid": uid, "i": 0, "score": 0, "streak": 0, "best_streak": 0, "correct": 0, "q": q}
    redis.set(ROUND_KEY.format(rid=rid), json.dumps(state, ensure_ascii=False), ex=900)
    redis.hset(LB_NAMES, str(uid), (name or "طالب")[:30])
    return {"round": rid, "question": _public_q(q, 0)}


def answer(uid, rid, choice):
    raw = redis.get(ROUND_KEY.format(rid=rid))
    if not raw:
        raise LookupError("round_not_found")
    st = json.loads(raw)
    if st["uid"] != uid:
        raise LookupError("round_not_found")
    q = st["q"]
    elapsed = time.time() - q["asked"]
    ok = isinstance(choice, int) and choice == q["answer"] and elapsed <= SECONDS + GRACE
    points = 0
    if ok:
        st["streak"] += 1
        st["correct"] += 1
        st["best_streak"] = max(st["best_streak"], st["streak"])
        speed = max(0.0, SECONDS - elapsed) / SECONDS
        points = 100 + int(50 * speed) + min(100, 20 * max(0, st["streak"] - 1))
        st["score"] += points
    else:
        st["streak"] = 0
    out = {"correct": ok, "answer": q["answer"], "explain": q["explain"], "points": points,
           "score": st["score"], "streak": st["streak"], "timeout": elapsed > SECONDS + GRACE}
    st["i"] += 1
    if st["i"] >= QUESTIONS:
        redis.delete(ROUND_KEY.format(rid=rid))
        out["done"] = True
        out["result"] = finish(uid, st)
        return out
    nq = make_question(st["i"])
    nq["asked"] = time.time()
    st["q"] = nq
    redis.set(ROUND_KEY.format(rid=rid), json.dumps(st, ensure_ascii=False), ex=900)
    out["done"] = False
    out["next"] = _public_q(nq, st["i"])
    return out


def _zbest(key, uid, score):
    prev = redis.zscore(key, str(uid))
    if prev is None or score > float(prev):
        redis.zadd(key, {str(uid): score})
        return True
    return False


def finish(uid, st):
    wk = LB_WEEK.format(week=week_key())
    new_week_best = _zbest(wk, uid, st["score"])
    redis.expire(wk, 90 * 86400)
    _zbest(LB_ALL, uid, st["score"])
    best = float(redis.zscore(LB_ALL, str(uid)) or 0)
    rank = redis.zrevrank(wk, str(uid))
    return {"score": st["score"], "correct": st["correct"], "total": QUESTIONS, "best_streak": st["best_streak"],
            "title": title_for(st["score"]), "best": int(best), "week_rank": None if rank is None else rank + 1,
            "new_best": new_week_best}


def _pairs(rows):
    # upstash gives [(member, score)], some versions give a flat list
    if rows and not isinstance(rows[0], (list, tuple)):
        rows = list(zip(rows[0::2], rows[1::2]))
    return [(str(m), float(s)) for m, s in (rows or [])]


def leaderboard(uid, which="week", limit=10):
    key = LB_WEEK.format(week=week_key()) if which == "week" else LB_ALL
    rows = _pairs(redis.zrange(key, 0, limit - 1, rev=True, withscores=True))
    names = redis.hgetall(LB_NAMES) or {}
    top = [{"rank": i + 1, "name": names.get(m, "طالب"), "score": int(s), "title": title_for(s), "me": m == str(uid)}
           for i, (m, s) in enumerate(rows)]
    r = redis.zrevrank(key, str(uid))
    s = redis.zscore(key, str(uid))
    me = {"rank": r + 1, "score": int(float(s))} if r is not None and s is not None else None
    return {"top": top, "me": me, "players": redis.zcard(key) or 0, "which": which}


def announce_week_champion():
    last = (cp.now().date() - timedelta(days=7))
    wk = week_key(last)
    if not redis.set(f"lb:announced:{wk}", "1", nx=True, ex=60 * 86400):
        return None
    rows = _pairs(redis.zrange(LB_WEEK.format(week=wk), 0, 2, rev=True, withscores=True))
    if not rows:
        return None
    names = redis.hgetall(LB_NAMES) or {}
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 أبطال Subnet Rush للأسبوع اللي فات", ""]
    lines += [f"{medals[i]} {names.get(m, 'طالب')} - {int(s)} نقطة" for i, (m, s) in enumerate(rows)]
    lines += ["", "الأسبوع الجديد بدا، منو يكسرها؟ 💪"]
    cp.post_to_group("\n".join(lines), cp.markup(cp.app_button("🎮 العب هسه", "fun", private=False)))
    return rows[0]


# polls (anonymous, results show after you vote)

def _load_polls():
    return cp._load(POLLS_KEY, [])


def create_poll(question, options, hours=24, by=None):
    question = (question or "").strip()[:200]
    options = [str(o).strip()[:80] for o in (options or []) if str(o).strip()]
    if not question:
        raise ValueError("اكتب السؤال")
    if not 2 <= len(options) <= 6:
        raise ValueError("لازم من 2 لـ 6 خيارات")
    if len(set(options)) != len(options):
        raise ValueError("في خيارات مكررة")
    hours = max(1, min(int(hours or 24), 168))
    p = {"id": uuid.uuid4().hex[:8], "question": question, "options": options, "created": time.time(),
         "closes": time.time() + hours * 3600, "by": by, "closed": False}
    polls = [x for x in _load_polls() if x["created"] > time.time() - 30 * 86400][-19:]
    cp._save(POLLS_KEY, polls + [p])
    cp.post_to_group(f"🗳 تصويت جديد للدفعة\n\n{question}\n\nصوّت من التطبيق 👇 (التصويت سري)",
                     cp.markup(cp.app_button("🗳 صوّت", "fun", private=False)))
    cp.refresh_dashboard()
    return p


def _tally(p):
    votes = redis.hgetall(VOTES_KEY.format(pid=p["id"])) or {}
    counts = [0] * len(p["options"])
    for v in votes.values():
        i = int(v)
        if 0 <= i < len(counts):
            counts[i] += 1
    return counts, votes


def poll_view(p, uid, admin):
    counts, votes = _tally(p)
    mine = votes.get(str(uid))
    closed = p["closed"] or p["closes"] < time.time()
    show = mine is not None or closed or admin
    return {"id": p["id"], "question": p["question"], "options": p["options"], "closes": p["closes"],
            "closed": closed, "my_vote": None if mine is None else int(mine), "total": sum(counts),
            "counts": counts if show else None}


def list_polls(uid, admin):
    polls = sorted(_load_polls(), key=lambda p: -p["created"])
    return [poll_view(p, uid, admin) for p in polls if p["created"] > time.time() - 14 * 86400]


def vote(pid, uid, option):
    p = next((x for x in _load_polls() if x["id"] == pid), None)
    if not p:
        raise LookupError("poll_not_found")
    if p["closed"] or p["closes"] < time.time():
        raise ValueError("التصويت انغلق")
    if not isinstance(option, int) or not 0 <= option < len(p["options"]):
        raise ValueError("خيار غير صحيح")
    redis.hset(VOTES_KEY.format(pid=pid), str(uid), str(option))  # changing your vote is allowed
    redis.expire(VOTES_KEY.format(pid=pid), 60 * 86400)
    return p


def results_text(p):
    counts, _ = _tally(p)
    total = sum(counts) or 1
    lines = [f"📊 نتيجة التصويت ({sum(counts)} صوت)", "", p["question"], ""]
    for opt, c in sorted(zip(p["options"], counts), key=lambda x: -x[1]):
        pct = round(c * 100 / total)
        lines.append(f"{'▓' * (pct // 10)}{'░' * (10 - pct // 10)} {pct}%  {opt} ({c})")
    return "\n".join(lines)


def close_poll(pid):
    polls = _load_polls()
    p = next((x for x in polls if x["id"] == pid), None)
    if not p:
        raise LookupError("poll_not_found")
    if not p.get("announced"):
        cp.post_to_group(results_text(p))
    p["closed"] = True
    p["announced"] = True
    cp._save(POLLS_KEY, polls)
    cp.refresh_dashboard()
    return p


def delete_poll(pid):
    cp._save(POLLS_KEY, [x for x in _load_polls() if x["id"] != pid])
    redis.delete(VOTES_KEY.format(pid=pid))
    cp.refresh_dashboard()


def close_expired_polls():
    done = 0
    for p in _load_polls():
        if not p.get("announced") and p["closes"] < time.time():
            close_poll(p["id"])
            done += 1
    return done


def open_poll_line():
    open_polls = [p for p in _load_polls() if not p["closed"] and p["closes"] > time.time()]
    if not open_polls:
        return None
    return f"🗳 تصويت مفتوح: {open_polls[-1]['question'][:100]}"
