import os
import json
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from groq import Groq
from pymongo import MongoClient
from dateutil import parser
from fastapi import FastAPI
from pydantic import BaseModel

# =========================
# INIT
# =========================
load_dotenv()

app = FastAPI()

client_ai = Groq(api_key=os.getenv("GROQ_API_KEY"))

mongo_client = MongoClient(
    os.getenv("MONGO_URI"),
    serverSelectionTimeoutMS=5000
)

db = mongo_client["waelzyai"]
collection = db["wz_transactions"]

# =========================
# CACHE
# =========================
cached_data = []

def load_data():
    return list(collection.find({}, {"_id": 0}).limit(1000))

def normalize(tx):
    dt = None

    if isinstance(tx.get("txn_date"), datetime):
        dt = tx.get("txn_date")

    elif isinstance(tx.get("txn_date"), dict):
        raw = tx["txn_date"].get("$date")
        dt = parser.parse(raw) if raw else None

    elif isinstance(tx.get("date"), str):
        dt = parser.parse(tx.get("date"))

    if dt and dt.tzinfo:
        dt = dt.replace(tzinfo=None)

    amount = tx.get("amount", 0)
    if isinstance(amount, dict):
        amount = amount.get("value", 0)

    try:
        amount = float(amount)
    except:
        amount = 0

    if tx.get("type", "").upper() == "DEBIT":
        amount = -abs(amount)
    else:
        amount = abs(amount)

    balance = (
        tx.get("balance") if tx.get("balance") is not None
        else tx.get("running_balance") if tx.get("running_balance") is not None
        else tx.get("current_balance")
    )

    return {
        "date": dt,
        "amount": amount,
        "type": tx.get("type", "").lower(),
        "balance": balance
    }

def refresh_cache():
    global cached_data
    raw = load_data()
    cached_data = [normalize(tx) for tx in raw]

# =========================
# STARTUP
# =========================
@app.on_event("startup")
def startup():
    refresh_cache()

# =========================
# AI PARSER
# =========================
def parse_query_ai(query):
    try:
        response = client_ai.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{
                "role": "user",
                "content": f"""
Extract:
intent (balance/spent/received/transactions),
days,
month

Return JSON only.

Query: {query}
"""
            }],
            timeout=5
        )

        text = response.choices[0].message.content.strip()
        return json.loads(re.search(r"\{.*\}", text, re.DOTALL).group())

    except:
        return {"intent": "transactions", "days": None, "month": None}

# =========================
# DATE FILTER
# =========================
def get_date_range(parsed):
    today = datetime.now()

    if parsed.get("days"):
        return today - timedelta(days=int(parsed["days"])), today

    if parsed.get("month"):
        months = {
            "january":1,"february":2,"march":3,"april":4,
            "may":5,"june":6,"july":7,"august":8,
            "september":9,"october":10,"november":11,"december":12
        }

        m = months.get(parsed["month"].lower())
        if m:
            return datetime(today.year, m, 1), datetime(today.year, m % 12 + 1, 1)

    return None, None

def filter_data(data, start, end):
    if not start:
        return data
    return [tx for tx in data if tx["date"] and start <= tx["date"] <= end]

# =========================
# CHATBOT
# =========================
def chatbot(query):
    data = cached_data

    if not data:
        return "No data found"

    q = query.lower()

    if any(w in q for w in ["hi", "hello", "hey"]):
        return "Hey! Ask me about transactions, spent, or received."

    if not any(w in q for w in ["spent","received","credit","debit","transaction","balance","month","days","count"]):
        return "I can help with transactions, spent, or received."

    # BALANCE FROM DB
    if "balance" in q:
        latest = sorted(data, key=lambda x: x["date"] or datetime.min, reverse=True)
        if latest and latest[0]["balance"] is not None:
            return f"💰 Current Balance: {latest[0]['balance']}"
        return "Balance not available"

    parsed = parse_query_ai(query)
    intent = parsed.get("intent")

    start, end = get_date_range(parsed)
    filtered = filter_data(data, start, end)

    if not filtered:
        return "No transactions found"

    if "total transactions" in q or "count" in q:
        return f"📊 Total Transactions: {len(filtered)}"

    spent = abs(sum(tx["amount"] for tx in filtered if tx["amount"] < 0))
    received = sum(tx["amount"] for tx in filtered if tx["amount"] > 0)

    if intent == "spent":
        return f"💸 Spent: {round(spent,2)}"

    if intent == "received":
        return f"💰 Received: {round(received,2)}"

    # DEFAULT
    output = f"\nTransactions: {len(filtered)}\n\n"

    for tx in filtered[:20]:
        d = tx["date"].strftime("%Y-%m-%d") if tx["date"] else "N/A"
        output += f"{d} | {tx['type']} | {tx['amount']} | Bal: {tx['balance']}\n"

    return output

# =========================
# ROUTES
# =========================
class ChatRequest(BaseModel):
    query: str

@app.post("/chat")
def chat(req: ChatRequest):
    return {"response": chatbot(req.query)}

@app.get("/chat")
def chat_get(query: str):
    return {"response": chatbot(query)}

@app.get("/")
def home():
    return {"status": "running"}

def handler(request):
    return app