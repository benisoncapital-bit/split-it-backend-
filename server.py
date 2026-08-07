from fastapi import FastAPI, APIRouter, HTTPException, Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import certifi
import os
import io
import csv
import logging
import uuid
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime, timezone
from collections import defaultdict

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url, tlsCAFile=certifi.where())
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ---------- Utility ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

CATEGORIES = [
    {"key": "groceries", "label": "Groceries", "icon": "cart", "color": "#285943"},
    {"key": "dining", "label": "Dining Out", "icon": "restaurant", "color": "#DDA74F"},
    {"key": "utilities", "label": "Utilities", "icon": "bulb", "color": "#4A6966"},
    {"key": "rent", "label": "Rent", "icon": "home", "color": "#0A3B24"},
    {"key": "transport", "label": "Transport", "icon": "car", "color": "#175939"},
    {"key": "entertainment", "label": "Entertainment", "icon": "film", "color": "#D07D6E"},
    {"key": "travel", "label": "Travel", "icon": "airplane", "color": "#2E5D6E"},
    {"key": "shopping", "label": "Shopping", "icon": "bag-handle", "color": "#6C4A6E"},
    {"key": "health", "label": "Health", "icon": "medkit", "color": "#8A5A44"},
    {"key": "other", "label": "Other", "icon": "ellipsis-horizontal", "color": "#5C605C"},
]

CURRENCIES = [
    {"code": "INR", "symbol": "₹"},
    {"code": "USD", "symbol": "$"},
    {"code": "EUR", "symbol": "€"},
    {"code": "GBP", "symbol": "£"},
    {"code": "JPY", "symbol": "¥"},
    {"code": "AED", "symbol": "د.إ"},
    {"code": "AUD", "symbol": "A$"},
    {"code": "CAD", "symbol": "C$"},
    {"code": "SGD", "symbol": "S$"},
]

MEMBER_COLORS = ["#0A3B24", "#DDA74F", "#4A6966", "#D07D6E", "#175939", "#6C4A6E", "#8A5A44", "#2E5D6E"]

# ---------- Models ----------
class Member(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    email: Optional[str] = None
    color: str = MEMBER_COLORS[0]

class Group(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    emoji: str = "💸"
    cover: Optional[str] = None  # url key
    currency: str = "INR"
    members: List[Member] = []
    created_at: str = Field(default_factory=now_iso)

class GroupCreate(BaseModel):
    name: str
    emoji: Optional[str] = "💸"
    cover: Optional[str] = None
    currency: Optional[str] = "INR"
    member_names: List[str] = []

class GroupUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    cover: Optional[str] = None
    currency: Optional[str] = None

class MemberCreate(BaseModel):
    name: str
    email: Optional[str] = None

class Expense(BaseModel):
    id: str = Field(default_factory=new_id)
    group_id: str
    description: str
    category: str = "other"
    amount: float
    paid_by: Dict[str, float]  # {member_id: amount_paid}
    split_type: str = "equal"  # "equal" | "exact" | "percent"
    split_among: List[str] = []  # member ids participating
    shares: Dict[str, float] = {}  # {member_id: amount_owed}. If empty, split_among is equal-shared.
    date: str = Field(default_factory=now_iso)
    receipt_base64: Optional[str] = None
    note: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)

class ExpenseCreate(BaseModel):
    description: str
    category: str = "other"
    amount: float
    paid_by: Dict[str, float]
    split_type: str = "equal"
    split_among: List[str] = []
    shares: Dict[str, float] = {}
    date: Optional[str] = None
    receipt_base64: Optional[str] = None
    note: Optional[str] = None

class ExpenseUpdate(BaseModel):
    description: Optional[str] = None
    category: Optional[str] = None
    amount: Optional[float] = None
    paid_by: Optional[Dict[str, float]] = None
    split_type: Optional[str] = None
    split_among: Optional[List[str]] = None
    shares: Optional[Dict[str, float]] = None
    date: Optional[str] = None
    receipt_base64: Optional[str] = None
    note: Optional[str] = None

class Settlement(BaseModel):
    id: str = Field(default_factory=new_id)
    group_id: str
    payer_id: str
    payee_id: str
    amount: float
    date: str = Field(default_factory=now_iso)
    note: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)

class SettlementCreate(BaseModel):
    payer_id: str
    payee_id: str
    amount: float
    date: Optional[str] = None
    note: Optional[str] = None

# ---------- Helpers ----------
PROJECT = {"_id": 0}

