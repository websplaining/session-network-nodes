#!/usr/bin/env python3
"""Session network node dashboard scraper.

Fetches the Session Observer node list, scrapes each node's public_ip and
metadata, enriches IPs with geo/VPS-host data (ip-api.com batch), aggregates
summary + detail, and writes nodes.json for the /secret dashboard.

Caching makes re-runs cheap: node detail and geo are cached persistently, so
only new/changed nodes and new IPs are fetched on subsequent runs.
"""
import json, re, html, os, sys, time, argparse, urllib.request, concurrent.futures
from collections import Counter
from datetime import datetime, timezone

BASE = "https://observer.getsession.org"
LIST_URL = BASE + "/service_nodes"
NODE_URL = BASE + "/sn/{}/1"          # /1 form exposes public_ip
NODE_URL_BARE = BASE + "/sn/{}"
GEO_BATCH = "http://ip-api.com/batch"
GEO_FIELDS = "status,query,country,countryCode,regionName,city,isp,org,as"

WORK = "/root/session-nodes"
CACHE = os.path.join(WORK, "cache.json")      # pubkey -> node fields
GEOCACHE = os.path.join(WORK, "geo.json")     # ip -> geo fields
OUT_DIR = "/var/www/session-agent/html/secret"
OUT_JSON = os.path.join(OUT_DIR, "nodes.json")

NODE_TTL = 6 * 3600       # refetch a node's detail if older than 6h
CONCURRENCY = 8
TIMEOUT = 25
UA = {"User-Agent": "session-nodes-dashboard/1.0"}

# VPS host normalisation (matched case-insensitively against org/isp/as)
VENDORS = [
    ("ovh", "OVH"), ("hetzner", "Hetzner"), ("contabo", "Contabo"),
    ("digitalocean", "DigitalOcean"), ("amazon", "AWS"), ("aws", "AWS"),
    ("google", "Google Cloud"), ("netcup", "netcup"), ("mevspace", "MEVSPACE"),
    ("vultr", "Vultr"), ("choopa", "Vultr"), ("linode", "Linode"),
    ("akamai", "Akamai"), ("oracle", "Oracle Cloud"), ("microsoft", "Azure"),
    ("azure", "Azure"), ("scaleway", "Scaleway"), ("ionos", "IONOS"),
    ("kamatera", "Kamatera"), ("leaseweb", "Leaseweb"), ("equinix", "Equinix"),
    ("m247", "M247"), ("terrahost", "TerraHost"), ("hostinger", "Hostinger"),
    ("datacamp", "DataCamp"), ("servers.com", "Servers.com"), ("gcore", "Gcore"),
    ("cloudflare", "Cloudflare"), ("comcast", "Comcast"), ("verizon", "Verizon"),
    ("att", "AT&T"), ("telefonica", "Telefonica"), ("orange", "Orange"),
    ("vodafone", "Vodafone"), ("telia", "Telia"), ("deutsche telekom", "DTAG"),
    ("centurylink", "CenturyLink"), ("lumen", "Lumen"), ("cogent", "Cogent"),
]


