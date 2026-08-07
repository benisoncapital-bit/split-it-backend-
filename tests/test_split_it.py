"""Backend tests for Split It — expenses w/ multi-payer, edit, and unequal splits."""
import os
import re
import pytest
import requests

BASE = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://shared-expenses-50.preview.emergentagent.com").rstrip("/")
API = f"{BASE}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(scope="module")
def group(s):
    """Create a fresh test group with 3 members."""
    r = s.post(f"{API}/groups", json={
        "name": "TEST_SplitIt_Suite",
        "emoji": "🧪",
        "currency": "INR",
        "member_names": ["TEST_A", "TEST_B", "TEST_C"],
    })
    assert r.status_code == 200, r.text
    g = r.json()
    assert "_id" not in g
    assert len(g["members"]) == 3
    yield g
    s.delete(f"{API}/groups/{g['id']}")


# ---------- Health & meta ----------

def test_root(s):
    r = s.get(f"{API}/")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_no_mongo_id_in_groups_listing(s):
    r = s.get(f"{API}/groups")
    assert r.status_code == 200
    for g in r.json():
        assert "_id" not in g


# ---------- Equal split (baseline + multi-payer) ----------

def test_equal_split_single_payer(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_Pizza",
        "category": "dining",
        "amount": 300,
        "paid_by": {m[0]["id"]: 300},
        "split_type": "equal",
        "split_among": [x["id"] for x in m],
    })
    assert r.status_code == 200, r.text
    e = r.json()
    assert "_id" not in e
    assert e["split_type"] == "equal"
    assert abs(sum(e["shares"].values()) - 300) < 0.01
    assert len(e["shares"]) == 3


def test_multi_payer_sum_mismatch_400(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_BadPayer",
        "amount": 100,
        "paid_by": {m[0]["id"]: 40, m[1]["id"]: 40},  # sums 80, not 100
        "split_type": "equal",
        "split_among": [x["id"] for x in m],
    })
    assert r.status_code == 400
    assert "paid_by" in r.text.lower() or "sum" in r.text.lower()


def test_multi_payer_ok(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_MultiPay",
        "amount": 200,
        "paid_by": {m[0]["id"]: 120, m[1]["id"]: 80},
        "split_type": "equal",
        "split_among": [x["id"] for x in m],
    })
    assert r.status_code == 200, r.text
    e = r.json()
    assert abs(sum(e["paid_by"].values()) - 200) < 0.01
    assert len(e["paid_by"]) == 2


# ---------- Exact split ----------

def test_exact_split_ok(s, group):
    m = group["members"]
    shares = {m[0]["id"]: 60, m[1]["id"]: 30, m[2]["id"]: 10}
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_ExactGood",
        "amount": 100,
        "paid_by": {m[0]["id"]: 100},
        "split_type": "exact",
        "shares": shares,
    })
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["split_type"] == "exact"
    for k, v in shares.items():
        assert abs(e["shares"][k] - v) < 0.01


def test_exact_split_mismatch_400(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_ExactBad",
        "amount": 100,
        "paid_by": {m[0]["id"]: 100},
        "split_type": "exact",
        "shares": {m[0]["id"]: 50, m[1]["id"]: 30},  # sums 80
    })
    assert r.status_code == 400
    assert "shares" in r.text.lower() or "sum" in r.text.lower()


# ---------- Percent split ----------

def test_percent_split_ok(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_PercentGood",
        "amount": 200,
        "paid_by": {m[0]["id"]: 200},
        "split_type": "percent",
        "shares": {m[0]["id"]: 50, m[1]["id"]: 30, m[2]["id"]: 20},
    })
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["split_type"] == "percent"
    # Percent converted to money amounts server-side
    assert abs(sum(e["shares"].values()) - 200) < 0.02
    assert abs(e["shares"][m[0]["id"]] - 100) < 0.02


def test_percent_split_not_100_400(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_PercentBad",
        "amount": 200,
        "paid_by": {m[0]["id"]: 200},
        "split_type": "percent",
        "shares": {m[0]["id"]: 50, m[1]["id"]: 30},  # sum 80%
    })
    assert r.status_code == 400
    assert "100" in r.text or "percent" in r.text.lower()


# ---------- GET single expense (for edit prefill) ----------

def test_get_single_expense(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_GetOne",
        "amount": 90,
        "paid_by": {m[0]["id"]: 90},
        "split_type": "equal",
        "split_among": [x["id"] for x in m],
    })
    eid = r.json()["id"]
    r2 = s.get(f"{API}/groups/{group['id']}/expenses/{eid}")
    assert r2.status_code == 200
    e = r2.json()
    assert "_id" not in e
    assert e["id"] == eid
    assert e["description"] == "TEST_GetOne"
    assert isinstance(e["paid_by"], dict)


# ---------- PATCH re-validates on financial change ----------

def test_patch_revalidates_and_persists(s, group):
    m = group["members"]
    # Create initial equal expense
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_Editable",
        "amount": 90,
        "paid_by": {m[0]["id"]: 90},
        "split_type": "equal",
        "split_among": [x["id"] for x in m],
    })
    eid = r.json()["id"]

    # Bad patch: amount 100 but paid_by still 90 → 400
    bad = s.patch(f"{API}/groups/{group['id']}/expenses/{eid}", json={"amount": 100})
    assert bad.status_code == 400

    # Good patch: switch to exact, matching sum
    good = s.patch(f"{API}/groups/{group['id']}/expenses/{eid}", json={
        "amount": 150,
        "paid_by": {m[1]["id"]: 150},
        "split_type": "exact",
        "shares": {m[0]["id"]: 100, m[1]["id"]: 50},
    })
    assert good.status_code == 200, good.text
    e2 = good.json()
    assert "_id" not in e2
    assert e2["split_type"] == "exact"
    assert abs(sum(e2["shares"].values()) - 150) < 0.01

    # Verify persistence via GET
    v = s.get(f"{API}/groups/{group['id']}/expenses/{eid}").json()
    assert v["amount"] == 150
    assert v["split_type"] == "exact"
    assert list(v["paid_by"].keys()) == [m[1]["id"]]


# ---------- Balances & summary sanity ----------

def test_summary_balances_sum_zero(s, group):
    # Mixed-mode expenses were created above; net balances should sum to 0
    r = s.get(f"{API}/groups/{group['id']}/summary")
    assert r.status_code == 200
    data = r.json()
    assert "_id" not in data["group"]
    net = sum(b["net_balance"] for b in data["balances"])
    assert abs(net) < 0.05, f"Net balance not zero: {net}"
    assert data["totals"]["expense_count"] >= 4


def test_summary_no_mongo_id_deep(s, group):
    r = s.get(f"{API}/groups/{group['id']}/summary")
    body = r.text
    # No literal "_id" anywhere
    assert not re.search(r'"_id"\s*:', body)


# ---------- Delete flow ----------

def test_delete_expense(s, group):
    m = group["members"]
    r = s.post(f"{API}/groups/{group['id']}/expenses", json={
        "description": "TEST_ToDelete",
        "amount": 10,
        "paid_by": {m[0]["id"]: 10},
        "split_type": "equal",
        "split_among": [x["id"] for x in m],
    })
    eid = r.json()["id"]
    d = s.delete(f"{API}/groups/{group['id']}/expenses/{eid}")
    assert d.status_code == 200
    g = s.get(f"{API}/groups/{group['id']}/expenses/{eid}")
    assert g.status_code == 404
