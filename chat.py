"""Claude chat integration via AWS Bedrock for health insurance education."""

import os
import json
import re
import boto3
from datetime import datetime, timedelta
from prompts import SYSTEM_PROMPT
from typing import List, Dict, Optional


# US Federal Holidays for 2024-2025 (add more as needed)
US_HOLIDAYS = {
    # 2024
    (2024, 1, 1),   # New Year's Day
    (2024, 1, 15),  # MLK Day
    (2024, 2, 19),  # Presidents Day
    (2024, 5, 27),  # Memorial Day
    (2024, 6, 19),  # Juneteenth
    (2024, 7, 4),   # Independence Day
    (2024, 9, 2),   # Labor Day
    (2024, 10, 14), # Columbus Day
    (2024, 11, 11), # Veterans Day
    (2024, 11, 28), # Thanksgiving
    (2024, 12, 25), # Christmas
    # 2025
    (2025, 1, 1),   # New Year's Day
    (2025, 1, 20),  # MLK Day
    (2025, 2, 17),  # Presidents Day
    (2025, 5, 26),  # Memorial Day
    (2025, 6, 19),  # Juneteenth
    (2025, 7, 4),   # Independence Day
    (2025, 9, 1),   # Labor Day
    (2025, 10, 13), # Columbus Day
    (2025, 11, 11), # Veterans Day
    (2025, 11, 27), # Thanksgiving
    (2025, 12, 25), # Christmas
    # 2026
    (2026, 1, 1),   # New Year's Day
}


def is_business_day(date: datetime) -> bool:
    """Check if a date is a business day (weekday and not a holiday)."""
    # Check if weekend (Saturday=5, Sunday=6)
    if date.weekday() >= 5:
        return False
    # Check if holiday
    if (date.year, date.month, date.day) in US_HOLIDAYS:
        return False
    return True


def get_next_business_days(count: int = 3, min_days_ahead: int = 2) -> List[datetime]:
    """Get the next N business days, starting at least min_days_ahead from today."""
    today = datetime.now()
    start_date = today + timedelta(days=min_days_ahead)

    business_days = []
    current = start_date
    while len(business_days) < count:
        if is_business_day(current):
            business_days.append(current)
        current += timedelta(days=1)

    return business_days


def format_date_options() -> str:
    """Format the next available business days as options for the user."""
    days = get_next_business_days(3, min_days_ahead=2)
    options = []
    for d in days:
        # Format as "Monday, Dec 23"
        options.append(d.strftime("%A, %b %d"))
    return ", ".join(options)


def parse_user_date(user_input: str) -> Optional[datetime]:
    """Parse user's date input and validate it's a valid business day at least 2 days ahead."""
    today = datetime.now()
    min_date = today + timedelta(days=2)
    user_lower = user_input.lower().strip()

    # Handle relative day names
    day_map = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6
    }

    # Check for day names
    for day_name, day_num in day_map.items():
        if day_name in user_lower:
            # Find the next occurrence of this day that's at least 2 days ahead
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # Next week
            target = today + timedelta(days=days_ahead)
            # If less than 2 days ahead, go to next week
            while target < min_date:
                target += timedelta(days=7)
            return target if is_business_day(target) else None

    # Try parsing various date formats
    formats = [
        "%B %d", "%b %d",  # December 23, Dec 23
        "%m/%d", "%m-%d",  # 12/23, 12-23
        "%A, %b %d",       # Monday, Dec 23
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(user_input, fmt)
            # Add current year (or next year if date has passed)
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
            if parsed >= min_date and is_business_day(parsed):
                return parsed
        except ValueError:
            continue

    return None

# Initialize Bedrock client
bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name=os.getenv('AWS_REGION', 'us-east-1')
)

MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"  # Fast and cheap for chat


