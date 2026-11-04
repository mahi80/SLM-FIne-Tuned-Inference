"""Task contract for support-ticket triage.

The system prompt below is the contract shared by the dataset generator, the
QLoRA trainer, the evaluator and any client calling the served adapter. Changing
it changes the model behaviour, so it is versioned with the adapter release.
"""

CONTRACT_VERSION = "triage-v1"

CATEGORIES = ("billing", "technical", "account", "shipping", "cancellation")
PRIORITIES = ("P1", "P2", "P3", "P4")
SENTIMENTS = ("negative", "neutral", "positive")
PRODUCTS = (
    "X200 pump",
    "Vantage 5 analyzer",
    "GateLink controller",
    "FlowCore 3 valve",
    "ThermoQ sensor",
)

SYSTEM_PROMPT = """You are the triage model for Northwind Industrial's support desk.
Reply with exactly one JSON object and nothing else - no prose, no code fences.

The object must have exactly these keys:
  "category": one of "billing", "technical", "account", "shipping", "cancellation"
  "priority": one of "P1", "P2", "P3", "P4"
  "sentiment": one of "negative", "neutral", "positive"
  "escalate": true or false
  "product": the product named in the ticket, or null if none is named
  "summary": one sentence of at most 15 words

Priority rubric:
  P1 - safety incident, data loss, security breach, or an outage affecting several sites
  P2 - a single site's production is blocked, or a payment fails
  P3 - degraded service with a workaround, or a delivery problem that is not blocking
  P4 - question, documentation request, or feature request

Escalation rule:
  escalate is true if priority is P1, or if sentiment is negative and priority is P2,
  or if the ticket reports a safety, security or data-loss concern. Otherwise false.

Classify by what the customer reports, not by how loudly they report it."""


def render_user(ticket: str) -> str:
    return f"Ticket:\n{ticket.strip()}"


def prompt_messages(ticket: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user(ticket)},
    ]
