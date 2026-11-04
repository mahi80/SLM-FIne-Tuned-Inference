"""Deterministic synthetic dataset for support-ticket triage.

The dataset is generated, not downloaded: every ticket is a template with slots
filled from fixed vocabularies, and its labels come from the scenario definition
rather than from a second model. That keeps supervision exact and the split
reproducible.

Splits:
  train-v1  clean scenarios only, first two paraphrases      (easy curriculum)
  train-v2  clean + adversarial hard cases, first two paraphrases
  eval      clean + hard, last two paraphrases, plus four scenario families
            that never appear in training (distribution shift)

Nothing here is a real customer record.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path

from finetune.prompt import PRODUCTS

CUSTOMERS = (
    "Acme Foods",
    "Brightline Metals",
    "Cobalt Water",
    "Dunmore Chemicals",
    "Eastgate Pharma",
    "Fairhaven Mills",
    "Granite Packaging",
    "Harbor Textiles",
)
SITES = (
    "plant 2",
    "the north line",
    "the Oswego site",
    "the blending hall",
    "the packaging cell",
    "the main plant",
    "the Riverside works",
    "the finishing line",
)


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    priority: str
    sentiment: str
    escalate: bool
    product: bool
    summary: str
    variants: tuple[str, ...]
    eval_only: bool = False


CLEAN: tuple[Scenario, ...] = (
    Scenario("billing-duplicate", "billing", "P3", "negative", False, True,
             "Duplicate charge on invoice {order}",
             ("You billed the {product} order {order} twice this month and the second charge of ${amount} is still on our card. Please reverse it.",
              "Invoice {order} shows a duplicate {product} line for ${amount}. We only received one shipment. Can you correct it?",
              "We have been charged twice for the {product} order {order}. The duplicate ${amount} payment needs to be refunded.",
              "There is a second ${amount} charge on our statement for order {order}. Please take a look and reverse the duplicate.")),
    Scenario("billing-payment-failed", "billing", "P2", "negative", True, True,
             "Renewal payment failed and monitoring is locked",
             ("The renewal payment for our {product} subscription failed even though the card is valid, and our monitoring portal is locked.",
              "Our card was declined on the {product} renewal this morning. The whole team is locked out until it is paid.",
              "Billing could not charge the subscription card and the {product} dashboard now refuses every login. We need this unblocked today.",
              "Payment for the {product} plan failed overnight and nobody can reach the portal. What do you need from us to fix it?")),
    Scenario("billing-invoice-copy", "billing", "P4", "neutral", False, False,
             "Request for a copy of invoice {order}",
             ("Could you send a PDF copy of invoice {order} for our finance team? No rush.",
              "Please email a copy of invoice {order} when convenient.",
              "Our accounts payable system lost invoice {order}. Can you resend it as a PDF?",
              "I need a copy of invoice {order} for month-end. No urgency, whenever you can.")),
    Scenario("billing-overcharge", "billing", "P3", "negative", False, True,
             "Charged for unused licences",
             ("We were charged for eight {product} licences but only five are in use. Please refund the difference on invoice {order}.",
              "Invoice {order} bills eight seats and our contract says five. We can keep working, but the overcharge needs correcting.",
              "There are three extra licences on the last {product} invoice. We are not blocked, just send a credit note.",
              "You charged for eight seats, we use five. Not urgent, but please adjust invoice {order}.")),
    Scenario("billing-thanks", "billing", "P4", "positive", False, True,
             "Thanks for correcting the billing error",
             ("Thanks for sorting out last month's {product} billing correction so quickly.",
              "The credit note for order {order} arrived this morning. Appreciate the fast turnaround.",
              "Just wanted to say the invoice correction was handled well, thank you.",
              "Thanks for fixing the duplicate charge before month-end. That helped our close a lot.")),
    Scenario("tech-vibration", "technical", "P3", "negative", False, True,
             "Excess vibration with a workaround",
             ("The {product} at {site} is vibrating more than usual but still running. We can live with it until the next service window.",
              "Our {product} on {site} has started to vibrate at higher load. Production continues, so this can wait for the planned outage.",
              "There is a new rattle from the {product} during ramp-up. We reduced speed and it is stable, so no rush.",
              "The {product} at {site} sounds rough at full speed. We are running it slower until someone can look at it.")),
    Scenario("tech-site-down", "technical", "P2", "negative", True, True,
             "Single-site outage blocking production",
             ("The {product} at {site} has been offline since 06:00 and the line is stopped. Nothing we restart brings it back.",
              "Our {product} is down at {site} and production is blocked. We have power-cycled it twice with no change.",
              "The {product} controller will not start on {site}, so the whole line is stopped. We need help now.",
              "Since this morning the {product} at {site} shows a red fault and the line cannot run. No workaround.")),
    Scenario("tech-safety", "technical", "P1", "negative", True, True,
             "Safety incident with crew evacuation",
             ("The {product} at {site} stuck open during the night shift and the crew was evacuated. Nobody was hurt.",
              "A {product} failed closed on {site} and the area was cleared for two hours. No injuries, but we need an inspection.",
              "The {product} released pressure unexpectedly at {site} and we evacuated the hall. It is isolated now.",
              "Operators heard a loud release from the {product} at {site} and pulled the emergency stop. Everyone is safe.")),
    Scenario("tech-data-loss", "technical", "P1", "negative", True, True,
             "Audit records lost after buffer overwrite",
             ("We lost three days of readings from the {product} because the gateway overwrote the buffer. Those records are required for our audit.",
              "The {product} history for last week is gone after the gateway restarted. We need those records for an audit and cannot recreate them.",
              "Our {product} database contains no data between Monday and Thursday. That is regulated data and we have no backup.",
              "Half of the {product} trend history disappeared overnight. This is audit evidence, so we need it recovered.")),
    Scenario("tech-modbus-question", "technical", "P4", "neutral", False, True,
             "Question about Modbus register map",
             ("Does the {product} expose a Modbus register map we can poll from our own SCADA system?",
              "Where can I find the Modbus register list for the {product}? We are planning an integration.",
              "Is there a published register map for the {product}? Our controls team wants to poll it directly.",
              "Can the {product} be read over Modbus? If so, please point me at the register documentation.")),
    Scenario("tech-firmware-positive", "technical", "P4", "positive", False, True,
             "Firmware update fixed slow charts",
             ("The new {product} firmware fixed the slow trend charts. Nice work.",
              "After the firmware update the {product} dashboard is much faster. Thanks to the team.",
              "The {product} firmware release solved our chart delays. Please pass on the thanks.",
              "Trend rendering on the {product} is finally quick after the update. Good job.")),
    Scenario("account-password", "account", "P4", "neutral", False, False,
             "Password reset link expired",
             ("How do I reset the password for our portal admin account? The email link expired.",
              "The password reset email for our admin account has expired. Can you send a new one?",
              "Our portal admin cannot log in and the reset link no longer works. What is the procedure?",
              "Could you explain how to reset an expired admin password on the portal?")),
    Scenario("account-suspicious-login", "account", "P1", "negative", True, False,
             "Unauthorised login from another country",
             ("We received a login alert from another country for our admin account. We did not authorise it and need it locked down now.",
              "Our security team flagged a portal sign-in from an unfamiliar country on the admin account. Please revoke all sessions.",
              "Someone logged into our portal admin account from abroad. We believe the credentials leaked and want them invalidated.",
              "There is an active session on our admin account that nobody here started, from outside the country. Lock it down.")),
    Scenario("account-add-seats", "account", "P4", "neutral", False, True,
             "Question about adding subscription seats",
             ("We are adding two engineers next month. How do we add seats to our {product} subscription?",
              "What is the process for adding two more users to the {product} portal?",
              "We need to expand the {product} subscription by two seats next month. Can you explain the steps?",
              "How do we purchase additional {product} seats for new starters?")) ,
    Scenario("account-sso-broken", "account", "P2", "negative", True, False,
             "SSO change locked out every user",
             ("Our SSO provider change broke every user's login and the team cannot reach the portal. We have no workaround.",
              "Since we switched identity providers nobody can sign in to the portal. The whole team is blocked.",
              "The SSO migration broke authentication for all portal users. There is no local login fallback.",
              "Every user is locked out after the identity provider change. We need a fix before the morning shift.")),
    Scenario("account-thanks", "account", "P4", "positive", False, False,
             "Thanks for the account merge",
             ("The account merge you did last week went smoothly, thanks.",
              "Thanks for merging the two portal accounts, everything works now.",
              "The account consolidation is done and our team is happy with it. Appreciate the help.",
              "Just a note to say the account merge was painless. Thank you.")),
    Scenario("ship-delay", "shipping", "P3", "negative", False, True,
             "Delivery delayed but not blocking",
             ("Order {order} for the {product} was due on Monday and it is still showing as in transit. We have some buffer, but please chase it.",
              "The {product} shipment for order {order} is a week late. We still have stock, so just let us know where it is.",
              "Order {order} has not arrived yet. Not blocking us this week, but we would like an update on the {product}.",
              "Our {product} delivery for order {order} slipped again. We can cover production for now, please chase the carrier.")),
    Scenario("ship-lost", "shipping", "P3", "negative", False, True,
             "Shipment lost in transit",
             ("The carrier says the {product} shipment for order {order} is lost. We can wait for a replacement, but please start one.",
              "Order {order} never arrived and tracking has not moved for ten days. We have enough {product} stock for now.",
              "Our {product} shipment for order {order} appears to be lost. Please send a replacement when you can.",
              "Tracking for order {order} is dead and the carrier cannot find the pallet. We are not blocked yet.")),
    Scenario("ship-wrong-item", "shipping", "P3", "negative", False, True,
             "Wrong mounting kit shipped",
             ("The box for order {order} contained the wrong mounting kit. We can adapt the old one for now, but we need the correct part.",
              "Order {order} arrived with the wrong {product} kit. We improvised a mount, so this is not urgent, but please correct it.",
              "You shipped the wrong bracket with order {order}. We have a temporary fix in place and need the right part.",
              "The mounting kit in order {order} does not match our {product}. We can keep running on the old one for now.")),
    Scenario("ship-damaged-safety", "shipping", "P1", "negative", True, True,
             "Damaged unit with exposed wiring",
             ("The {product} arrived with a cracked housing and exposed wiring. We have quarantined it because it is a shock hazard.",
              "Order {order} delivered a {product} with a broken enclosure and live conductors visible. Safety has isolated it.",
              "The {product} we received is physically damaged with wiring exposed. We will not power it on and need a replacement urgently.",
              "Our delivery of the {product} has a split case and bare wires. It is locked away as a safety risk.")),
    Scenario("ship-tracking", "shipping", "P4", "neutral", False, False,
             "Question about tracking number",
             ("Where can I find the tracking number for order {order}?",
              "Could you send the tracking reference for order {order}?",
              "I cannot locate the tracking details for order {order}. Where should I look?",
              "Is there a tracking number for order {order}?")),
    Scenario("cancel-subscription", "cancellation", "P3", "neutral", False, True,
             "End-of-quarter subscription cancellation",
             ("We are closing the {site} pilot and need to cancel the {product} subscription at the end of the quarter.",
              "Please note that the {product} subscription for {site} should end with the quarter. We are consolidating vendors.",
              "We plan to cancel the {product} plan for {site} at quarter end. What do you need from us?",
              "The pilot at {site} is not continuing, so please schedule cancellation of the {product} subscription.")),
    Scenario("cancel-refund", "cancellation", "P3", "negative", False, True,
             "Refund not received after cancellation",
             ("We cancelled order {order} last week but the charge is still on our card. Please confirm the refund.",
              "The {product} order was cancelled and we still have not seen the refund for order {order}.",
              "You cancelled order {order} five days ago and the money has not come back. We can wait, but please confirm.",
              "Order {order} was cancelled but the refund is missing. We are not blocked, just chasing it.")),
    Scenario("cancel-competitor", "cancellation", "P3", "neutral", False, True,
             "Early exit terms for the contract",
             ("We are moving to another vendor and want to cancel the {product} contract early. What are the exit terms?",
              "Please explain the early termination terms for our {product} agreement. We are evaluating a switch.",
              "Our procurement team is considering a different supplier and asked what cancelling the {product} contract would cost.",
              "We may end the {product} contract early. Can you send the termination terms?")),
)


HARD: tuple[Scenario, ...] = (
    Scenario("hard-sarcasm", "technical", "P2", "negative", True, True,
             "Sarcastic report of a line outage",
             ("Fantastic work on the update. The {product} at {site} has been down all morning and the line is stopped. Truly world-class reliability.",
              "Brilliant job, {product} is offline at {site} and production is halted. Keep up the excellent work.",
              "Love the new release: {product} down at {site}, line stopped, zero output since dawn.",
              "Another flawless update. {product} at {site} is dead and we are losing a shift of output.")),
    Scenario("hard-caps-question", "account", "P4", "neutral", False, False,
             "Urgent-looking request to change portal email",
             ("URGENT!!! ASAP!!! HOW DO I CHANGE THE EMAIL ON MY PORTAL ACCOUNT???",
              "NEED THIS NOW: how do I change the email address on our portal admin account?",
              "ASAP HELP: changing the email on a portal account, what is the procedure?",
              "URGENT: I need the steps to update the email address on our portal login.")),
    Scenario("hard-urgent-docs", "technical", "P4", "neutral", False, True,
             "Urgent wording for a documentation request",
             ("URGENT: where is the Modbus register map for the {product}? I need it for a design review tomorrow, there is no outage.",
              "ASAP please: the register documentation for the {product}. Nothing is broken, I just need the document.",
              "Need the {product} Modbus register list urgently for a design meeting. No production issue.",
              "Can you send the {product} register map today? Marked urgent, but it is only for documentation.")),
    Scenario("hard-multi-issue", "billing", "P3", "negative", False, True,
             "Duplicate charge plus a slow trends page",
             ("Two problems: invoice {order} was charged twice, and the trends page on the {product} is slow. Please fix the billing first.",
              "You double-charged invoice {order} and the {product} trends view takes minutes to load. Billing is the main issue.",
              "Invoice {order} has a duplicate charge. Separately, the {product} trends page is slow. Start with the money.",
              "We were overcharged on invoice {order} and the {product} charts are sluggish. The billing error matters most.")),
    Scenario("hard-missing-product", "technical", "P2", "negative", True, False,
             "Line controller down with no model number",
             ("Our line controller has been offline since last night and the whole cell is stopped. I cannot find the model number anywhere on the unit.",
              "The controller on {site} is dead and production is blocked. There are no markings on the box, so I cannot tell you the model.",
              "Since last night the control unit at {site} will not power up and the cell is stopped. No nameplate on it.",
              "Our controller failed overnight and the line is down at {site}. I do not have a model number for you.")),
    Scenario("hard-typos", "shipping", "P3", "negative", False, True,
             "Wrong part delivered, casually written",
             ("recived the rong part for order {order}, box says {product} kit but we need the other one. can we swap it",
              "we got the wrong kit for order {order}, its for the {product} not ours. still have the old one fitted so no panic",
              "order {order} came with the wrong bracket kit, we made it work temporarily. pls send correct one",
              "wrong part in the box for {order}, we can manage for now with the old hardware. swap when possibl")),
    Scenario("hard-feature-request", "technical", "P4", "positive", False, True,
             "Feature request for Parquet export",
             ("Would be great if the {product} export supported Parquet. Not urgent at all, just a nice-to-have.",
              "Feature idea: Parquet export from the {product}. No rush, we are big fans of the product.",
              "Any chance the {product} could export Parquet? Low priority, just curious.",
              "It would help if {product} trends exported to Parquet. Purely a wishlist item, keep up the good work.")),
    Scenario("hard-phishing", "account", "P1", "negative", True, False,
             "Operator gave credentials to a caller",
             ("One of our operators gave portal credentials to someone who called claiming to be your support team. We need those sessions revoked immediately.",
              "A caller impersonating your helpdesk obtained an operator's portal password. Please revoke all active sessions now.",
              "Someone phoned our operator pretending to be support and took their credentials. We need the account locked down.",
              "Our operator was tricked into sharing portal credentials by a caller claiming to be from your team. Revoke access.")),
    Scenario("hard-near-miss", "technical", "P1", "negative", True, True,
             "Repeated emergency stops",
             ("The {product} at {site} tripped the emergency stop twice today. No injuries yet, but we are not restarting it until you inspect it.",
              "The {product} on {site} has issued two emergency stops today. We have locked it out until an engineer inspects it.",
              "Twice today the {product} at {site} went to emergency stop. Nobody was hurt, but we will not restart it.",
              "Our {product} at {site} keeps hitting the safety stop. The line is isolated and we need an inspection.")),
    Scenario("hard-data-loss-soft", "technical", "P1", "negative", True, True,
             "Audit history empty after firmware push",
             ("After the firmware push the {product} is reporting empty history. We cannot find six weeks of audit records.",
              "Since the firmware update the {product} shows no history at all. Six weeks of audit data is missing.",
              "The {product} trend database is empty after the update. Those records are audit evidence and we have no copy.",
              "We pushed the firmware and the {product} lost its history. It is regulated audit data.")),
    Scenario("hard-delay-calm", "shipping", "P3", "neutral", False, True,
             "Delivery slipped with stock still available",
             ("Order {order} slipped a week. Not blocking us, we still have stock, just keep us posted.",
              "The {product} delivery for order {order} moved by seven days. No impact on production, an update is enough.",
              "Our shipment for order {order} is late by a week. We have cover, so a status note is all we need.",
              "Order {order} is delayed. We are fine for stock, just let us know when it ships.")),
    Scenario("hard-angry-invoice", "billing", "P4", "negative", False, False,
             "Angry request for an invoice copy",
             ("I have asked three times for a copy of invoice {order} and nobody answers. It is a PDF, not a moon landing.",
              "Three requests for invoice {order} and still no reply. This is a document request, not an emergency.",
              "Why is it so hard to get invoice {order} resent? I have been asking all week.",
              "Invoice {order} has been requested three times now. It is just a PDF, please send it.")),
    Scenario("hard-cancel-refund", "cancellation", "P3", "negative", False, True,
             "Immediate cancellation with refund request",
             ("Cancel the {product} subscription immediately and refund the unused months. We already signed with another vendor.",
              "We want the {product} plan cancelled and the remaining months refunded. A new supplier is already in place.",
              "Please terminate the {product} subscription now and credit the unused term. We have moved to another vendor.",
              "Cancel our {product} subscription and refund the balance. Migration to the replacement is done.")),
    Scenario("hard-p2-calm", "technical", "P2", "neutral", False, True,
             "Polite report of a blocked line",
             ("No drama, but the {product} at {site} stopped at 05:00 and the line is blocked. Could someone take a look when you get a chance?",
              "The {product} on {site} has stopped and we cannot run the line. No rush, but it is fully blocked.",
              "Our {product} halted this morning at {site}. The cell has no output; whenever you can get to it.",
              "Line stopped at {site} because the {product} will not start. We are patient, but production is blocked.")),
    Scenario("hard-eval-mixed-language", "technical", "P2", "negative", True, True,
             "French greeting with a blocked line",
             ("Bonjour, the {product} at {site} is down since this morning and production stopped. Please call us.",
              "Hello and bonjour, {product} offline at {site} since 05:00, the line is stopped. Merci.",
              "Bonjour, our {product} on {site} is not starting and the line is blocked. Could you call?",
              "Bonjour, the {product} at {site} has been down all morning. Production is stopped."),
             eval_only=True),
    Scenario("hard-eval-payment-lockout", "billing", "P2", "negative", True, False,
             "Failed payment locked out the whole team",
             ("The payment for our subscription failed and the whole team is locked out of the portal. We cannot work.",
              "Our subscription payment was rejected and now none of the team can access the portal.",
              "A failed card payment locked every user out of the portal. We cannot do anything until it is restored.",
              "The portal says the subscription payment failed and the entire team is locked out.",
             ),
             eval_only=True),
    Scenario("hard-eval-late-cancel", "cancellation", "P3", "negative", False, True,
             "Cancelling a late order",
             ("We want to cancel the order because it is late and we sourced locally instead.",
              "Please cancel the {product} order. It was late, so we bought locally.",
              "Cancel our late order for the {product}; we sourced the part locally.",
              "The {product} order is late, so we are cancelling it and sourcing locally.",
             ),
             eval_only=True),
    Scenario("hard-eval-interlock", "technical", "P1", "negative", True, True,
             "Interlock failure flagged by safety officer",
             ("Our safety officer flagged that the {product} interlock does not engage. We have locked it out and need an urgent inspection.",
              "The interlock on our {product} failed its test and safety has isolated the unit. We need an engineer urgently.",
              "A safety inspection found the {product} interlock inactive. The unit is locked out until it is checked.",
              "The {product} guard interlock is not working. Safety has stopped the line and wants an urgent visit.",
             ),
             eval_only=True),
)


def _validate() -> None:
    for sc in CLEAN + HARD:
        assert sc.priority in ("P1", "P2", "P3", "P4"), sc.id
        assert sc.sentiment in ("negative", "neutral", "positive"), sc.id
        assert sc.category in ("billing", "technical", "account", "shipping", "cancellation"), sc.id
        assert len(sc.variants) == 4, sc.id
        if sc.priority == "P1":
            assert sc.escalate, f"{sc.id}: P1 must escalate"
        if sc.sentiment == "negative" and sc.priority == "P2":
            assert sc.escalate, f"{sc.id}: negative P2 must escalate"
        for v in sc.variants:
            if not sc.product:
                assert "{product}" not in v, f"{sc.id}: unexpected product placeholder"
        if sc.product:
            assert any("{product}" in v for v in sc.variants), f"{sc.id}: no product named"


def _slot_combos(rng: random.Random) -> list[dict[str, str]]:
    combos = []
    for customer in CUSTOMERS:
        for site in SITES:
            combos.append({
                "customer": customer,
                "site": site,
                "order": str(rng.randint(10000, 99999)),
                "amount": str(rng.choice((120, 240, 380, 460, 950, 1240))),
            })
    rng.shuffle(combos)
    return combos


def _candidates(scenarios, paraphrases, combos, product_cycle):
    out = []
    for sc in scenarios:
        for pi in paraphrases:
            for i, combo in enumerate(combos):
                slots = dict(combo)
                slots["product"] = PRODUCTS[(i + product_cycle + len(sc.id)) % len(PRODUCTS)]
                text = sc.variants[pi].format(**slots)
                target = {
                    "category": sc.category,
                    "priority": sc.priority,
                    "sentiment": sc.sentiment,
                    "escalate": sc.escalate,
                    "product": slots["product"] if "{product}" in sc.variants[pi] else None,
                    "summary": sc.summary.format(**slots) if "{" in sc.summary else sc.summary,
                }
                out.append({"id": f"{sc.id}-p{pi}-{i}", "ticket": text, "target": target})
    return out


def _take(cands, n, seed):
    rng = random.Random(seed)
    rng.shuffle(cands)
    seen, out = set(), []
    for c in cands:
        if c["ticket"] in seen:
            continue
        seen.add(c["ticket"])
        out.append(c)
        if len(out) == n:
            break
    if len(out) < n:
        raise RuntimeError(f"wanted {n} unique tickets, built {len(out)}")
    return sorted(out, key=lambda c: c["id"])


def build(seed: int = 20) -> dict[str, list[dict]]:
    _validate()
    combos = _slot_combos(random.Random(seed))
    clean_train = _candidates(CLEAN, (0, 1), combos, product_cycle=0)
    clean_eval = _candidates(CLEAN, (2, 3), combos[::-1], product_cycle=1)
    hard_shared = [s for s in HARD if not s.eval_only]
    hard_only = [s for s in HARD if s.eval_only]
    hard_train = _candidates(hard_shared, (0, 1), combos, product_cycle=2)
    hard_eval = _candidates(hard_shared, (2, 3), combos[::-1], product_cycle=3)
    held_out = _candidates(hard_only, (0, 1, 2, 3), combos, product_cycle=4)

    return {
        "train-v1": _take(clean_train, 320, seed + 1),
        "train-v2": _take(clean_train, 240, seed + 2) + _take(hard_train, 96, seed + 3),
        "eval": _take(clean_eval, 80, seed + 4) + _take(hard_eval, 80, seed + 5) + _take(held_out, 40, seed + 6),
    }


def write(out_dir: Path, seed: int = 20) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, rows in build(seed).items():
        path = out_dir / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[name] = len(rows)
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="finetune/data")
    ap.add_argument("--seed", type=int, default=20)
    args = ap.parse_args()
    counts = write(Path(args.out), args.seed)
    for name, n in counts.items():
        print(f"{name:10} {n:4} rows")


if __name__ == "__main__":
    main()