def detect_scheduling_context(messages: List[Dict[str, str]]) -> Optional[str]:
    """
    Detect what the assistant last asked for in the scheduling flow.
    Returns a direct response if we can handle it, otherwise returns None.
    """
    if len(messages) < 1:
        return None

    # Get the latest user message
    latest_user_msg = messages[-1]['content'].strip() if messages[-1]['role'] == 'user' else ""
    user_lower = latest_user_msg.lower()

    # Check if this is a scheduling request (can happen on first message)
    is_scheduling_request = (
        'schedule' in user_lower or
        'call me' in user_lower or
        ('agent' in user_lower and ('talk' in user_lower or 'speak' in user_lower or 'connect' in user_lower)) or
        'talk to someone' in user_lower or
        'speak with' in user_lower
    )

    # If only 1 message and it's a scheduling request, start the flow
    if len(messages) == 1 and is_scheduling_request:
        print("DEBUG: First message is scheduling request - asking for name")
        return "I'd be happy to connect you with a licensed agent! First, what's your name?"

    if len(messages) < 2:
        return None

    # Get the last assistant message
    last_assistant_msg = None
    for msg in reversed(messages[:-1]):  # Exclude the latest user message
        if msg['role'] == 'assistant':
            last_assistant_msg = msg['content'].lower()
            break

    if not last_assistant_msg:
        return None

    # Check if user sent digits (likely a phone number)
    digits_only = re.sub(r'\D', '', latest_user_msg)
    has_10_digits = len(digits_only) == 10

    # Debug logging
    print(f"DEBUG: Last assistant msg: {last_assistant_msg[:80]}...")
    print(f"DEBUG: User msg: {latest_user_msg}, has_10_digits: {has_10_digits}")

    # Skip if already confirmed (contains "Perfect" or "agent will call")
    if 'perfect' in last_assistant_msg or 'agent will call' in last_assistant_msg:
        print("DEBUG: Skipping - already confirmed")
        return None

    print(f"DEBUG: is_scheduling_request={is_scheduling_request}, user_lower={user_lower[:50]}")

    # Check if we're at the start of scheduling (no name question asked yet)
    has_name_question = any(
        ('your name' in m['content'].lower() or 'first name' in m['content'].lower())
        for m in messages if m['role'] == 'assistant'
    )
    print(f"DEBUG: has_name_question={has_name_question}")

    if is_scheduling_request and not has_name_question:
        print("DEBUG: Detected SCHEDULING INITIATION - asking for name")
        return "I'd be happy to connect you with a licensed agent! First, what's your name?"

    # NAME STEP: Assistant asked for name, user responded
    is_asking_for_name = (
        'your name' in last_assistant_msg or
        'first name' in last_assistant_msg or
        "what's your name" in last_assistant_msg
    )

    if is_asking_for_name and not has_10_digits:
        # User just gave their name
        name = latest_user_msg.strip()
        # Handle "my name is X" patterns
        name_match = re.search(r"(?:my name is|i'm|i am|call me|it's)\s+(\w+)", name, re.IGNORECASE)
        if name_match:
            name = name_match.group(1).capitalize()
        else:
            words = name.split()
            if words:
                name = words[0].capitalize()
        print(f"DEBUG: Detected NAME step - name is {name}")
        return f"Nice to meet you, {name}! What's the best phone number to reach you at?"

    # TIME STEP: Assistant asked for time preference (morning/afternoon/evening)
    # Check this FIRST to prevent phone detection from triggering on time question
    is_asking_for_time = (
        'morning' in last_assistant_msg and
        'afternoon' in last_assistant_msg and
        'evening' in last_assistant_msg
    )

    if is_asking_for_time:
        print("DEBUG: Detected TIME step")
        time_pref = latest_user_msg.lower()
        # Extract name, phone from user messages and date from assistant message
        name = "there"
        phone = ""
        date = ""

        # Look for date in the previous assistant message (format: "Great, Monday, December 23 it is!")
        date_match = re.search(r'Great,\s+([A-Za-z]+,\s+[A-Za-z]+\s+\d+)\s+it is', last_assistant_msg, re.IGNORECASE)
        if date_match:
            date = date_match.group(1)

        # Extract name by looking at what came after assistant asked for name
        found_name_question = False
        for msg in messages:
            if msg['role'] == 'assistant':
                msg_lower = msg['content'].lower()
                if 'your name' in msg_lower or 'first name' in msg_lower or "what's your name" in msg_lower:
                    found_name_question = True
            elif msg['role'] == 'user':
                content = msg['content'].strip()
                content_digits = re.sub(r'\D', '', content)
                if len(content_digits) == 10:
                    phone = content_digits
                elif found_name_question and name == "there":
                    # This should be the name
                    if not re.match(r'^[\d\s\-\(\)\.]+$', content):
                        name_match = re.search(r"(?:my name is|i'm|i am|call me|it's)\s+(\w+)", content, re.IGNORECASE)
                        if name_match:
                            name = name_match.group(1).capitalize()
                        else:
                            words = content.split()
                            if words and len(words[0]) > 1:
                                name = words[0].capitalize()
                        found_name_question = False  # Only capture once

        # Determine time from user response
        if 'morning' in time_pref:
            time_str = 'morning'
        elif 'afternoon' in time_pref:
            time_str = 'afternoon'
        elif 'evening' in time_pref:
            time_str = 'evening'
        else:
            time_str = time_pref

        # Build confirmation message
        if phone and date:
            return f"Perfect, {name}! An agent will call you at {phone} on {date} in the {time_str}. Is there anything else I can help you with?"
        elif phone:
            return f"Perfect, {name}! An agent will call you at {phone} in the {time_str}. Is there anything else I can help you with?"
        elif date:
            return f"Perfect, {name}! An agent will call you on {date} in the {time_str}. Is there anything else I can help you with?"
        else:
            return f"Perfect, {name}! An agent will call you in the {time_str}. Is there anything else I can help you with?"

    # DATE STEP: Assistant asked for date (check before phone to avoid conflicts)
    is_asking_for_date = (
        'what day' in last_assistant_msg or
        'which day' in last_assistant_msg or
        'available days' in last_assistant_msg or
        ('day' in last_assistant_msg and 'works' in last_assistant_msg)
    )

    if is_asking_for_date:
        print("DEBUG: Detected DATE step")
        # Validate the user's date choice
        parsed_date = parse_user_date(latest_user_msg)
        if parsed_date:
            date_formatted = parsed_date.strftime("%A, %B %d")
            return f"Great, {date_formatted} it is! What time works best - morning, afternoon, or evening?"
        else:
            # Invalid date - show available options
            options = format_date_options()
            return f"Sorry, that date isn't available. Our next available days are: {options}. Which works best for you?"

    # PHONE STEP: Assistant asked for phone (but NOT in a confirmation context)
    is_asking_for_phone = (
        ('phone' in last_assistant_msg or 'number' in last_assistant_msg) and
        ('what' in last_assistant_msg or 'best' in last_assistant_msg or 'reach' in last_assistant_msg) and
        'confirm' not in last_assistant_msg and
        'day' not in last_assistant_msg  # Not the date question
    )

    if has_10_digits and is_asking_for_phone:
        print("DEBUG: Detected PHONE step")
        # Extract name by looking at what came after assistant asked for name
        name = "there"
        found_name_question = False
        for i, msg in enumerate(messages):
            if msg['role'] == 'assistant':
                msg_lower = msg['content'].lower()
                # Check if assistant asked for name
                if 'your name' in msg_lower or 'first name' in msg_lower or "what's your name" in msg_lower:
                    found_name_question = True
            elif msg['role'] == 'user' and found_name_question:
                content = msg['content'].strip()
                # This message should be the name (comes right after name question)
                # Skip if it's digits
                if not re.match(r'^[\d\s\-\(\)\.]+$', content):
                    # Handle "my name is X" or "I'm X" patterns
                    name_match = re.search(r"(?:my name is|i'm|i am|call me|it's)\s+(\w+)", content, re.IGNORECASE)
                    if name_match:
                        name = name_match.group(1).capitalize()
                    else:
                        # Just take the first word as name
                        words = content.split()
                        if words and len(words[0]) > 1:
                            name = words[0].capitalize()
                    break

        print(f"DEBUG: Extracted name: {name}")
        # Get available business days (at least 2 days ahead)
        date_options = format_date_options()
        return f"Got it, {name}! What day works best for the call? Our next available days are: {date_options}"

    print("DEBUG: No scheduling context detected, passing to Claude")
    return None


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
    # Check if we can handle the scheduling flow directly (bypass Claude)
    direct_response = detect_scheduling_context(messages)
    if direct_response:
        print(f"DEBUG: Using direct response: {direct_response}")
        return direct_response

    # Otherwise, use Claude
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
