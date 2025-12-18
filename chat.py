"""Claude chat integration via AWS Bedrock for health insurance education."""

import os
import json
import boto3
from prompts import SYSTEM_PROMPT
from typing import List, Dict, Optional

# Initialize Bedrock client
bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name=os.getenv('AWS_REGION', 'us-east-1')
)

MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"  # Fast and cheap for chat


def chat_with_claude(
    messages: List[Dict[str, str]],
    system_prompt: str = SYSTEM_PROMPT,
    max_tokens: int = 300,
) -> str:
    """
    Send messages to Claude via Bedrock and get a response.

    Args:
        messages: List of message dicts with 'role' and 'content'
        system_prompt: System prompt for Claude
        max_tokens: Maximum tokens in response

    Returns:
        Claude's response text
    """
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    })

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        body=body
    )

    response_body = json.loads(response['body'].read())
    return response_body['content'][0]['text']


def extract_contact_info(messages: List[Dict[str, str]]) -> Dict[str, Optional[str]]:
    """
    Extract contact information from conversation history.

    Args:
        messages: Conversation history

    Returns:
        Dict with extracted contact info (first_name, last_name, email, phone)
    """
    import re

    contact_info = {
        'first_name': None,
        'last_name': None,
        'email': None,
        'phone': None,
    }

    # Get all user messages
    user_messages = [m['content'] for m in messages if m['role'] == 'user']
    full_text = ' '.join(user_messages)

    # Extract email
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    email_match = re.search(email_pattern, full_text)
    if email_match:
        contact_info['email'] = email_match.group()

    # Extract phone number (various formats)
    phone_patterns = [
        r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b',  # 123-456-7890, 123.456.7890, 123 456 7890
        r'\(\d{3}\)\s*\d{3}[-.\s]?\d{4}',  # (123) 456-7890
    ]
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_text)
        if phone_match:
            contact_info['phone'] = re.sub(r'[^\d]', '', phone_match.group())  # Normalize to digits
            break

    # Try to extract names from context
    # Look for patterns like "my name is X" or "I'm X" or just a single word after asking for name
    name_patterns = [
        r"(?:my name is|i'm|i am|this is|call me)\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?",
        r"^([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?$",  # Just a name on its own line
    ]

    for msg in user_messages:
        msg_clean = msg.strip()
        for pattern in name_patterns:
            match = re.search(pattern, msg_clean, re.IGNORECASE)
            if match:
                if match.group(1) and not contact_info['first_name']:
                    # Check it's not a common word
                    first = match.group(1).capitalize()
                    if first.lower() not in ['yes', 'no', 'sure', 'okay', 'thanks', 'hello', 'hi', 'hey', 'medicare', 'insurance']:
                        contact_info['first_name'] = first
                if match.lastindex and match.lastindex >= 2 and match.group(2) and not contact_info['last_name']:
                    contact_info['last_name'] = match.group(2).capitalize()

    return contact_info


def has_complete_contact_info(contact_info: Dict[str, Optional[str]]) -> bool:
    """Check if we have enough contact info to create a lead."""
    has_name = contact_info.get('first_name') is not None
    has_contact = contact_info.get('email') is not None or contact_info.get('phone') is not None
    return has_name and has_contact


def detect_insurance_topics(message: str) -> List[str]:
    """
    Detect insurance topics mentioned in a message.

    Args:
        message: User's message text

    Returns:
        List of detected topics
    """
    topics = []
    message_lower = message.lower()

    topic_keywords = {
        "Medicare": ["medicare", "part a", "part b", "part c", "part d", "65", "turning 65"],
        "Medicare Advantage": ["medicare advantage", "ma plan", "part c"],
        "Medigap": ["medigap", "supplement", "supplemental"],
        "ACA/Marketplace": ["marketplace", "obamacare", "aca", "healthcare.gov", "subsidy", "subsidies"],
        "Medicaid": ["medicaid", "low income", "medicaid expansion"],
        "Prescription Drugs": ["drug", "medication", "prescription", "part d", "pharmacy"],
        "Enrollment": ["enroll", "sign up", "open enrollment", "deadline"],
        "Costs": ["cost", "premium", "deductible", "copay", "afford"],
        "Coverage": ["cover", "coverage", "benefit", "include"],
    }

    for topic, keywords in topic_keywords.items():
        if any(keyword in message_lower for keyword in keywords):
            topics.append(topic)

    return topics


def should_suggest_agent(messages: List[Dict[str, str]], topics: List[str]) -> bool:
    """
    Determine if we should suggest speaking with an agent.

    Args:
        messages: Conversation history
        topics: Topics discussed so far

    Returns:
        True if we should suggest agent connection
    """
    # Suggest after 4+ exchanges or if multiple topics discussed
    message_count = len([m for m in messages if m["role"] == "user"])

    if message_count >= 4:
        return True

    if len(topics) >= 3:
        return True

    # Check for specific intent signals
    intent_signals = [
        "what should i do",
        "what plan",
        "which one",
        "help me choose",
        "confused",
        "don't know what",
        "recommend",
        "best option",
        "sign up",
        "enroll",
    ]

    last_message = messages[-1]["content"].lower() if messages else ""
    if any(signal in last_message for signal in intent_signals):
        return True

    return False


def generate_lead_summary(messages: List[Dict[str, str]], topics: List[str]) -> str:
    """
    Generate a summary of the conversation for lead notes.

    Args:
        messages: Conversation history
        topics: Topics discussed

    Returns:
        Summary string
    """
    summary_prompt = f"""Based on this conversation, provide a brief 2-3 sentence summary for a licensed agent to review before calling this lead. Include:
1. What the person is looking for
2. Key details about their situation (age, current coverage, concerns)
3. Topics they asked about

Topics discussed: {', '.join(topics)}

Conversation:
"""

    for msg in messages[-6:]:  # Last 6 messages for context
        role = "User" if msg["role"] == "user" else "Assistant"
        summary_prompt += f"\n{role}: {msg['content']}"

    summary_messages = [{"role": "user", "content": summary_prompt}]

    return chat_with_claude(
        summary_messages,
        system_prompt="You are a helpful assistant that summarizes conversations. Be concise and factual.",
        max_tokens=200,
    )