def fetch(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def get_pubkeys():
    s = fetch(LIST_URL)
    # preserve order, dedupe
    seen, out = set(), []
    for pk in re.findall(r"/sn/([a-f0-9]{64})", s):
        if pk not in seen:
            seen.add(pk)
            out.append(pk)
    return out


def parse_node(page):
    for p in re.findall(r"<pre[^>]*>(.*?)</pre>", page, re.S):
        t = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if t.startswith("{"):
            try:
                d = json.loads(t)
                if "public_ip" in d:
                    return d
            except Exception:
                pass
    return None


def fetch_node(pk):
    """Return (pk, fields) or (pk, None) on failure."""
    for url in (NODE_URL.format(pk), NODE_URL_BARE.format(pk)):
        try:
            d = parse_node(fetch(url))
            if d:
                ver = d.get("service_node_version") or []
                fee = d.get("operator_fee")
                return pk, {
                    "ip": d.get("public_ip") or "",
                    "operator": d.get("operator_address") or "",
                    "active": bool(d.get("active")),
                    "fee": round(fee / 10000, 2) if isinstance(fee, (int, float)) else None,
                    "version": ".".join(map(str, ver)) if ver else "",
                    "swarm": d.get("swarm_id"),
                    "uptime": d.get("last_uptime_proof"),
                    "ts": int(time.time()),
                }
        except Exception:
            continue
    return pk, None


def norm_host(org, isp, asname):
    s = " ".join(x for x in (org, isp, asname) if x).lower()
    for k, v in VENDORS:
        if k in s:
            return v
    for cand in (org, isp):
        if cand:
            return cand
    if asname:
        parts = asname.split(" ", 1)
        return parts[1] if len(parts) > 1 else parts[0]
    return "Unknown"


def geo_batch(ips):
    url = GEO_BATCH + "?fields=" + GEO_FIELDS
    data = json.dumps(ips).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def enrich_geo(ips, geo):
    """Fill geo{} for any IPs not already cached. Returns geo dict."""
    todo = [ip for ip in ips if ip and ip not in geo]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        try:
            for item in geo_batch(chunk):
                q = item.get("query")
                if not q:
                    continue
                if item.get("status") == "success":
                    geo[q] = {
                        "country": item.get("country") or "Unknown",
                        "cc": (item.get("countryCode") or "").lower(),
                        "region": item.get("regionName") or "",
                        "city": item.get("city") or "",
                        "host": norm_host(item.get("org"), item.get("isp"), item.get("as")),
                    }
                else:
                    geo[q] = {"country": "Unknown", "cc": "", "region": "", "city": "", "host": "Unknown"}
        except Exception as e:
            print(f"  geo batch error: {e}", file=sys.stderr)
        time.sleep(1.5)  # stay under ip-api free rate limit
    return geo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="process only first N nodes (testing)")
    ap.add_argument("--out", default=OUT_JSON)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    cache = load(CACHE, {})
    geo = load(GEOCACHE, {})

    print("Fetching node list...")
    pubkeys = get_pubkeys()
    if args.limit:
        pubkeys = pubkeys[:args.limit]
    print(f"  {len(pubkeys)} nodes")

    # Which nodes need a (re)fetch?
    now = int(time.time())
    todo = [pk for pk in pubkeys
            if pk not in cache or now - cache[pk].get("ts", 0) > NODE_TTL]
    print(f"  {len(todo)} need detail fetch (cached: {len(pubkeys) - len(todo)})")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for pk, fields in ex.map(fetch_node, todo):
            if fields:
                cache[pk] = fields
            done += 1
            if done % 100 == 0:
                print(f"    fetched {done}/{len(todo)}")
    # drop nodes no longer in the list
    for pk in list(cache.keys()):
        if pk not in pubkeys:
            del cache[pk]

    # Geo-enrich unique IPs
    ips = sorted({f["ip"] for f in cache.values() if f.get("ip")})
    new_ips = [ip for ip in ips if ip not in geo]
    print(f"  {len(ips)} unique IPs, {len(new_ips)} need geo lookup")
    geo = enrich_geo(ips, geo)

    save(CACHE, cache)
    save(GEOCACHE, geo)

    # Build detail rows (only for nodes in the current list)
    detail = []
    for pk in pubkeys:
        f = cache.get(pk)
        if not f:
            continue
        ip = f.get("ip") or ""
        g = geo.get(ip, {}) if ip else {}
        detail.append({
            "pk": pk[:10],
            "pkfull": pk,
            "ip": ip,
            "country": g.get("country", "Unknown"),
            "cc": g.get("cc", ""),
            "region": g.get("region", ""),
            "city": g.get("city", ""),
            "host": g.get("host", "Unknown"),
            "operator": f.get("operator", ""),
            "active": f.get("active", False),
            "fee": f.get("fee"),
            "version": f.get("version", ""),
            "swarm": f.get("swarm"),
            "uptime": f.get("uptime"),
        })

    by_country = Counter(d["country"] for d in detail)
    by_host = Counter(d["host"] for d in detail)
    cc_by_country = {}
    for d in detail:
        if d["country"] not in cc_by_country and d["cc"]:
            cc_by_country[d["country"]] = d["cc"]

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_nodes": len(detail),
        "total_countries": len(by_country),
        "total_hosts": len(by_host),
        "active_nodes": sum(1 for d in detail if d["active"]),
        "countries": [{"country": c, "cc": cc_by_country.get(c, ""), "count": n}
                      for c, n in by_country.most_common()],
        "hosts": [{"host": h, "count": n} for h, n in by_host.most_common()],
        "nodes": detail,
    }
    save(args.out, out)
    print(f"Wrote {args.out}: {out['total_nodes']} nodes, "
          f"{out['total_countries']} countries, {out['total_hosts']} hosts")


if __name__ == "__main__":
    main()
