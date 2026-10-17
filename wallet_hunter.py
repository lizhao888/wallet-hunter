#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wallet-hunter — 链上早期买家 / 聪明钱共识扫描（7 步法）

零依赖（只用 Python 标准库），可在任意机器上跑。数据全部来自公开链上接口，
不需要 API key。

子命令
  doctor                    体检：哪些数据源当前可用
  launchpad                 列出发射平台最近新发的币（four.meme / 任意 EVM 工厂）
  scan                      对一个币跑完 7 步：最早买家 → 活跃度 → 去机器人 → 共识 → 打分

用法
  python wallet_hunter.py doctor
  python wallet_hunter.py launchpad --source fourmeme --hours 6 --limit 20
  python wallet_hunter.py scan --chain bsc --launchpad fourmeme --token 0xabc...
  python wallet_hunter.py scan --chain eth --token 0x6982508145454ce325ddbe47a25d4ec3d2311933 --first 20

环境变量（都可选，用来换成自己的节点/实例）
  WH_RPC_ETH   WH_RPC_BSC   WH_BS_ETH   WH_OUT
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

VERSION = "1.0.0"
NOW = datetime.now(timezone.utc)

# ----------------------------------------------------------------- 常量
MASK = (1 << 64) - 1
RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
      0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
      0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
      0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
      0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
      0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
       [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]

ACTIVE_DAYS = 30
BOT_GAP_S = 60
BOT_TX30 = 500
CONSENSUS_DAYS = 7
MIN_MCAP = 200_000
MIN_HOLDERS = 500

# 稳定币/包装币天然出现在所有钱包里，不算共识
STABLE_TOKENS = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
}


def _rol(v, n):
    n %= 64
    return ((v << n) | (v >> (64 - n))) & MASK if n else v


