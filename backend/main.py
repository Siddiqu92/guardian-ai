from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import os
import re
import httpx
from datetime import datetime

load_dotenv()

app = FastAPI(title="SentinelAI - Healthcare Agent Security")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

audit_logs = []
stats = {
    "total_requests": 0,
    "blocked_requests": 0,
    "pii_detected": 0,
    "injections_detected": 0,
    "allowed_requests": 0,
}

PHI_PATTERNS = {
    "SSN": r'\b\d{3}-\d{2}-\d{4}\b',
    "Phone": r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b',
    "Email": r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    "DOB": r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b',
    "MedicalID": r'\bMRN[-:\s]?\d{6,10}\b',
    "CreditCard": r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b',
}

INJECTION_PATTERNS = [
    r'ignore previous instructions',
    r'ignore all instructions',
    r'disregard.*instructions',
    r'you are now',
    r'act as',
    r'pretend you are',
    r'jailbreak',
    r'dan mode',
    r'bypass.*filter',
    r'override.*policy',
    r'reveal.*patient',
    r'admin mode',
]

class PromptRequest(BaseModel):
    prompt: str
    user_id: str = "anonymous"
    department: str = "general"

def detect_phi(text: str):
    detected = []
    for phi_type, pattern in PHI_PATTERNS.items():
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            detected.append({"type": phi_type, "count": len(matches), "masked": True})
    return detected

def detect_injection(text: str):
    text_lower = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False

def mask_phi(text: str):
    masked = text
    for phi_type, pattern in PHI_PATTERNS.items():
        masked = re.sub(pattern, f"[{phi_type}_REDACTED]", masked, flags=re.IGNORECASE)
    return masked

def calculate_risk_score(phi_detected, injection_detected):
    score = 0
    if injection_detected:
        score += 80
    score += len(phi_detected) * 15
    return min(score, 100)

async def call_gemini(prompt: str):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                GEMINI_URL,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 500}
                }
            )
            data = response.json()
            if "candidates" in data and len(data["candidates"]) > 0:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            elif "error" in data:
                return f"Gemini API Error: {data['error']['message']}"
            else:
                return f"Unexpected response: {str(data)[:200]}"
    except Exception as e:
        return f"Gemini Error: {str(e)}"

@app.post("/analyze")
async def analyze_prompt(request: PromptRequest):
    stats["total_requests"] += 1

    phi_detected = detect_phi(request.prompt)
    injection_detected = detect_injection(request.prompt)
    risk_score = calculate_risk_score(phi_detected, injection_detected)
    masked_prompt = mask_phi(request.prompt)

    if injection_detected:
        action = "BLOCKED"
        reason = "Prompt injection attack detected"
        stats["blocked_requests"] += 1
        stats["injections_detected"] += 1
    elif len(phi_detected) > 0 and risk_score > 50:
        action = "QUARANTINED"
        reason = f"High-risk PHI detected: {[p['type'] for p in phi_detected]}"
        stats["blocked_requests"] += 1
        stats["pii_detected"] += 1
    elif len(phi_detected) > 0:
        action = "MASKED_AND_ALLOWED"
        reason = "PHI detected and masked before Gemini call"
        stats["pii_detected"] += 1
        stats["allowed_requests"] += 1
    else:
        action = "ALLOWED"
        reason = "No threats detected"
        stats["allowed_requests"] += 1

    log_entry = {
        "id": len(audit_logs) + 1,
        "timestamp": datetime.now().isoformat(),
        "user_id": request.user_id,
        "department": request.department,
        "action": action,
        "risk_score": risk_score,
        "phi_detected": phi_detected,
        "injection_detected": injection_detected,
        "reason": reason,
        "original_prompt_length": len(request.prompt),
    }
    audit_logs.append(log_entry)

    llm_response = None
    if action in ["ALLOWED", "MASKED_AND_ALLOWED"]:
        llm_response = await call_gemini(masked_prompt)

    return {
        "status": "success",
        "action": action,
        "risk_score": risk_score,
        "phi_detected": phi_detected,
        "injection_detected": injection_detected,
        "masked_prompt": masked_prompt,
        "reason": reason,
        "llm_response": llm_response,
        "audit_id": log_entry["id"]
    }

@app.get("/dashboard/stats")
async def get_stats():
    compliance_score = 100
    if stats["total_requests"] > 0:
        breach_rate = stats["pii_detected"] / stats["total_requests"]
        compliance_score = max(0, int(100 - (breach_rate * 50)))
    return {
        **stats,
        "compliance_score": compliance_score,
        "hipaa_status": "COMPLIANT" if compliance_score >= 80 else "AT_RISK"
    }

@app.get("/dashboard/logs")
async def get_logs():
    return {"logs": list(reversed(audit_logs[-50:]))}

@app.get("/health")
async def health():
    return {"status": "SentinelAI is running", "version": "1.0.0"}