def _normalize_paid_by(e: dict) -> dict:
    """Ensure paid_by is a dict {member_id: amount}. Legacy string form is converted."""
    pb = e.get("paid_by")
    if isinstance(pb, str):
        e["paid_by"] = {pb: float(e.get("amount", 0))}
    elif isinstance(pb, dict):
        e["paid_by"] = {k: float(v) for k, v in pb.items()}
    else:
        e["paid_by"] = {}
    return e

async def _get_group(group_id: str) -> dict:
    g = await db.groups.find_one({"id": group_id}, PROJECT)
    if not g:
        raise HTTPException(404, "Group not found")
    return g

def _round(v: float) -> float:
    return round(v + 1e-9, 2)

def _compute_balances(group: dict, expenses: List[dict], settlements: List[dict]) -> Dict[str, dict]:
    """Return per-member stats: total_paid, total_consumed, settled_paid, settled_received, net_balance."""
    stats: Dict[str, dict] = {}
    for m in group.get("members", []):
        stats[m["id"]] = {
            "member_id": m["id"],
            "name": m["name"],
            "color": m.get("color", "#0A3B24"),
            "total_paid": 0.0,
            "total_consumed": 0.0,
            "settled_paid": 0.0,
            "settled_received": 0.0,
            "net_balance": 0.0,
        }
    for e in expenses:
        amount = float(e["amount"])
        pb = e.get("paid_by") or {}
        if isinstance(pb, str):
            pb = {pb: amount}
        for pid, paid_amt in pb.items():
            if pid in stats:
                stats[pid]["total_paid"] += float(paid_amt)
        shares = e.get("shares") or {}
        if isinstance(shares, dict) and shares:
            for sid, amt in shares.items():
                if sid in stats:
                    stats[sid]["total_consumed"] += float(amt)
        else:
            splitters = e.get("split_among") or []
            if splitters:
                share = amount / len(splitters)
                for sid in splitters:
                    if sid in stats:
                        stats[sid]["total_consumed"] += share
    for s in settlements:
        if s["payer_id"] in stats:
            stats[s["payer_id"]]["settled_paid"] += float(s["amount"])
        if s["payee_id"] in stats:
            stats[s["payee_id"]]["settled_received"] += float(s["amount"])
    for mid, v in stats.items():
        # net = paid - consumed + settled_received? No:
        # Positive net = they are owed (paid more than consumed).
        # Settling: when a payer sends money to payee, payer reduces their debt (their net was negative → becomes less negative)
        # so settled_paid effectively acts like paid; settled_received acts like receiving money i.e. reduces credit.
        v["net_balance"] = _round(v["total_paid"] - v["total_consumed"] + v["settled_paid"] - v["settled_received"])
        v["total_paid"] = _round(v["total_paid"])
        v["total_consumed"] = _round(v["total_consumed"])
        v["settled_paid"] = _round(v["settled_paid"])
        v["settled_received"] = _round(v["settled_received"])
    return stats

def _settle_plan(stats: Dict[str, dict]) -> List[dict]:
    """Min-transactions greedy: pair largest debtor with largest creditor."""
    creditors = []  # (net, id)
    debtors = []
    for mid, v in stats.items():
        n = v["net_balance"]
        if n > 0.01:
            creditors.append([n, mid])
        elif n < -0.01:
            debtors.append([-n, mid])
    creditors.sort(reverse=True)
    debtors.sort(reverse=True)
    plan = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][0], creditors[j][0])
        plan.append({
            "payer_id": debtors[i][1],
            "payee_id": creditors[j][1],
            "amount": _round(pay),
        })
        debtors[i][0] -= pay
        creditors[j][0] -= pay
        if debtors[i][0] < 0.01:
            i += 1
        if creditors[j][0] < 0.01:
            j += 1
    return plan

# ---------- Routes: Meta ----------
@api_router.get("/")
async def root():
    return {"app": "Split It", "status": "ok"}

@api_router.get("/health")
async def health():
    """Real connectivity proof: actually pings MongoDB rather than assuming
    the client object being non-null means the DB is reachable. Motor/Mongo
    clients connect lazily, so `client = AsyncIOMotorClient(...)` succeeds
    even with a wrong URL — nothing fails until the first real command."""
    try:
        await client.admin.command("ping")
        db_status = "connected"
    except Exception as e:
        db_status = f"unreachable: {e}"
    return {"app": "Split It", "api": "ok", "database": db_status}

