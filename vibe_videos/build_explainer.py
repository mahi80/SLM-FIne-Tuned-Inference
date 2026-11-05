#!/usr/bin/env python3
"""Build the customer-facing SLM Serving Fabric explainer.

Scenes are drawn with Pillow, narrated with edge-tts, and muxed with ffmpeg.
Set STILLS=1 to write one PNG per scene instead of the full video.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".build"
OUT = ROOT / "slm-platform-explainer.mp4"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

W, H, FPS = 1280, 720, 30
FONT_DIR = Path(r"C:\Windows\Fonts") if os.name == "nt" else Path("/mnt/c/Windows/Fonts")
SEGOE_BOLD = "segoeuib.ttf"
SEGOE_SEMI = "seguisb.ttf"
SEGOE_REG = "segoeui.ttf"
SEGOE_LIGHT = "segoeuil.ttf"
SEGOE_MONO = "consola.ttf"

BG_TOP = (6, 12, 24)
BG_BOT = (10, 25, 47)
CARD = (17, 30, 52)
CARD_2 = (21, 38, 66)
BORDER = (48, 72, 112)
WHITE = (238, 244, 255)
MUTED = (150, 168, 198)
CYAN = (62, 216, 255)
BLUE = (92, 148, 255)
AMBER = (255, 182, 72)
GREEN = (61, 220, 151)
RED = (255, 96, 112)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


F_SMALL = font(SEGOE_SEMI, 15)
F_TINY = font(SEGOE_REG, 14)
F_SUB = font(SEGOE_REG, 24)
F_H2 = font(SEGOE_BOLD, 50)
F_H3 = font(SEGOE_SEMI, 26)
F_CARD = font(SEGOE_SEMI, 21)
F_CARD_S = font(SEGOE_REG, 17)
F_BIG = font(SEGOE_BOLD, 96)
F_URL = font(SEGOE_MONO, 22)


def col(rgb, a: float = 1.0):
    return (*rgb, max(0, min(255, int(round(255 * a)))))


def rv(t: float, start: float, dur: float = 0.5) -> float:
    if t <= start:
        return 0.0
    x = min(1.0, (t - start) / dur)
    return 1 - (1 - x) ** 3


def make_bg(accent) -> Image.Image:
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        f = y / (H - 1)
        c = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * f) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, W, 64):
        gd.line([(x, 0), (x, H)], fill=(255, 255, 255, 7))
    for y in range(0, H, 64):
        gd.line([(0, y), (W, y)], fill=(255, 255, 255, 7))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gld = ImageDraw.Draw(glow)
    gld.ellipse((W - 560, -300, W + 300, 360), fill=col(accent, 0.10))
    gld.ellipse((-320, H - 260, 420, H + 260), fill=col(BLUE, 0.06))
    grid = Image.alpha_composite(grid, glow)
    img = Image.alpha_composite(img.convert("RGBA"), grid)
    return img.convert("RGB")


BG_CACHE: dict[tuple, Image.Image] = {}


def bg_for(accent):
    key = tuple(accent)
    if key not in BG_CACHE:
        BG_CACHE[key] = make_bg(accent)
    return BG_CACHE[key]


def panel(d, box, accent=None, a=1.0, fill=CARD, radius=18, width=2):
    d.rounded_rectangle(
        box,
        radius=radius,
        fill=col(fill, 0.88 * a),
        outline=col(accent or BORDER, (0.55 if accent else 0.9) * a),
        width=width,
    )


def kicker(d, text, accent, a=1.0):
    w = d.textlength(text, font=F_SMALL)
    d.rounded_rectangle((90, 62, 90 + w + 40, 100), radius=19, fill=col(CARD, 0.95 * a),
                        outline=col(accent, 0.45 * a))
    d.ellipse((104, 75, 114, 85), fill=col(accent, a))
    d.text((126, 72), text, font=F_SMALL, fill=col(accent, a))


def title(d, lines, y=118, size=50, color=WHITE, a=1.0):
    f = font(SEGOE_BOLD, size)
    for line in lines:
        d.text((90, y), line, font=f, fill=col(color, a))
        y += int(size * 1.18)


def subtitle(d, lines, y, size=24, color=MUTED, a=1.0):
    f = font(SEGOE_REG, size)
    for line in lines:
        d.text((90, y), line, font=f, fill=col(color, a))
        y += int(size * 1.42)


def arrow(d, p1, p2, color=CYAN, w=3, a=1.0, head=13):
    d.line([p1, p2], fill=col(color, a), width=w)
    ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    for s in (-0.55, 0.55):
        d.line([p2, (p2[0] - head * math.cos(ang + s), p2[1] - head * math.sin(ang + s))],
               fill=col(color, a), width=w)


def check(d, x, y, r, color=GREEN, a=1.0):
    d.ellipse((x - r, y - r, x + r, y + r), fill=col((9, 24, 40), a), outline=col(color, a), width=4)
    d.line([(x - r * 0.45, y), (x - r * 0.1, y + r * 0.4), (x + r * 0.5, y - r * 0.35)],
           fill=col(color, a), width=4, joint="curve")


def pill(d, x, y, text, color=CYAN, a=1.0, f=None):
    f = f or F_SMALL
    w = d.textlength(text, font=f)
    d.rounded_rectangle((x, y, x + w + 26, y + 30), radius=15, fill=col(color, 0.12 * a),
                        outline=col(color, 0.45 * a))
    d.text((x + 13, y + 6), text, font=f, fill=col(color, a))
    return x + w + 26


def line_text(d, xy, text, f, color=WHITE, a=1.0, anchor=None):
    d.text(xy, text, font=f, fill=col(color, a), anchor=anchor)


def footer(img, idx, n, progress):
    d = ImageDraw.Draw(img)
    d.line([(0, H - 4), (int(W * progress), H - 4)], fill=col(CYAN), width=4)
    d.line([(int(W * progress), H - 4), (W, H - 4)], fill=col((38, 56, 88)), width=4)
    d.text((90, H - 36), "SLM SERVING FABRIC", font=F_TINY, fill=col(MUTED, 0.75))
    d.text((W - 90, H - 36), f"{idx + 1:02d} / {n:02d}", font=F_TINY, fill=col(MUTED, 0.75), anchor="ra")


# ---------------------------------------------------------------- scenes

def s1(d, t, dur):
    kicker(d, "PRIVATE LLM SERVING", CYAN)
    title(d, ["One narrow job.", "No frontier bill."], y=132, size=58)
    subtitle(d, ["Private AI, inside your perimeter — on hardware you control."], y=286)

    a1 = rv(t, 0.7)
    panel(d, (90, 420, 400, 600), CYAN, a1)
    d.ellipse((116, 448, 156, 488), fill=col(CYAN, 0.18 * a1), outline=col(CYAN, 0.6 * a1), width=2)
    d.text((136, 468), "T", font=F_H3, fill=col(CYAN, a1), anchor="mm")
    for i, wd in enumerate((190, 220, 150)):
        d.rounded_rectangle((176, 452 + i * 26, 176 + wd, 460 + i * 26), radius=4, fill=col(MUTED, 0.35 * a1))
    pill(d, 116, 552, "support ticket", AMBER, a1)

    a2 = rv(t, 1.0)
    arrow(d, (410, 510), (520, 510), CYAN, 3, a2)
    panel(d, (530, 452, 780, 568), CYAN, a2)
    d.text((655, 478), "1–8B model", font=F_H3, fill=col(WHITE, a2), anchor="ma")
    d.text((655, 512), "fine-tuned for one task", font=F_CARD_S, fill=col(MUTED, a2), anchor="ma")

    a3 = rv(t, 1.3)
    arrow(d, (790, 510), (900, 510), CYAN, 3, a3)
    panel(d, (910, 420, 1190, 600), GREEN, a3)
    check(d, 1050, 472, 26, GREEN, a3)
    d.text((1050, 520), "Priority: P1", font=F_H3, fill=col(WHITE, a3), anchor="ma")
    d.text((1050, 556), "triage output", font=F_CARD_S, fill=col(MUTED, a3), anchor="ma")


def s2(d, t, dur):
    kicker(d, "CUSTOMER NEED", CYAN)
    title(d, ["A small model, fine-tuned", "for the job."], y=124, size=48)
    subtitle(d, ["Most of the quality of a frontier model — without paying for one."], y=252)

    a = rv(t, 0.6)
    panel(d, (90, 330, 400, 560), BORDER, a)
    d.text((116, 352), "Support ticket", font=F_CARD, fill=col(WHITE, a))
    for i, wd in enumerate((240, 210, 250, 170)):
        d.rounded_rectangle((116, 398 + i * 30, 116 + wd, 406 + i * 30), radius=4, fill=col(MUTED, 0.32 * a))
    pill(d, 116, 508, "customer data", AMBER, a)

    arrow(d, (412, 445), (516, 445), CYAN, 3, a)
    d.text((464, 412), "fine-tune", font=F_TINY, fill=col(CYAN, a), anchor="ma")

    panel(d, (528, 372, 792, 518), CYAN, a)
    d.text((660, 396), "1–8B · QLoRA", font=F_H3, fill=col(WHITE, a), anchor="ma")
    d.text((660, 434), "trained on your task", font=F_CARD_S, fill=col(MUTED, a), anchor="ma")
    pill(d, 596, 464, "runs on one GPU", CYAN, a)

    arrow(d, (804, 445), (908, 445), CYAN, 3, a)

    panel(d, (920, 330, 1190, 560), GREEN, a)
    d.text((950, 352), "Triage output", font=F_CARD, fill=col(WHITE, a))
    check(d, 1055, 446, 28, GREEN, a)
    d.text((1055, 492), "Priority: P1", font=F_H3, fill=col(WHITE, a), anchor="ma")
    d.text((1055, 528), "schema-checked", font=F_CARD_S, fill=col(MUTED, a), anchor="ma")

    # quality bar
    y = 610
    d.text((90, y - 30), "quality on this one task", font=F_TINY, fill=col(MUTED, 0.9))
    d.rounded_rectangle((90, y, 1190, y + 12), radius=6, fill=col(CARD_2, 0.9))
    d.rounded_rectangle((90, y, 90 + int(1010 * 0.88), y + 12), radius=6, fill=col(CYAN, 0.85))
    d.line([(90 + 1010, y - 6), (90 + 1010, y + 18)], fill=col(AMBER), width=2)
    d.text((90 + 1010, y - 26), "frontier model", font=F_TINY, fill=col(AMBER), anchor="ra")


def s3(d, t, dur):
    kicker(d, "COST", AMBER)
    title(d, ["A fraction of the cost."], y=132, size=56)
    subtitle(d, ["Same task. Smaller model. Lower bill."], y=222)

    a = rv(t, 0.5)
    d.text((90, 310), "cost per 1M output tokens", font=F_SMALL, fill=col(MUTED, a))
    d.text((90, 356), "Frontier API", font=F_CARD, fill=col(WHITE, a))
    d.rounded_rectangle((360, 352, 1190, 396), radius=10, fill=col(AMBER, 0.22 * a), outline=col(AMBER, 0.6 * a))
    d.rounded_rectangle((360, 352, 1190, 396), radius=10, fill=col(AMBER, 0.33 * a))
    d.text((1150, 374), "baseline", font=F_TINY, fill=col(WHITE, 0.9 * a), anchor="rm")

    a2 = rv(t, 1.0)
    d.text((90, 476), "Fine-tuned SLM", font=F_CARD, fill=col(WHITE, a2))
    d.rounded_rectangle((360, 472, 1190, 516), radius=10, outline=col(BORDER, 0.6 * a2))
    d.rounded_rectangle((360, 472, 360 + 120, 516), radius=10, fill=col(CYAN, 0.85 * a2))
    d.text((500, 494), "target", font=F_TINY, fill=col(CYAN, a2), anchor="lm")

    a3 = rv(t, 1.5)
    d.text((90, 590), "The ownership cost model in the repo turns this into dollars per million tokens.",
           font=F_CARD_S, fill=col(MUTED, a3))
    d.text((90, 622), "No per-token frontier bill. The hardware is yours, or rented by the hour.",
           font=F_CARD_S, fill=col(MUTED, 0.8 * a3))


def s4(d, t, dur):
    kicker(d, "THE CATCH", BLUE)
    title(d, ["One decision,", "for every request."], y=118, size=48)
    subtitle(d, ["Model, tier, quantisation and residency — resolved inside the perimeter."], y=244)

    a = rv(t, 0.5)
    d.rounded_rectangle((90, 300, 1190, 616), radius=26, outline=col(BLUE, 0.35 * a), width=2)
    d.text((112, 312), "customer perimeter", font=F_TINY, fill=col(BLUE, 0.85 * a))

    a1 = rv(t, 0.8)
    panel(d, (120, 420, 300, 500), BORDER, a1)
    d.text((210, 460), "request", font=F_CARD, fill=col(WHITE, a1), anchor="mm")

    arrow(d, (312, 460), (396, 460), BLUE, 3, a1)
    a2 = rv(t, 1.1)
    d.polygon([(470, 384), (548, 460), (470, 536), (392, 460)], fill=col(CARD_2, 0.95 * a2),
              outline=col(CYAN, 0.8 * a2))
    d.text((470, 460), "router", font=F_H3, fill=col(WHITE, a2), anchor="mm")

    a3 = rv(t, 1.4)
    arrow(d, (560, 460), (632, 460), BLUE, 3, a3)
    chips = [("model", 646, 358), ("tier", 854, 358), ("quantisation", 646, 452), ("residency", 854, 452)]
    for i, (label, x, y) in enumerate(chips):
        aa = rv(t, 1.5 + i * 0.12)
        panel(d, (x, y, x + 186, y + 62), CYAN, aa, radius=14)
        d.text((x + 93, y + 31), label, font=F_CARD_S, fill=col(WHITE, aa), anchor="mm")

    a4 = rv(t, 2.1)
    arrow(d, (1052, 460), (1100, 460), GREEN, 3, a4)
    check(d, 1140, 460, 26, GREEN, a4)
    d.text((1140, 504), "SLO met", font=F_TINY, fill=col(GREEN, a4), anchor="ma")

    a5 = rv(t, 2.4)
    d.text((640, 560), "the same request may route differently as load, cost and residency change",
           font=F_TINY, fill=col(MUTED, 0.9 * a5))


def s5(d, t, dur):
    kicker(d, "THE EASY 20%", RED)
    title(d, ["Most stacks stop here."], y=126, size=54)
    subtitle(d, ["One model server, one API key — and call it private."], y=214)

    a = rv(t, 0.5)
    panel(d, (90, 300, 600, 600), BORDER, a)
    d.text((120, 330), "vLLM", font=F_H3, fill=col(WHITE, a))
    d.text((120, 368), "single tier", font=F_CARD_S, fill=col(MUTED, a))
    d.rounded_rectangle((120, 420, 570, 500), radius=12, fill=col(CARD_2, 0.9 * a), outline=col(BORDER, a))
    d.text((140, 444), "POST /v1/chat/completions", font=F_URL, fill=col(CYAN, 0.9 * a))
    d.text((140, 474), "Authorization: Bearer ••••", font=F_TINY, fill=col(MUTED, a))
    pill(d, 120, 532, "API key", RED, a)

    a2 = rv(t, 0.9)
    d.text((700, 296), "solved", font=F_SMALL, fill=col(MUTED, a2))
    d.rounded_rectangle((700, 330, 1190, 362), radius=8, fill=col(RED, 0.25 * a2), outline=col(RED, 0.5 * a2))
    d.rounded_rectangle((700, 330, 700 + 98, 362), radius=8, fill=col(RED, 0.75 * a2))
    d.text((820, 346), "20%", font=F_H3, fill=col(RED, a2), anchor="lm")

    a3 = rv(t, 1.4)
    d.text((700, 410), "still to build", font=F_SMALL, fill=col(MUTED, a3))
    d.rounded_rectangle((700, 444, 1190, 476), radius=8, outline=col(BORDER, 0.8 * a3))
    d.text((1160, 460), "80%", font=F_H3, fill=col(MUTED, a3), anchor="rm")
    d.text((700, 512), "tier routing · tenancy · releases · audit", font=F_CARD_S, fill=col(WHITE, 0.85 * a3))
    d.text((700, 552), "This repo builds the other 80%.", font=F_CARD_S, fill=col(CYAN, a3))


def s6(d, t, dur):
    kicker(d, "THE OTHER 80% · 1", CYAN)
    title(d, ["GPU or CPU? Decide with numbers."], y=124, size=48)
    subtitle(d, ["Latency, cost and concurrency — evaluated per request, not by vibes."], y=212)

    a = rv(t, 0.5)
    panel(d, (90, 286, 620, 606), CYAN, a)
    d.text((120, 312), "GPU tier", font=F_H3, fill=col(WHITE, a))
    d.polygon([(138, 404), (158, 404), (146, 432), (156, 432), (132, 468), (140, 434), (130, 434)],
              fill=col(CYAN, 0.9 * a))
    for i, label in enumerate(("lowest latency", "highest concurrency", "highest cost")):
        color = GREEN if i < 2 else AMBER
        d.ellipse((180, 410 + i * 46, 190, 420 + i * 46), fill=col(color, a))
        d.text((204, 404 + i * 46), label, font=F_CARD_S, fill=col(WHITE, 0.9 * a))
    d.text((120, 552), "full-precision attention at speed", font=F_TINY, fill=col(MUTED, a))

    a2 = rv(t, 0.9)
    panel(d, (660, 286, 1190, 606), AMBER, a2)
    d.text((690, 312), "CPU tier", font=F_H3, fill=col(WHITE, a2))
    d.rounded_rectangle((708, 408, 750, 450), radius=6, outline=col(AMBER, 0.9 * a2), width=2)
    for i in range(4):
        d.line([(708 + i * 11, 402), (708 + i * 11, 408)], fill=col(AMBER, 0.9 * a2), width=2)
        d.line([(708 + i * 11, 450), (708 + i * 11, 456)], fill=col(AMBER, 0.9 * a2), width=2)
    for i, label in enumerate(("lowest cost when idle", "handles light traffic", "lower concurrency")):
        color = GREEN if i < 2 else AMBER
        d.ellipse((772, 410 + i * 46, 782, 420 + i * 46), fill=col(color, a2))
        d.text((796, 404 + i * 46), label, font=F_CARD_S, fill=col(WHITE, 0.9 * a2))
    d.text((690, 552), "the residency-gated standby", font=F_TINY, fill=col(MUTED, a2))

    a3 = rv(t, 1.4)
    pill(d, 470, 628, "policy decides per request", CYAN, a3)


def s7(d, t, dur):
    kicker(d, "THE OTHER 80% · 2", AMBER)
    title(d, ["Isolation is not a feature flag."], y=124, size=48)
    subtitle(d, ["Tenants, data residency and air-gapped sites stay sealed."], y=212)

    a = rv(t, 0.5)
    zones = [("Tenant A", 90, BLUE), ("Tenant B", 490, CYAN), ("Air-gapped site", 890, AMBER)]
    for label, x, accent in zones:
        aa = rv(t, 0.5 + (x - 90) / 900)
        if label == "Air-gapped site":
            d.rounded_rectangle((x, 330, x + 300, 560), radius=18, fill=col(CARD, 0.75 * aa),
                                outline=col(accent, 0.8 * aa), width=3)
        else:
            panel(d, (x, 330, x + 300, 560), accent, aa)
        d.text((x + 150, 360), label, font=F_H3, fill=col(WHITE, aa), anchor="ma")
        for i in range(3):
            d.ellipse((x + 34, 420 + i * 44, x + 46, 432 + i * 44), fill=col(accent, 0.7 * aa))
            d.rounded_rectangle((x + 60, 424 + i * 44, x + 264, 430 + i * 44), radius=3,
                                fill=col(MUTED, 0.3 * aa))

    a2 = rv(t, 0.9)
    d.polygon([(246, 286), (256, 300), (266, 286), (256, 291)], fill=col(AMBER, a2))
    d.text((276, 284), "EU residency", font=F_TINY, fill=col(AMBER, a2), anchor="lm")

    a3 = rv(t, 1.3)
    arrow(d, (402, 445), (478, 445), RED, 4, a3)
    d.line([(428, 433), (450, 457)], fill=col(RED, a3), width=5)
    d.line([(450, 433), (428, 457)], fill=col(RED, a3), width=5)
    d.text((440, 470), "denied", font=F_TINY, fill=col(RED, a3), anchor="ma")

    a4 = rv(t, 1.6)
    d.line([(1040, 560), (1040, 596)], fill=col(AMBER, 0.7 * a4), width=2)
    d.text((1040, 604), "no egress", font=F_TINY, fill=col(AMBER, a4), anchor="ma")

    a5 = rv(t, 1.9)
    d.text((90, 636), "Policy and admission rules refuse the route before a token is generated.",
           font=F_TINY, fill=col(MUTED, 0.9 * a5))


def s8(d, t, dur):
    kicker(d, "THE OTHER 80% · 3", GREEN)
    title(d, ["Release without a redeploy."], y=124, size=48)
    subtitle(d, ["Gate, canary, promote — or roll back. The base model keeps serving."], y=212)

    steps = [("Gate", 90, GREEN), ("Canary 10%", 390, CYAN), ("Promote", 690, BLUE)]
    for i, (label, x, accent) in enumerate(steps):
        aa = rv(t, 0.6 + i * 0.45)
        panel(d, (x, 366, x + 220, 486), accent, aa)
        d.text((x + 110, 410), label, font=F_H3, fill=col(WHITE, aa), anchor="ma")
        d.text((x + 110, 448), ["tests + eval", "sticky per tenant", "swap weights"][i],
               font=F_CARD_S, fill=col(MUTED, aa), anchor="ma")
        if i:
            arrow(d, (x - 74, 426), (x - 16, 426), accent, 3, rv(t, 0.45 + i * 0.45))

    a = rv(t, 2.0)
    d.line([(800, 496), (800, 566), (200, 566), (200, 496)], fill=col(AMBER, a), width=3)
    ang = math.atan2(-70, 0)
    d.polygon([(200, 496), (190, 514), (210, 514)], fill=col(AMBER, a))
    d.text((500, 576), "rollback in one step", font=F_TINY, fill=col(AMBER, a), anchor="ma")

    a2 = rv(t, 2.3)
    pill(d, 968, 400, "hot-loaded adapters", CYAN, a2)
    d.text((968, 446), "no pod restart", font=F_TINY, fill=col(MUTED, a2))


def s9(d, t, dur):
    kicker(d, "THE OTHER 80% · 4", BLUE)
    title(d, ["Every request, explainable."], y=124, size=48)
    subtitle(d, ["An auditor can see why it went where it went."], y=212)

    a = rv(t, 0.5)
    panel(d, (90, 286, 790, 626), BORDER, a)
    d.text((116, 306), "hash-chained audit log", font=F_SMALL, fill=col(CYAN, a))
    rows = [
        ("12:04:11", "acme", "slm-triage", "GPU", "eu-west"),
        ("12:04:12", "globex", "slm-small", "CPU", "eu-west"),
        ("12:04:13", "acme", "slm-triage-v2", "GPU", "eu-west"),
    ]
    for i, (ts, tenant, alias, tier, region) in enumerate(rows):
        aa = rv(t, 0.8 + i * 0.3)
        y = 344 + i * 84
        d.rounded_rectangle((116, y, 764, y + 66), radius=10, fill=col(CARD_2, 0.8 * aa),
                            outline=col(BORDER, 0.8 * aa))
        d.text((134, y + 14), ts, font=F_TINY, fill=col(MUTED, aa))
        d.text((134, y + 36), tenant, font=F_CARD_S, fill=col(WHITE, aa))
        d.text((300, y + 24), alias, font=F_CARD_S, fill=col(CYAN, aa))
        d.text((540, y + 24), tier, font=F_CARD_S, fill=col(WHITE, aa))
        d.text((660, y + 24), region, font=F_CARD_S, fill=col(MUTED, aa))
        if i < 2:
            d.line([(728, y + 66), (728, y + 84)], fill=col(CYAN, 0.5 * aa), width=2)
            d.ellipse((724, y + 72, 732, y + 80), fill=col(CYAN, 0.7 * aa))

    a2 = rv(t, 2.0)
    d.ellipse((900, 330, 980, 410), outline=col(CYAN, 0.9 * a2), width=4)
    d.line([(960, 392), (1004, 436)], fill=col(CYAN, 0.9 * a2), width=6)
    for i, label in enumerate(("alias", "tier", "region")):
        yy = 470 + i * 44
        d.text((900, yy), label, font=F_CARD_S, fill=col(MUTED, a2))
        arrow(d, (980, yy + 10), (1040, yy + 10), CYAN, 2, a2)
        d.text((1052, yy), ["signed policy", "load + cost", "residency"][i], font=F_CARD_S,
               fill=col(WHITE, 0.9 * a2))
    d.text((900, 596), "one record per request", font=F_TINY, fill=col(MUTED, 0.9 * a2))


def s_ops(d, t, dur):
    kicker(d, "THE OTHER 80% · 5", GREEN)
    title(d, ["Two pools. One health check."], y=124, size=48)
    subtitle(d, ["Pin the GPU tier, keep the router off it, and prove it from the cluster."], y=196)

    a = rv(t, 0.5)
    panel(d, (90, 258, 600, 432), CYAN, a)
    d.text((116, 274), "GPU pool", font=F_H3, fill=col(WHITE, a))
    pill(d, 368, 268, "nvidia.com/gpu taint", CYAN, a)
    d.rounded_rectangle((116, 318, 574, 408), radius=12, fill=col(CARD_2, 0.85 * a),
                        outline=col(BORDER, 0.9 * a))
    d.text((140, 334), "vllm-gpu", font=F_CARD, fill=col(WHITE, a))
    d.text((140, 368), "nodeSelector · toleration · 1 GPU", font=F_CARD_S, fill=col(MUTED, a))

    a2 = rv(t, 0.9)
    panel(d, (680, 258, 1190, 432), AMBER, a2)
    d.text((706, 274), "CPU pool", font=F_H3, fill=col(WHITE, a2))
    pill(d, 878, 268, "slm.fabric/pool=cpu", AMBER, a2)
    for i, (name, note) in enumerate((("gateway", "preferred affinity"),
                                      ("tiersim-cpu · tiersim-cloud", "modelled tiers"))):
        y = 318 + i * 46
        d.rounded_rectangle((706, y, 1164, y + 38), radius=10, fill=col(CARD_2, 0.8 * a2),
                            outline=col(BORDER, 0.9 * a2))
        d.text((722, y + 9), name, font=F_CARD_S, fill=col(WHITE, a2))
        d.text((1148, y + 10), note, font=F_TINY, fill=col(MUTED, a2), anchor="ra")

    a3 = rv(t, 1.3)
    d.text((640, 446), "preference keeps a single node schedulable; the taint does the enforcing",
           font=F_TINY, fill=col(MUTED, 0.9 * a3), anchor="ma")

    a4 = rv(t, 1.6)
    panel(d, (90, 472, 1190, 630), GREEN, a4)
    d.text((116, 486), "$ make cluster-health", font=F_URL, fill=col(GREEN, a4))
    rows = [
        "PASS  cluster       3 nodes ready",
        "PASS  gpu-capacity  1 allocatable nvidia.com/gpu",
        "PASS  endpoints     4/4 services have ready addresses",
        "PASS  gateway       policy v3 · 0 breaker(s) open",
    ]
    for i, text in enumerate(rows):
        aa = rv(t, 1.9 + i * 0.15)
        d.text((116, 520 + i * 28), text, font=F_URL, fill=col(WHITE, 0.9 * aa))


def s10(d, t, dur):
    a = rv(t, 0.3)
    d.text((W // 2, 150), "SLM SERVING FABRIC", font=font(SEGOE_BOLD, 72), fill=col(WHITE, a), anchor="ma")
    d.text((W // 2, 250), "A runnable reference platform for private LLM serving.",
           font=font(SEGOE_REG, 28), fill=col(MUTED, a), anchor="ma")

    a2 = rv(t, 0.6)
    chips = ["signed releases", "digest-pinned images", "tenant isolation",
             "hash-chained audit", "cluster health"]
    total = sum(d.textlength(c, font=F_CARD_S) + 44 for c in chips) + 16 * (len(chips) - 1)
    x = (W - total) // 2
    for c in chips:
        w = d.textlength(c, font=F_CARD_S) + 44
        d.rounded_rectangle((x, 320, x + w, 366), radius=23, fill=col(CARD_2, 0.9 * a2),
                            outline=col(CYAN, 0.35 * a2))
        d.text((x + w / 2, 343), c, font=F_CARD_S, fill=col(CYAN, a2), anchor="mm")
        x += w + 16

    a3 = rv(t, 0.9)
    centers = [525, 655, 785]
    d.rounded_rectangle((580, 372, 700, 410), radius=10, fill=col(CARD_2, 0.95 * a3),
                        outline=col(BORDER, a3))
    d.text((640, 391), "gateway", font=F_TINY, fill=col(WHITE, 0.85 * a3), anchor="mm")
    for cx in centers:
        d.line([(640, 410), (cx, 430)], fill=col(BORDER, 0.9 * a3), width=2)
    for i, (label, accent) in enumerate((("GPU", CYAN), ("CPU", AMBER), ("cloud", BLUE))):
        bx = 470 + i * 130
        d.rounded_rectangle((bx, 430, bx + 110, 490), radius=12, fill=col(CARD, 0.9 * a3),
                            outline=col(accent, 0.5 * a3))
        d.text((bx + 55, 460), label, font=F_H3, fill=col(accent, a3), anchor="mm")

    a4 = rv(t, 1.2)
    d.text((W // 2, 560), "github.com/mahi80/SLM-FIne-Tuned-Inference", font=F_URL,
           fill=col(CYAN, a4), anchor="ma")
    d.text((W // 2, 600), "Every decision recorded as a reviewable ADR.", font=F_TINY,
           fill=col(MUTED, a4), anchor="ma")


SCENE_SPECS = [
    dict(key="intro", kicker="PRIVATE LLM SERVING", accent=CYAN, draw=s1,
         narration="Suppose you need a private AI model for one narrow job: triaging support tickets, or classifying documents."),
    dict(key="need", kicker="CUSTOMER NEED", accent=CYAN, draw=s2,
         narration="You don't need a frontier model. A one to eight billion parameter model, fine-tuned for that job, gets most of the quality."),
    dict(key="cost", kicker="COST", accent=AMBER, draw=s3,
         narration="At a fraction of the cost, on hardware you already own or rent."),
    dict(key="catch", kicker="THE CATCH", accent=BLUE, draw=s4,
         narration="But only if the platform can decide, for every request, which model, which tier, and which quantisation still meets the service level, inside your own perimeter."),
    dict(key="easy", kicker="THE EASY 20%", accent=RED, draw=s5,
         narration="Most private AI stacks stop at a single model server behind an API key. That is the easy twenty percent."),
    dict(key="tier", kicker="THE OTHER 80% · 1", accent=CYAN, draw=s6,
         narration="The hard part is choosing GPU or CPU by latency, cost and concurrency, not by guesswork."),
    dict(key="isolation", kicker="THE OTHER 80% · 2", accent=AMBER, draw=s7,
         narration="Keeping tenants, data residency and air-gapped sites from leaking into each other."),
    dict(key="release", kicker="THE OTHER 80% · 3", accent=GREEN, draw=s8,
         narration="Shipping a new model release through gates, canary and rollback, without a redeploy."),
    dict(key="audit", kicker="THE OTHER 80% · 4", accent=BLUE, draw=s9,
         narration="And proving to an auditor why every request went where it went."),
    dict(key="ops", kicker="THE OTHER 80% · 5", accent=GREEN, draw=s_ops,
         narration="In the cluster, the GPU tier is pinned to GPU nodes, everything else prefers CPU workers, and one command checks nodes, capacity, endpoints and breaker state."),
    dict(key="close", kicker="THE PLATFORM", accent=CYAN, draw=s10,
         narration="This repository builds that platform, and records every decision as a reviewable architecture decision record."),
]

VOICE = "en-US-AndrewNeural"
RATE = "+3%"
SR = 48000
LEAD = 0.45
TAIL = 0.75


def tts(scene) -> float:
    mp3 = BUILD / f"{scene['key']}.mp3"
    wav = BUILD / f"{scene['key']}.wav"
    if os.environ.get("REUSE_TTS") and wav.exists():
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / w.getframerate()
    for attempt in range(3):
        proc = subprocess.run(
            [sys.executable, "-m", "edge_tts", "--voice", VOICE, f"--rate={RATE}",
             "--text", scene["narration"], "--write-media", str(mp3)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and mp3.exists() and mp3.stat().st_size > 1000:
            break
    else:
        raise SystemExit(f"tts failed for {scene['key']}: {proc.stderr[-400:]}")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp3),
                    "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True)
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def build_audio(timeline):
    total = sum(s["dur"] for s in timeline)
    master = bytearray(int(total * SR) * 2)
    for scene in timeline:
        with wave.open(str(scene["wav"]), "rb") as w:
            data = w.readframes(w.getnframes())
        offset = int((scene["start"] + LEAD) * SR) * 2
        master[offset:offset + len(data)] = data
    out = BUILD / "voice.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(master))
    return out, total


def render_video(timeline, audio):
    total = sum(s["dur"] for s in timeline)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-s", f"{W}x{H}", "-pix_fmt", "rgb24",
        "-r", str(FPS), "-i", "-", "-i", str(audio),
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(OUT),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    n = len(timeline)
    elapsed = 0.0
    for idx, scene in enumerate(timeline):
        frames = int(round(scene["dur"] * FPS))
        for i in range(frames):
            t = i / FPS
            dur = scene["dur"]
            base = bg_for(scene["accent"]).copy().convert("RGBA")
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            scene["draw"](d, t, dur)
            fade_in = min(1.0, t / 0.5)
            fade_out = min(1.0, max(0.0, dur - t) / 0.35)
            alpha = min(fade_in, fade_out)
            if alpha < 1:
                al = layer.getchannel("A").point(lambda v: int(v * alpha))
                layer.putalpha(al)
            dy = int(14 * (1 - fade_in))
            base.alpha_composite(layer, (0, dy))
            img = base.convert("RGB")
            footer(img, idx, n, min(1.0, (elapsed + t) / total))
            proc.stdin.write(img.tobytes())
        elapsed += scene["dur"]
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")


def main():
    BUILD.mkdir(exist_ok=True)
    timeline = []
    start = 0.0
    for spec in SCENE_SPECS:
        nar = tts(spec)
        dur = max(4.2, LEAD + nar + TAIL)
        if spec["key"] == "close":
            dur += 1.0
        timeline.append({**spec, "nar": nar, "dur": dur, "start": start,
                         "wav": BUILD / f"{spec['key']}.wav"})
        start += dur
        print(f"{spec['key']:10s} narration {nar:5.2f}s  scene {dur:5.2f}s")

    if os.environ.get("STILLS"):
        for idx, scene in enumerate(timeline):
            img = bg_for(scene["accent"]).copy().convert("RGBA")
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            scene["draw"](ImageDraw.Draw(layer), scene["dur"] * 0.7, scene["dur"])
            img.alpha_composite(layer)
            out = BUILD / f"still_{idx + 1:02d}.png"
            img.convert("RGB").save(out)
            print(out)
        return

    audio, total = build_audio(timeline)
    print(f"video {total:.2f}s -> {OUT}")
    render_video(timeline, audio)
    print("done", OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
