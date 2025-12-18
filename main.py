"""FastAPI backend for health insurance chatbot."""

import os
import uuid
from datetime import datetime
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import init_db, get_db, Lead, ChatSession
from chat import chat_with_claude, detect_insurance_topics, should_suggest_agent, generate_lead_summary, extract_contact_info, has_complete_contact_info
from prompts import CONVERSATION_STARTERS

# Initialize FastAPI app
app = FastAPI(
    title="Health Insurance Chatbot API",
    description="Educational chatbot for Medicare and health insurance questions",
    version="1.0.0",
)

# CORS configuration - allow all origins for App Runner deployment
# In production, restrict this to your specific frontend domain
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database on startup
@app.on_event("startup")
def startup():
    init_db()


# Pydantic models
class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    response: str
    suggest_agent: bool
    topics: List[str]
    lead_captured: bool = False
    contact_info: Optional[dict] = None


class LeadCreate(BaseModel):
    session_id: Optional[str] = None
    first_name: str
    last_name: str
    email: EmailStr
    phone: Optional[str] = None
    zip_code: Optional[str] = None
    state: Optional[str] = None
    insurance_interest: str


class LeadResponse(BaseModel):
    id: int
    message: str


# API Endpoints
@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "Health Insurance Chatbot API",
        "disclaimer": "This tool provides general educational information and does not replace advice from a licensed insurance agent.",
    }


@app.get("/api/chat/start")
def start_chat():
    """Start a new chat session."""
    import random

    session_id = str(uuid.uuid4())
    starter = random.choice(CONVERSATION_STARTERS)

    return {
        "session_id": session_id,
        "message": starter,
        "disclaimer": "This tool provides general educational information and does not replace advice from a licensed insurance agent.",
    }


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, checking forwarded headers."""
    # Check for forwarded headers (when behind proxy/load balancer)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    # Fall back to direct client IP
    return request.client.host if request.client else "unknown"


@app.post("/api/chat", response_model=ChatResponse)
def chat(chat_request: ChatRequest, request: Request, db: Session = Depends(get_db)):
    """Send a message and get a response."""

    # Get client IP for logging
    client_ip = get_client_ip(request)

    # Get or create session
    session_id = chat_request.session_id or str(uuid.uuid4())

    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()

    if not session:
        session = ChatSession(
            session_id=session_id,
            messages=[],
            insurance_topics=[],
            ip_address=client_ip,
        )
        db.add(session)
        db.commit()
    elif not session.ip_address:
        # Update IP if not already set
        session.ip_address = client_ip

    # Get conversation history
    messages = session.messages or []

    # Add user message
    messages.append({"role": "user", "content": chat_request.message})

    # Detect topics in this message
    new_topics = detect_insurance_topics(chat_request.message)
    all_topics = list(set((session.insurance_topics or []) + new_topics))

    # Get Claude response
    try:
        response_text = chat_with_claude(messages)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")

    # Add assistant response
    messages.append({"role": "assistant", "content": response_text})

    # Check if we should suggest agent
    suggest_agent = should_suggest_agent(messages, all_topics)

    # Extract contact info from conversation
    contact_info = extract_contact_info(messages)
    lead_captured = False

    # Auto-create lead if we have enough contact info and haven't already
    if has_complete_contact_info(contact_info) and not session.lead_id:
        try:
            db_lead = Lead(
                first_name=contact_info.get('first_name', ''),
                last_name=contact_info.get('last_name', ''),
                email=contact_info.get('email'),
                phone=contact_info.get('phone'),
                insurance_interest=all_topics[0] if all_topics else 'General',
                source="Hackathon Chatbot - Conversational",
                notes=f"Auto-captured from chat. Topics: {', '.join(all_topics)}",
            )
            db.add(db_lead)
            db.commit()
            db.refresh(db_lead)
            session.lead_id = db_lead.id
            lead_captured = True
        except Exception:
            pass  # Don't fail the chat if lead creation fails

    # Update session
    session.messages = messages
    session.insurance_topics = all_topics
    session.updated_at = datetime.utcnow()
    db.commit()

    return ChatResponse(
        session_id=session_id,
        response=response_text,
        suggest_agent=suggest_agent,
        topics=all_topics,
        lead_captured=lead_captured,
        contact_info=contact_info if any(contact_info.values()) else None,
    )


@app.post("/api/leads", response_model=LeadResponse)
def create_lead(lead: LeadCreate, db: Session = Depends(get_db)):
    """Capture a lead from chatbot interaction."""

    # Generate summary from chat if session exists
    notes = None
    if lead.session_id:
        session = db.query(ChatSession).filter(ChatSession.session_id == lead.session_id).first()
        if session and session.messages:
            try:
                notes = generate_lead_summary(session.messages, session.insurance_topics or [])
            except Exception:
                notes = f"Topics discussed: {', '.join(session.insurance_topics or [])}"

    # Create lead
    db_lead = Lead(
        first_name=lead.first_name,
        last_name=lead.last_name,
        email=lead.email,
        phone=lead.phone,
        zip_code=lead.zip_code,
        state=lead.state,
        insurance_interest=lead.insurance_interest,
        source="Hackathon Chatbot",
        notes=notes,
    )

    db.add(db_lead)
    db.commit()
    db.refresh(db_lead)

    # Link lead to session if exists
    if lead.session_id:
        session = db.query(ChatSession).filter(ChatSession.session_id == lead.session_id).first()
        if session:
            session.lead_id = db_lead.id
            db.commit()

    return LeadResponse(
        id=db_lead.id,
        message="Thank you! A licensed agent will reach out to you soon.",
    )


@app.get("/api/leads")
def list_leads(db: Session = Depends(get_db)):
    """List all captured leads (for demo purposes)."""
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    return [
        {
            "id": lead.id,
            "name": f"{lead.first_name} {lead.last_name}",
            "email": lead.email,
            "phone": lead.phone,
            "interest": lead.insurance_interest,
            "source": lead.source,
            "created_at": lead.created_at.isoformat(),
            "notes": lead.notes,
        }
        for lead in leads
    ]


@app.get("/api/health")
def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