@api_router.get("/meta")
async def meta():
    return {"categories": CATEGORIES, "currencies": CURRENCIES, "member_colors": MEMBER_COLORS}

# ---------- Routes: Groups ----------
@api_router.get("/groups")
async def list_groups():
    groups = await db.groups.find({}, PROJECT).sort("created_at", -1).to_list(500)
    # Attach quick stats
    out = []
    for g in groups:
        exps = await db.expenses.find({"group_id": g["id"]}, PROJECT).to_list(2000)
        exps = [_normalize_paid_by(e) for e in exps]
        setts = await db.settlements.find({"group_id": g["id"]}, PROJECT).to_list(2000)
        total = _round(sum(float(e["amount"]) for e in exps))
        stats = _compute_balances(g, exps, setts)
        # your balance = sum of positive nets? Show total unsettled magnitude
        unsettled = _round(sum(abs(v["net_balance"]) for v in stats.values()) / 2)
        g["stats"] = {
            "total_expenses": total,
            "expense_count": len(exps),
            "member_count": len(g.get("members", [])),
            "unsettled": unsettled,
        }
        out.append(g)
    return out

@api_router.post("/groups")
async def create_group(payload: GroupCreate):
    if not payload.name or not payload.name.strip():
        raise HTTPException(400, "Group name is required")
    members = []
    for i, name in enumerate(payload.member_names or []):
        n = name.strip()
        if not n:
            continue
        members.append(Member(name=n, color=MEMBER_COLORS[i % len(MEMBER_COLORS)]).dict())
    if len(members) < 2:
        raise HTTPException(400, "A group needs at least 2 members")
    g = Group(
        name=payload.name.strip(),
        emoji=payload.emoji or "💸",
        cover=payload.cover,
        currency=payload.currency or "INR",
        members=members,
    ).dict()
    await db.groups.insert_one(g)
    g.pop("_id", None)
    return g

@api_router.get("/groups/{group_id}")
async def get_group(group_id: str):
    return await _get_group(group_id)

@api_router.patch("/groups/{group_id}")
async def update_group(group_id: str, payload: GroupUpdate):
    g = await _get_group(group_id)
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    if updates:
        await db.groups.update_one({"id": group_id}, {"$set": updates})
    return await _get_group(group_id)

@api_router.delete("/groups/{group_id}")
async def delete_group(group_id: str):
    await _get_group(group_id)
    await db.groups.delete_one({"id": group_id})
    await db.expenses.delete_many({"group_id": group_id})
    await db.settlements.delete_many({"group_id": group_id})
    return {"ok": True}

# ---------- Routes: Members ----------
@api_router.post("/groups/{group_id}/members")
async def add_member(group_id: str, payload: MemberCreate):
    g = await _get_group(group_id)
    idx = len(g.get("members", []))
    m = Member(name=payload.name.strip(), email=payload.email, color=MEMBER_COLORS[idx % len(MEMBER_COLORS)]).dict()
    await db.groups.update_one({"id": group_id}, {"$push": {"members": m}})
    return m

@api_router.delete("/groups/{group_id}/members/{member_id}")
async def delete_member(group_id: str, member_id: str):
    g = await _get_group(group_id)
    # Prevent deletion if member has expenses/settlements
    used = await db.expenses.find_one({"group_id": group_id, "$or": [
        {"paid_by": member_id},
        {f"paid_by.{member_id}": {"$exists": True}},
        {"split_among": member_id},
    ]})
    if used:
        raise HTTPException(400, "Cannot remove member with existing expenses. Delete their expenses first.")
    used_s = await db.settlements.find_one({"group_id": group_id, "$or": [{"payer_id": member_id}, {"payee_id": member_id}]})
    if used_s:
        raise HTTPException(400, "Cannot remove member with settlement history.")
    await db.groups.update_one({"id": group_id}, {"$pull": {"members": {"id": member_id}}})
    return {"ok": True}

# ---------- Routes: Expenses ----------
@api_router.get("/groups/{group_id}/expenses")
async def list_expenses(group_id: str):
    await _get_group(group_id)
    exps = await db.expenses.find({"group_id": group_id}, PROJECT).sort("date", -1).to_list(5000)
    return [_normalize_paid_by(e) for e in exps]

