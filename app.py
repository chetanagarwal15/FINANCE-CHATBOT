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

client_ai = Groq(api_key=os.getenv("GROQ_API_KEY"))
mongo_client = MongoClient(os.getenv("MONGO_URI"))

db = mongo_client["waelzyai"]
collection = db["wz_transactions"]

app = FastAPI()

# =========================
# LOAD DATA
# =========================
def load_data():
    return list(collection.find({}, {"_id": 0}))

# =========================
# NORMALIZE
# =========================
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

    return {
        "date": dt,
        "amount": amount,
        "type": tx.get("type", "").lower(),
        "desc": tx.get("short_description") or tx.get("description", ""),
        "balance": tx.get("balance") or tx.get("running_balance") or tx.get("current_balance")
    }

# =========================
# AI PARSER (FIXED)
# =========================
def parse_query_ai(query):
    prompt = f"""
Extract:
- intent: balance / spent / received / transactions
- days: number if present
- month: month name if present

Return ONLY JSON:
{{
  "intent": "",
  "days": null,
  "month": null
}}

Query: {query}
"""

    try:
        response = client_ai.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            timeout=5
        )
        text = response.choices[0].message.content.strip()

        try:
            return json.loads(text)
        except:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            return json.loads(match.group())

    except Exception:
        return {"intent": "transactions", "days": None, "month": None}

# =========================
# DATE RANGE
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
            start = datetime(today.year, m, 1)
            end = datetime(today.year + (m // 12), (m % 12) + 1, 1)
            return start, end

    return None, None

# =========================
# FILTER
# =========================
def filter_data(data, start, end):
    if not start:
        return data
    return [tx for tx in data if tx["date"] and start <= tx["date"] <= end]

# =========================
# CHATBOT
# =========================
def chatbot(query):
    raw = load_data()
    if not raw:
        return "No data found in MongoDB"

    data = [normalize(tx) for tx in raw]
    q = query.lower()

    # greeting
    if any(w in q for w in ["hi", "hello", "hey"]):
        return "Hey! Ask me about transactions, spent, or received."

    # random text filter
    if not any(w in q for w in ["spent","received","credit","debit","transaction","balance","month","days"]):
        return "I can help with transactions, spent, or received."

    # balance (from DB, not calculated)
    if "balance" in q:
        latest = sorted([tx for tx in data if tx["date"]], key=lambda x: x["date"], reverse=True)
        if latest and latest[0].get("balance") is not None:
            return f"Current Balance: {latest[0]['balance']}"
        return "Balance not available in data"

    parsed = parse_query_ai(query)
    intent = (parsed.get("intent") or "").lower()

    start, end = get_date_range(parsed)
    filtered = filter_data(data, start, end)

    if not filtered:
        return "No transactions found"

    if "total transactions" in q or "count" in q:
        return f"Total Transactions: {len(filtered)}"

    spent = abs(sum(tx["amount"] for tx in filtered if tx["amount"] < 0))
    received = sum(tx["amount"] for tx in filtered if tx["amount"] > 0)

    if intent == "spent":
        return f"Spent: {round(spent,2)}"

    if intent == "received":
        return f"Received: {round(received,2)}"

    # default → transactions
    LIMIT = 20
    output = f"\nTransactions: {len(filtered)}\n\n"

    for tx in filtered[:LIMIT]:
        d = tx["date"].strftime("%Y-%m-%d") if tx["date"] else "N/A"
        bal = tx.get("balance", "N/A")
        output += f"{d} | {tx['type']} | {tx['amount']} | Balance: {bal}\n"

    return output

# =========================
# FASTAPI ROUTE
# =========================
class ChatRequest(BaseModel):
    query: str

@app.post("/chat")
def chat(req: ChatRequest):
    return {"response": chatbot(req.query)}

# =========================
# CLI TEST
# =========================
if __name__ == "__main__":
    print("\n🤖 AI Finance Bot Ready\n")
    while True:
        q = input("You: ")
        if q.lower() == "exit":
            break
        print("Bot:", chatbot(q))