def _keccak_f(st):
    for rnd in range(24):
        C = [st[x] ^ st[x + 5] ^ st[x + 10] ^ st[x + 15] ^ st[x + 20] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                st[x + 5 * y] ^= D[x]
        B = [0] * 25
        for x in range(5):
            for y in range(5):
                B[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(st[x + 5 * y], ROT[x][y])
        for x in range(5):
            for y in range(5):
                st[x + 5 * y] = B[x + 5 * y] ^ ((~B[(x + 1) % 5 + 5 * y] & MASK) & B[(x + 2) % 5 + 5 * y])
        st[0] ^= RC[rnd]


def keccak256(data: bytes) -> bytes:
    rate, pad = 136, bytearray(data)
    pad.append(0x01)
    while len(pad) % rate != rate - 1:
        pad.append(0x00)
    pad.append(0x80)
    st = [0] * 25
    for off in range(0, len(pad), rate):
        blk = pad[off:off + rate]
        for i in range(rate // 8):
            st[i] ^= int.from_bytes(blk[i * 8:(i + 1) * 8], "little")
        _keccak_f(st)
    return st[0].to_bytes(8, "little") + st[1].to_bytes(8, "little") + \
        st[2].to_bytes(8, "little") + st[3].to_bytes(8, "little")


def topic(sig: str) -> str:
    return "0x" + keccak256(sig.encode()).hex()


TRANSFER_TOPIC = topic("Transfer(address,address,uint256)")

CHAINS = {
    "eth": {
        "blockscout": os.environ.get("WH_BS_ETH", "https://eth.blockscout.com"),
        "rpc": [os.environ.get("WH_RPC_ETH") or "https://eth.drpc.org",
                "https://rpc.mevblocker.io", "https://ethereum.publicnode.com"],
    },
    "bsc": {
        "blockscout": None,
        "rpc": [os.environ.get("WH_RPC_BSC") or "https://bsc.publicnode.com",
                "https://bsc-dataseed.binance.org"],
    },
}

# 发射平台适配器：合约 + 事件签名（签名用于算 topic0，不需要硬编码哈希）
LAUNCHPADS = {
    "fourmeme": {
        "chain": "bsc",
        "name": "four.meme (TokenManager2)",
        "contract": "0x5c952063c7fc8610FFDB798152D69F0B9550762b",
        "created_sig": "TokenCreate(address,address,uint256,string,string,uint256,uint256,uint256)",
        "buy_sig": "TokenPurchase(address,address,uint256,uint256,uint256,uint256,uint256,uint256)",
        # 这两个事件都是「非 indexed」：参数全在 data 里，按 ABI 静态字解码
        "created_token_word": 1,   # data[0]=creator, data[1]=token
        "buy_token_word": 0,       # data[0]=token, data[1]=account
        "buy_account_word": 1,
    },
}


def _word(data_hex, i):
    """取 ABI 编码 data 的第 i 个 32 字节字。"""
    body = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return body[i * 64:(i + 1) * 64]


def _addr_word(data_hex, i):
    w = _word(data_hex, i)
    return "0x" + w[-40:].lower() if len(w) == 64 else None

UA = {"User-Agent": f"wallet-hunter/{VERSION} (+https://github.com/)"}
_last = [0.0]


# ----------------------------------------------------------------- 取数
def http_json(url, data=None, tries=4):
    for i in range(tries):
        gap = time.time() - _last[0]
        if gap < 0.85:
            time.sleep(0.85 - gap)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers={**UA,
                                       **({"Content-Type": "application/json"} if data else {})})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (i + 1))
                continue
            if i == tries - 1:
                return None
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


class Rpc:
    def __init__(self, urls):
        self.urls, self.i = list(urls), 0

    def __call__(self, method, params):
        for _ in range(len(self.urls) * 2):
            url = self.urls[self.i % len(self.urls)]
            d = http_json(url, json.dumps({"jsonrpc": "2.0", "id": 1,
                                           "method": method, "params": params}).encode())
            if isinstance(d, dict) and "result" in d:
                return d["result"]
            self.i += 1
        return None

    def block(self):
        h = self("eth_blockNumber", [])
        return int(h, 16) if h else None

    def logs(self, address, topics, frm, to):
        return self("eth_getLogs", [{"address": address, "topics": topics,
                                     "fromBlock": hex(frm), "toBlock": hex(to)}])

    def is_contract(self, addr):
        c = self("eth_getCode", [addr, "latest"])
        return bool(c) and c != "0x"


def dexscreener(ca):
    """免 key 的行情兜底（有 BSC/ETH 都能用）：拿市值、流动性、24h 量。"""
    d = http_json(f"https://api.dexscreener.com/latest/dex/tokens/{ca}") or {}
    pairs = d.get("pairs") or []
    if not pairs:
        return {}
    p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    try:
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
    except Exception:
        mcap = 0.0
    try:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
    except Exception:
        liq = 0.0
    try:
        vol = float((p.get("volume") or {}).get("h24") or 0)
    except Exception:
        vol = 0.0
    return {"symbol": (p.get("baseToken") or {}).get("symbol") or "",
            "name": (p.get("baseToken") or {}).get("name") or "",
            "mcap": mcap, "liq": liq, "vol24": vol, "holders": 0}


class Evm:
    def __init__(self, chain):
        cfg = CHAINS[chain]
        self.chain, self.bs = chain, cfg["blockscout"]
        self.rpc = Rpc(cfg["rpc"])

    # --- Blockscout v2（有的链没有实例，就用 RPC 兜底）
    def v2(self, path, **params):
        if not self.bs:
            return None
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return http_json(f"{self.bs}/api/v2/{path}{q}")

    def token_meta(self, ca):
        d = self.v2(f"tokens/{ca}") or {}
        try:
            mcap = float(d.get("circulating_market_cap") or 0)
        except Exception:
            mcap = 0.0
        try:
            holders = int(d.get("holders_count") or 0)
        except Exception:
            holders = 0
        if d.get("symbol"):
            return {"symbol": d.get("symbol") or "", "name": d.get("name") or "",
                    "mcap": mcap, "holders": holders}
        ds = dexscreener(ca)          # 没有 Blockscout 实例的链（如 BSC）走行情兜底
        return {"symbol": ds.get("symbol", ""), "name": ds.get("name", ""),
                "mcap": ds.get("mcap", 0.0), "holders": ds.get("holders", 0)}

    def creation_block(self, ca, hint=None):
        if hint:
            return hint
        d = self.v2(f"addresses/{ca}") or {}
        txh = d.get("creation_transaction_hash")
        if not txh:
            return None
        tx = self.rpc("eth_getTransactionByHash", [txh]) or {}
        return int(tx["blockNumber"], 16) if tx.get("blockNumber") else None

    # --- 事件扫描：Blockscout 旧接口优先，失败切 RPC，结果太多自动缩窗
    def scan_logs(self, address, topic0, start, want, span=1500, rounds=40):
        out, blk, r = [], start, 0
        while r < rounds and len(out) < want:
            r += 1
            got = None
            if self.bs:
                d = http_json(f"{self.bs}/api?module=logs&action=getLogs&fromBlock={blk}"
                              f"&toBlock={blk + span}&address={address}&topic0={topic0}")
                if d and d.get("result") is not None:
                    got = d["result"]
            if got is None:
                got = self.rpc.logs(address, [topic0], blk, blk + span)
                if got is None:
                    span = max(25, span // 2)
                    continue
            out.extend(got)
            blk += span
        return out

    def wallet_profile(self, addr):
        tr = (self.v2(f"addresses/{addr}/token-transfers", type="ERC-20") or {}).get("items") or []
        txs = []
        stamps, recv = [], defaultdict(lambda: {"symbol": "", "count": 0, "last": None, "senders": {}})
        for it in tr:
            t = _parse_ts(it.get("timestamp"))
            if not t:
                continue
            stamps.append(t)
            if ((it.get("to") or {}).get("hash") or "").lower() == addr.lower():
                tok = it.get("token") or {}
                tk = (tok.get("address_hash") or tok.get("address") or "?").lower()
                frm = ((it.get("from") or {}).get("hash") or "").lower()
                recv[tk]["symbol"] = tok.get("symbol") or "?"
                recv[tk]["count"] += 1
                if frm:
                    recv[tk]["senders"][frm] = recv[tk]["senders"].get(frm, 0) + 1
                recv[tk]["last"] = max(recv[tk]["last"], t) if recv[tk]["last"] else t
        newest = max(stamps) if stamps else None
        if newest is None or (NOW - newest).days > ACTIVE_DAYS:      # 只在疑似休眠时补查原生交易
            txs = (self.v2(f"addresses/{addr}/transactions") or {}).get("items") or []
            for it in txs:
                t = _parse_ts(it.get("timestamp"))
                if t:
                    stamps.append(t)
        stamps.sort(reverse=True)
        last = stamps[0] if stamps else None
        gaps = [(stamps[i] - stamps[i + 1]).total_seconds() for i in range(len(stamps) - 1)]
        return {"address": addr,
                "last_active": last.isoformat()[:16] if last else None,
                "days_since": (NOW - last).days if last else None,
                "token_transfers": len(tr), "native_txs": len(txs),
                "tx_30d": sum(1 for t in stamps if (NOW - t).days <= ACTIVE_DAYS),
                "median_gap_s": statistics.median(gaps) if gaps else None,
                "received": dict(recv)}


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ----------------------------------------------------------------- 发射平台
def launchpad_new_tokens(source, hours=6, limit=50, chain=None):
    lp = LAUNCHPADS[source]
    evm = Evm(chain or lp["chain"])
    head = evm.rpc.block()
    if not head:
        raise SystemExit("RPC 不可用，检查 WH_RPC_* 环境变量")
    per_hour = 1200                      # BSC 3s/块 → 1200 块/小时
    start = max(0, head - hours * per_hour)
    logs = evm.scan_logs(lp["contract"], topic(lp["created_sig"]), start, want=limit * 3, span=900)
    rows, seen = [], set()
    for lg in logs:
        token = _addr_word(lg.get("data") or "", lp["created_token_word"])
        creator = _addr_word(lg.get("data") or "", 0)
        if not token or token in seen:
            continue
        seen.add(token)
        rows.append({"token": token, "creator": creator,
                     "block": int(lg["blockNumber"], 16), "tx": lg.get("transactionHash")})
    rows.sort(key=lambda x: -x["block"])
    return rows[:limit]


def launchpad_creation_block(source, token, lookback=300_000, chunk=5_000):
    """在 TokenCreate 事件里找到这个币的创建区块（同时验证它确实来自该平台）。"""
    lp = LAUNCHPADS[source]
    evm = Evm(chain=lp["chain"])
    head = evm.rpc.block()
    t = token.lower()
    blk = head
    while blk > max(0, head - lookback):
        frm = max(0, blk - chunk)
        logs = evm.rpc.logs(lp["contract"], [topic(lp["created_sig"])], frm, blk) or []
        for lg in logs:
            if _addr_word(lg.get("data") or "", lp["created_token_word"]) == t:
                return int(lg["blockNumber"], 16)
        blk = frm
        time.sleep(0.2)
    return None


def launchpad_activity(source, days=1, max_logs=15000, cache_dir="out"):
    """免 key 的活跃度来源：直接看发射平台最近 N 天的买入/卖出事件。
    返回 {account: {"buys": [(token, block)], "deals": n}}；结果按天缓存到磁盘。"""
    lp = LAUNCHPADS[source]
    evm = Evm(chain=lp["chain"])
    head = evm.rpc.block()
    span_blocks = int(days * 28800)              # BSC 3s/块 → 28800 块/天
    start = max(0, head - span_blocks)
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"activity-{source}-{head // 28800}-{int(days * 24)}h.json")
    if os.path.exists(cache):
        d = json.load(open(cache, encoding="utf-8"))
        return {k: {"buys": [(t, b) for t, b in v["buys"]], "deals": v["deals"]}
                for k, v in d["act"].items()}, d["start"], d["head"]

    act = defaultdict(lambda: {"buys": [], "deals": 0})
    for sig_key in ("buy_sig", "sell_sig"):
        sig = lp.get(sig_key)
        if not sig:
            continue
        blk, span, total, chunks = start, 600, 0, 0
        while blk <= head and total < max_logs:
            logs = evm.rpc.logs(lp["contract"], [topic(sig)], blk, min(blk + span, head))
            if logs is None:
                span = max(120, span // 2)
                continue
            total += len(logs)
            chunks += 1
            if chunks % 10 == 0:
                print(f"      … {sig_key} 已扫 {(blk - start) * 100 // max(1, head - start)}% ({total} 条)")
            for lg in logs:
                data = lg.get("data") or ""
                tok = _addr_word(data, lp["buy_token_word"])
                acct = _addr_word(data, lp["buy_account_word"])
                if not acct:
                    continue
                act[acct]["deals"] += 1
                if sig_key == "buy_sig":
                    act[acct]["buys"].append((tok, int(lg["blockNumber"], 16)))
            blk += span
    json.dump({"start": start, "head": head, "act": act},
              open(cache, "w", encoding="utf-8"), ensure_ascii=False, default=str)
    return act, start, head


def launchpad_first_buyers(source, token, n=20, chain=None, creation_block=None):
    """发射平台自己的买入事件里，直接拿最早买家（比解析 ERC20 Transfer 更准）。"""
    lp = LAUNCHPADS[source]
    evm = Evm(chain or lp["chain"])
    start = creation_block or launchpad_creation_block(source, token)
    if not start:
        return []
    head = evm.rpc.block()
    out, seen = [], set()
    blk, span = start, 3000
    while blk <= head and len(out) < n * 4:
        logs = evm.rpc.logs(lp["contract"], [topic(lp["buy_sig"])], blk, min(blk + span, head))
        if logs is None:
            span = max(200, span // 2)
            continue
        for lg in logs:
            data = lg.get("data") or ""
            tok = _addr_word(data, lp["buy_token_word"])
            acct = _addr_word(data, lp["buy_account_word"])
            if tok != token.lower() or not acct or acct in seen:
                continue
            seen.add(acct)
            out.append({"address": acct, "block": int(lg["blockNumber"], 16)})
        blk += span
    out.sort(key=lambda x: x["block"])
    return out[:n]


# ----------------------------------------------------------------- scan
def cmd_scan(args):
    lp = LAUNCHPADS.get(args.launchpad) if args.launchpad else None
    chain = args.chain or (lp["chain"] if lp else "eth")
    evm = Evm(chain)
    ca = args.token.lower()

    print(f"① 目标币 {args.symbol or ca[:10]}  chain={chain}  ca={ca}")
    meta = evm.token_meta(ca)
    if meta["symbol"]:
        print(f"   现市值 ${meta['mcap']:,.0f} / 持有人 {meta['holders']:,} / {meta['name']}")
    print("   验证：市值和持有人应与行情软件对得上，否则合约地址给错了")

    if lp:
        buyers = launchpad_first_buyers(args.launchpad, ca, args.first, chain=chain)
        src = f"{lp['name']} 买入事件"
        start = buyers[0]["block"] if buyers else None
    else:
        start = evm.creation_block(ca, hint=args.creation_block)
        raw = evm.scan_logs(ca, TRANSFER_TOPIC, start, want=args.scan, span=1200) if start else []
        seen, buyers = set(), []
        for lg in raw:
            tps = lg.get("topics") or []
            if len(tps) < 3:
                continue
            to = "0x" + tps[2][-40:]
            if to.lower().startswith("0x00000000") or to.lower() in seen:
                continue
            seen.add(to.lower())
            buyers.append({"address": to, "block": int(lg["blockNumber"], 16)})
        buyers.sort(key=lambda x: x["block"])
        src = "Transfer 事件"
    if not buyers:
        print("X 没扫到早期买家：换 --launchpad / --creation-block，或加大 --scan")
        return 1
    print(f"② 部署区块 {start}；来源 {src}；最早 {len(buyers)} 个候选")

    rows = []
    act, a_head, a_start = None, None, None
    if lp:
        print(f"   取 {lp['name']} 近 {args.days} 天买卖事件做活跃度（免 key）")
        act, a_start, a_head = launchpad_activity(args.launchpad, days=args.days)
        print(f"   活跃窗口 {a_start}~{a_head}，覆盖 {len(act):,} 个地址")

    print("③④ 逐个核查")
    for b in buyers:
        if evm.rpc.is_contract(b["address"]):
            continue
        if act is not None:
            a = act.get(b["address"].lower(), {"buys": [], "deals": 0})
            deals = a["deals"]
            last_blk = max([blk for _, blk in a["buys"]], default=None)
            days_since = round((a_head - last_blk) * 3 / 86400, 1) if last_blk else None
            why = [f"{deals}笔/{args.days}天"] if deals / max(args.days, 1) > 100 else []
            p = {"address": b["address"], "days_since": days_since, "tx_30d": deals,
                 "token_transfers": 0, "native_txs": 0, "median_gap_s": None,
                 "last_active": f"block {last_blk}" if last_blk else None,
                 "received": {tok: {"symbol": "", "count": 1, "last": None}
                              for tok, _ in a["buys"] if tok}}
            p.update({"bot": why, "active30": deals > 0})
        else:
            p = evm.wallet_profile(b["address"])
            why = []
            if p["median_gap_s"] is not None and p["median_gap_s"] < BOT_GAP_S:
                why.append(f"间隔中位{round(p['median_gap_s'])}s")
            if p["tx_30d"] > BOT_TX30:
                why.append(f"30天{p['tx_30d']}笔")
            p.update({"bot": why, "active30": p["days_since"] is not None and p["days_since"] <= ACTIVE_DAYS})
        rows.append(p)
        if len(rows) >= args.first:
            break
        print(f"   {b['address'][:10]}… {'活跃' if p['active30'] else '休眠'} "
              f"{'距今' + str(p['days_since']) + '天 ' if p['days_since'] is not None else ''}"
              f"{'机器人(' + ','.join(why) + ')' if why else '人'}")

    alive = [r for r in rows if r["active30"] and not r["bot"]]
    print(f"⑤ 幸存 {len(alive)} 个（活跃 + 非机器人）")

    cache, cons = {}, defaultdict(lambda: {"wallets": set(), "last": None, "symbol": "",
                                           "senders": defaultdict(set)})
    for r in alive:
        for tk, d in r["received"].items():
            if tk not in cache:
                cache[tk] = evm.token_meta(tk)
                cache[tk]["ok"] = (cache[tk]["mcap"] >= MIN_MCAP
                                   or cache[tk]["holders"] >= MIN_HOLDERS)
            if not cache[tk]["ok"] or tk in STABLE_TOKENS:
                continue
            if act is None:
                if not d["last"] or (NOW - d["last"]).days > CONSENSUS_DAYS:
                    continue
            cons[tk]["wallets"].add(r["address"])
            cons[tk]["symbol"] = cache[tk]["symbol"] or d["symbol"]
            cons[tk]["last"] = d["last"]
            for sender in (d.get("senders") or {}):
                cons[tk]["senders"][sender].add(r["address"])

    print(f"⑥ 共识（近 {CONSENSUS_DAYS} 天，已滤垃圾币；空投/单源分发会被标出来）")
    kept = []
    for tk, d in sorted(cons.items(), key=lambda kv: -len(kv[1]["wallets"])):
        n = len(d["wallets"])
        if n < 2:
            continue
        top, ws = (None, set())
        if d["senders"]:
            top, ws = max(d["senders"].items(), key=lambda kv: len(kv[1]))
        airdrop = len(ws) >= 3 and len(ws) / n >= 0.6
        tag = f"空投/单源分发 → 剔除（同一发送方 {top[:10]}… 给了 {len(ws)} 个钱包）" if airdrop else "真买入"
        mark = "★" if (n >= 3 and not airdrop) else ""
        print(f"   {d['symbol']:10s} {tk} {n} 个钱包 {mark} {tag}")
        if not airdrop:
            kept.append({"symbol": d["symbol"], "token": tk, "wallets": sorted(d["wallets"])})
    if not kept:
        print("   无：没有 ≥2 个钱包在同期真买入同一个币")

    result = {"version": VERSION, "chain": chain, "token": ca, "meta": meta,
              "source": src, "first_block": start, "wallets": rows,
              "consensus": kept}
    out_dir = args.out or os.environ.get("WH_OUT") or "out"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"scan-{meta['symbol'] or ca[:8]}-{datetime.now():%Y%m%d-%H%M}.json")
    json.dump(result, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"\n报告：{path}")
    return 0


def cmd_launchpad(args):
    rows = launchpad_new_tokens(args.source, hours=args.hours, limit=args.limit, chain=args.chain)
    print(f"{LAUNCHPADS[args.source]['name']} 最近 {args.hours} 小时新币（{len(rows)} 个）")
    for r in rows:
        print(f"  {r['token']}  block {r['block']}  creator {r['creator']}")
    if args.out:
        json.dump(rows, open(args.out, "w", encoding="utf-8"), indent=1)
    return 0


def cmd_doctor(args):
    print(f"wallet-hunter {VERSION}  python {sys.version.split()[0]}")
    for name, cfg in CHAINS.items():
        rpc_ok = bs_ok = "-"
        r = Rpc(cfg["rpc"])
        h = r.block()
        rpc_ok = f"block {h}" if h else "FAIL"
        if cfg["blockscout"]:
            d = http_json(cfg["blockscout"] + "/api/v2/stats")
            bs_ok = "OK" if d else "FAIL"
        print(f"  {name:4s} rpc={rpc_ok:12s} blockscout={bs_ok}")
    for k, v in LAUNCHPADS.items():
        print(f"  launchpad {k}: {v['name']} on {v['chain']} {v['contract']}")
    print("\n提示：RPC 慢/限流时设 WH_RPC_ETH / WH_RPC_BSC 换成自己的节点")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="wallet_hunter", description="链上早期买家 / 聪明钱共识扫描")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="体检数据源")
    d.set_defaults(func=cmd_doctor)

    lp = sub.add_parser("launchpad", help="列出发射平台新币")
    lp.add_argument("--source", default="fourmeme", choices=list(LAUNCHPADS))
    lp.add_argument("--hours", type=int, default=6)
    lp.add_argument("--limit", type=int, default=50)
    lp.add_argument("--chain")
    lp.add_argument("--out")
    lp.set_defaults(func=cmd_launchpad)

    sc = sub.add_parser("scan", help="对一个币跑 7 步")
    sc.add_argument("--token", required=True)
    sc.add_argument("--chain", choices=list(CHAINS))
    sc.add_argument("--launchpad", choices=list(LAUNCHPADS))
    sc.add_argument("--first", type=int, default=20)
    sc.add_argument("--scan", type=int, default=120, help="扫多少个候选地址")
    sc.add_argument("--creation-block", type=int)
    sc.add_argument("--days", type=float, default=1.0, help="发射平台模式下，活跃度回看天数（可小数）")
    sc.add_argument("--symbol")
    sc.add_argument("--out")
    sc.set_defaults(func=cmd_scan)

    a = ap.parse_args()
    sys.exit(a.func(a) or 0)


if __name__ == "__main__":
    main()