def _validate_expense_payload(g: dict, description: str, amount: float, paid_by: dict, split_type: str, split_among: List[str], shares: dict) -> tuple:
    member_ids = {m["id"] for m in g.get("members", [])}
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    if not paid_by:
        raise HTTPException(400, "paid_by cannot be empty")
    for pid, amt in paid_by.items():
        if pid not in member_ids:
            raise HTTPException(400, f"paid_by contains unknown member {pid}")
        if float(amt) < 0:
            raise HTTPException(400, "paid_by amounts must be >= 0")
    paid_sum = sum(float(v) for v in paid_by.values())
    if abs(paid_sum - float(amount)) > 0.01:
        raise HTTPException(400, f"Sum of paid_by amounts ({paid_sum:.2f}) must equal total amount ({amount:.2f})")
    clean_paid = {k: float(v) for k, v in paid_by.items() if float(v) > 0}

    st = (split_type or "equal").lower()
    if st not in ("equal", "exact", "percent"):
        raise HTTPException(400, "split_type must be one of: equal, exact, percent")

    clean_shares: Dict[str, float] = {}
    clean_split_among: List[str] = list(split_among or [])

    if st == "equal":
        if not clean_split_among:
            raise HTTPException(400, "split_among cannot be empty for equal split")
        for s in clean_split_among:
            if s not in member_ids:
                raise HTTPException(400, f"split_among contains unknown member {s}")
        # Compute shares for storage so balances read cleanly
        per = float(amount) / len(clean_split_among)
        rounded = round(per + 1e-9, 2)
        remainder = round(float(amount) - rounded * len(clean_split_among), 2)
        for i, sid in enumerate(clean_split_among):
            clean_shares[sid] = round(rounded + (remainder if i == 0 else 0), 2)
    elif st == "exact":
        if not shares:
            raise HTTPException(400, "shares are required for exact split")
        for sid, v in shares.items():
            if sid not in member_ids:
                raise HTTPException(400, f"shares contains unknown member {sid}")
            if float(v) < 0:
                raise HTTPException(400, "share amounts must be >= 0")
        share_sum = sum(float(v) for v in shares.values())
        if abs(share_sum - float(amount)) > 0.01:
            raise HTTPException(400, f"Sum of shares ({share_sum:.2f}) must equal total amount ({amount:.2f})")
        clean_shares = {k: float(v) for k, v in shares.items() if float(v) > 0}
        clean_split_among = list(clean_shares.keys())
    else:  # percent
        if not shares:
            raise HTTPException(400, "shares (percentages) are required for percent split")
        for sid, v in shares.items():
            if sid not in member_ids:
                raise HTTPException(400, f"shares contains unknown member {sid}")
            if float(v) < 0:
                raise HTTPException(400, "percentages must be >= 0")
        pct_sum = sum(float(v) for v in shares.values())
        if abs(pct_sum - 100.0) > 0.01:
            raise HTTPException(400, f"Percentages must add up to 100 (got {pct_sum:.2f})")
        # Convert percent to amounts
        for sid, v in shares.items():
            if float(v) > 0:
                clean_shares[sid] = round(float(amount) * float(v) / 100.0, 2)
        # Fix rounding drift on the largest share
        drift = round(float(amount) - sum(clean_shares.values()), 2)
        if abs(drift) >= 0.01 and clean_shares:
            top = max(clean_shares, key=clean_shares.get)
            clean_shares[top] = round(clean_shares[top] + drift, 2)
        clean_split_among = list(clean_shares.keys())

    return clean_paid, st, clean_split_among, clean_shares


@api_router.post("/groups/{group_id}/expenses")
async def create_expense(group_id: str, payload: ExpenseCreate):
    g = await _get_group(group_id)
    clean_paid, st, clean_split_among, clean_shares = _validate_expense_payload(
        g, payload.description, payload.amount, payload.paid_by,
        payload.split_type, payload.split_among, payload.shares,
    )
    e = Expense(
        group_id=group_id,
        description=payload.description.strip() or "Expense",
        category=payload.category or "other",
        amount=float(payload.amount),
        paid_by=clean_paid,
        split_type=st,
        split_among=clean_split_among,
        shares=clean_shares,
        date=payload.date or now_iso(),
        receipt_base64=payload.receipt_base64,
        note=payload.note,
    ).dict()
    await db.expenses.insert_one(e)
    e.pop("_id", None)
    return e

@api_router.get("/groups/{group_id}/expenses/{expense_id}")
async def get_expense(group_id: str, expense_id: str):
    e = await db.expenses.find_one({"id": expense_id, "group_id": group_id}, PROJECT)
    if not e:
        raise HTTPException(404, "Expense not found")
    return _normalize_paid_by(e)

@api_router.patch("/groups/{group_id}/expenses/{expense_id}")
async def update_expense(group_id: str, expense_id: str, payload: ExpenseUpdate):
    e = await db.expenses.find_one({"id": expense_id, "group_id": group_id}, PROJECT)
    if not e:
        raise HTTPException(404, "Expense not found")
    g = await _get_group(group_id)
    e = _normalize_paid_by(e)

    updates: Dict[str, object] = {}
    # Simple fields
    for f in ("description", "category", "date", "receipt_base64", "note"):
        v = getattr(payload, f)
        if v is not None:
            updates[f] = v.strip() if f in ("description",) and isinstance(v, str) else v

    # Financial fields — revalidate together
    touching_finance = any(getattr(payload, f) is not None for f in ("amount", "paid_by", "split_type", "split_among", "shares"))
    if touching_finance:
        amount = float(payload.amount if payload.amount is not None else e["amount"])
        paid_by = payload.paid_by if payload.paid_by is not None else e.get("paid_by") or {}
        split_type = payload.split_type if payload.split_type is not None else (e.get("split_type") or "equal")
        split_among = payload.split_among if payload.split_among is not None else (e.get("split_among") or list((e.get("shares") or {}).keys()))
        shares = payload.shares if payload.shares is not None else (e.get("shares") or {})
        description = payload.description if payload.description is not None else e.get("description", "")
        clean_paid, st, clean_split_among, clean_shares = _validate_expense_payload(
            g, description, amount, paid_by, split_type, split_among, shares,
        )
        updates.update({
            "amount": amount,
            "paid_by": clean_paid,
            "split_type": st,
            "split_among": clean_split_among,
            "shares": clean_shares,
        })

    if updates:
        await db.expenses.update_one({"id": expense_id}, {"$set": updates})
    return _normalize_paid_by(await db.expenses.find_one({"id": expense_id}, PROJECT))

@api_router.delete("/groups/{group_id}/expenses/{expense_id}")
async def delete_expense(group_id: str, expense_id: str):
    r = await db.expenses.delete_one({"id": expense_id, "group_id": group_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "Expense not found")
    return {"ok": True}

# ---------- Routes: Settlements ----------
@api_router.get("/groups/{group_id}/settlements")
async def list_settlements(group_id: str):
    await _get_group(group_id)
    setts = await db.settlements.find({"group_id": group_id}, PROJECT).sort("date", -1).to_list(2000)
    return setts

@api_router.post("/groups/{group_id}/settlements")
async def create_settlement(group_id: str, payload: SettlementCreate):
    g = await _get_group(group_id)
    ids = {m["id"] for m in g.get("members", [])}
    if payload.payer_id not in ids or payload.payee_id not in ids:
        raise HTTPException(400, "Members must belong to the group")
    if payload.payer_id == payload.payee_id:
        raise HTTPException(400, "Payer and payee must differ")
    if payload.amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    s = Settlement(
        group_id=group_id,
        payer_id=payload.payer_id,
        payee_id=payload.payee_id,
        amount=float(payload.amount),
        date=payload.date or now_iso(),
        note=payload.note,
    ).dict()
    await db.settlements.insert_one(s)
    s.pop("_id", None)
    return s

@api_router.delete("/groups/{group_id}/settlements/{settlement_id}")
async def delete_settlement(group_id: str, settlement_id: str):
    r = await db.settlements.delete_one({"id": settlement_id, "group_id": group_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------- Routes: Summary ----------
@api_router.get("/groups/{group_id}/summary")
async def group_summary(group_id: str):
    g = await _get_group(group_id)
    exps = await db.expenses.find({"group_id": group_id}, PROJECT).to_list(5000)
    exps = [_normalize_paid_by(e) for e in exps]
    setts = await db.settlements.find({"group_id": group_id}, PROJECT).to_list(2000)

    stats = _compute_balances(g, exps, setts)
    total_expenses = _round(sum(float(e["amount"]) for e in exps))
    total_settled = _round(sum(float(s["amount"]) for s in setts))

    # Category breakdown
    cat_totals: Dict[str, float] = defaultdict(float)
    for e in exps:
        cat_totals[e.get("category", "other")] += float(e["amount"])
    category_breakdown = [
        {"category": k, "amount": _round(v)} for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])
    ]

    # Monthly trend (last 6 months)
    month_totals: Dict[str, float] = defaultdict(float)
    for e in exps:
        try:
            d = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        key = d.strftime("%Y-%m")
        month_totals[key] += float(e["amount"])
    monthly_trend = [{"month": k, "amount": _round(v)} for k, v in sorted(month_totals.items())][-6:]

    # Settle plan
    plan = _settle_plan(stats)

    # Recent activity: mix expenses + settlements sorted desc, top 20
    activity: List[dict] = []
    for e in exps:
        activity.append({"type": "expense", "date": e["date"], "data": e})
    for s in setts:
        activity.append({"type": "settlement", "date": s["date"], "data": s})
    activity.sort(key=lambda x: x["date"], reverse=True)
    recent = activity[:20]

    unsettled_total = _round(sum(abs(v["net_balance"]) for v in stats.values()) / 2)

    return {
        "group": g,
        "totals": {
            "total_expenses": total_expenses,
            "total_settled": total_settled,
            "unsettled": unsettled_total,
            "expense_count": len(exps),
        },
        "balances": list(stats.values()),
        "category_breakdown": category_breakdown,
        "monthly_trend": monthly_trend,
        "settle_plan": plan,
        "recent_activity": recent,
    }

@api_router.get("/groups/{group_id}/export")
async def export_csv(group_id: str):
    g = await _get_group(group_id)
    exps = await db.expenses.find({"group_id": group_id}, PROJECT).sort("date", 1).to_list(5000)
    exps = [_normalize_paid_by(e) for e in exps]
    setts = await db.settlements.find({"group_id": group_id}, PROJECT).sort("date", 1).to_list(2000)
    id_to_name = {m["id"]: m["name"] for m in g.get("members", [])}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Type", "Date", "Description/Note", "Category", "Amount", "Paid By", "Split Among / Payee"])
    for e in exps:
        payer_str = ", ".join(f"{id_to_name.get(pid, '?')} ({amt:.2f})" for pid, amt in e.get("paid_by", {}).items())
        writer.writerow([
            "Expense", e["date"], e.get("description", ""), e.get("category", ""),
            f"{e['amount']:.2f}", payer_str,
            ", ".join(id_to_name.get(x, "?") for x in e.get("split_among", []))
        ])
    for s in setts:
        writer.writerow([
            "Settlement", s["date"], s.get("note", "") or "Settled up", "",
            f"{s['amount']:.2f}", id_to_name.get(s["payer_id"], "?"),
            id_to_name.get(s["payee_id"], "?")
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{g["name"]}-export.csv"'},
    )

# ---------- Global insights ----------
@api_router.get("/activity")
async def global_activity():
    groups = await db.groups.find({}, PROJECT).to_list(500)
    id_to_group = {g["id"]: g for g in groups}
    exps = await db.expenses.find({}, PROJECT).sort("date", -1).to_list(200)
    exps = [_normalize_paid_by(e) for e in exps]
    setts = await db.settlements.find({}, PROJECT).sort("date", -1).to_list(200)
    items = []
    for e in exps:
        g = id_to_group.get(e["group_id"])
        if not g:
            continue
        items.append({"type": "expense", "date": e["date"], "group": {"id": g["id"], "name": g["name"], "emoji": g.get("emoji"), "currency": g.get("currency", "INR")}, "data": e, "members": g.get("members", [])})
    for s in setts:
        g = id_to_group.get(s["group_id"])
        if not g:
            continue
        items.append({"type": "settlement", "date": s["date"], "group": {"id": g["id"], "name": g["name"], "emoji": g.get("emoji"), "currency": g.get("currency", "INR")}, "data": s, "members": g.get("members", [])})
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:100]

@api_router.get("/insights")
async def global_insights():
    groups = await db.groups.find({}, PROJECT).to_list(500)
    exps = await db.expenses.find({}, PROJECT).to_list(5000)
    total = _round(sum(float(e["amount"]) for e in exps))
    cat_totals: Dict[str, float] = defaultdict(float)
    month_totals: Dict[str, float] = defaultdict(float)
    for e in exps:
        cat_totals[e.get("category", "other")] += float(e["amount"])
        try:
            d = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            month_totals[d.strftime("%Y-%m")] += float(e["amount"])
        except Exception:
            pass
    return {
        "total_expenses": total,
        "group_count": len(groups),
        "expense_count": len(exps),
        "category_breakdown": [{"category": k, "amount": _round(v)} for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])],
        "monthly_trend": [{"month": k, "amount": _round(v)} for k, v in sorted(month_totals.items())][-6:],
    }

# ---------- App wiring ----------
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
