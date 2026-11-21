"""
Red vs. Blue Cybersecurity Game  v2
===================================
Turn-based, educational strategy game. Every attack is an abstract dice roll with no real
attack techniques: the point is learning which *defence* counters which *threat*.

New in v2
  * Two players on ONE keyboard (hot-seat) with a hidden-choice hand-off screen
  * Budget mode: every move costs points; you get income each turn (cap applies)
  * Save / load games as JSON (including the random state, so a loaded game continues exactly)
  * PDF report with purpose, real-world mapping (ATT&CK/NIST/CIS), insights, workshop guide; rebuild from any save

Also: play Red or Blue vs an AI (Easy/Normal/Hard), 6 tactics per side, per-turn lessons.

Requirements:  pip install reportlab matplotlib       (tkinter ships with Python)
Run GUI:       python red_vs_blue_game_v2.py
Headless:      python red_vs_blue_game_v2.py --demo --turns 10 --seed 3 --budget --save game.json --out report.pdf
Report a save: python red_vs_blue_game_v2.py --report-from game.json --out report.pdf
"""
from __future__ import annotations

import argparse
import io
import json
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# Game data
# ---------------------------------------------------------------------------
BLUE_MOVES = [
    ("Network Monitoring",   "IDS/SIEM watching traffic for anomalies. A good generalist."),
    ("User Training",        "Awareness training so staff spot and report suspicious messages."),
    ("Access Controls",      "MFA, least privilege and verification procedures for sensitive requests."),
    ("Endpoint Protection",  "EDR / anti-malware on every device."),
    ("Patch Management",     "Fast, tested patching of known vulnerabilities."),
    ("DDoS Mitigation",      "Rate limiting, CDN scrubbing and upstream filtering."),
]
BLUE_COST = [1, 2, 2, 3, 3, 2]

RED_MOVES = [
    dict(name="Reconnaissance", sev=1, primary=0, secondary=[2],
         desc="Quietly map systems and staff to prepare later attacks.",
         blocked="Monitoring spotted unusual scanning early. Detection stops attacks before they start.",
         landed="Recon went unnoticed. Log review and alerting on scans would have exposed it."),
    dict(name="Phishing", sev=2, primary=1, secondary=[2],
         desc="Deceptive emails that try to harvest credentials or get a click.",
         blocked="A trained user reported the email. Awareness plus a report button is a strong control.",
         landed="A user fell for it. Training, email filtering and MFA reduce this risk."),
    dict(name="Social Engineering", sev=2, primary=2, secondary=[1],
         desc="Pretexting and impersonation to talk someone into granting access.",
         blocked="Caller-verification rules stopped the request. Never rely on trust alone.",
         landed="Someone was talked into helping. Out-of-band verification would have caught it."),
    dict(name="Malware Delivery", sev=3, primary=3, secondary=[0],
         desc="Get malicious software onto a device to gain a foothold.",
         blocked="Endpoint protection quarantined the file. Layered endpoint defence matters.",
         landed="Malware ran on a device. Up-to-date EDR and application allow-listing help."),
    dict(name="Exploit Vulnerability", sev=3, primary=4, secondary=[0],
         desc="Abuse a known weakness in unpatched software.",
         blocked="The system was already patched. Patching closes the door attackers rely on.",
         landed="An unpatched flaw was abused. Prioritise patches for internet-facing systems."),
    dict(name="DDoS", sev=2, primary=5, secondary=[0],
         desc="Flood a service with traffic to knock it offline.",
         blocked="Traffic scrubbing absorbed the flood. Plan capacity and mitigation in advance.",
         landed="The service was overwhelmed. Pre-arranged DDoS mitigation keeps services up."),
]
RED_COST = [1, 1, 2, 3, 3, 2]

P_PRIMARY, P_SECONDARY, P_OPEN = 0.20, 0.45, 0.75
N = 6
SIDES = ("red", "blue", "both")


def red_success_prob(red_idx: int, blue_idx: int) -> float:
    r = RED_MOVES[red_idx]
    if blue_idx == r["primary"]:
        return P_PRIMARY
    if blue_idx in r["secondary"]:
        return P_SECONDARY
    return P_OPEN


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
@dataclass
class TurnRecord:
    turn: int
    red: int
    blue: int
    prob: float
    roll: float
    success: bool
    red_pts: int
    blue_pts: int
    text: str
    lesson: str
    red_cost: int = 0
    blue_cost: int = 0
    red_left: int = 0
    blue_left: int = 0


@dataclass
class Game:
    side: str = "red"            # "red" / "blue" (human vs AI) or "both" (two humans)
    difficulty: str = "normal"   # AI level: easy / normal / hard
    turns: int = 8
    seed: int | None = None
    budget_mode: bool = False
    start_budget: int = 6
    income: int = 3
    cap: int = 10
    history: list[TurnRecord] = field(default_factory=list)
    red_score: int = 0
    blue_score: int = 0
    red_budget: int = 0
    blue_budget: int = 0
    started: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

    def __post_init__(self):
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}")
        self.rng = random.Random(self.seed)
        if not self.history and self.red_budget == 0 and self.blue_budget == 0:
            self.red_budget = self.blue_budget = self.start_budget

    # ---- state helpers ----
    @property
    def over(self) -> bool:
        return len(self.history) >= self.turns

    @property
    def turn_no(self) -> int:
        return len(self.history) + 1

    def cost(self, side: str, idx: int) -> int:
        return (RED_COST if side == "red" else BLUE_COST)[idx] if self.budget_mode else 0

    def affordable(self, side: str) -> list[int]:
        if not self.budget_mode:
            return list(range(N))
        bud = self.red_budget if side == "red" else self.blue_budget
        ok = [i for i in range(N) if self.cost(side, i) <= bud]
        return ok or [min(range(N), key=lambda i: self.cost(side, i))]

    # ---- AI opponents ----
    def ai_choose(self, ai_side: str, difficulty: str | None = None) -> int:
        d = difficulty or self.difficulty
        ok = self.affordable(ai_side)
        if d == "easy" or not self.history:
            return self.rng.choice(ok)
        if ai_side == "blue":
            freq = [0] * N
            for h in self.history:
                freq[h.red] += 1
            w = [1.0] * N
            boost = 1.5 if d == "normal" else 4.0
            for ridx, f in enumerate(freq):
                r = RED_MOVES[ridx]
                w[r["primary"]] += boost * f
                for s in r["secondary"]:
                    w[s] += 0.4 * boost * f
            wts = [w[i] if i in ok else 0 for i in range(N)]
            return self.rng.choices(range(N), weights=wts)[0]
        wins, tries = [0] * N, [0] * N
        for h in self.history:
            tries[h.red] += 1
            wins[h.red] += h.success
        rate = [(wins[i] + 1) / (tries[i] + 2) for i in range(N)]
        if d == "hard" and self.rng.random() > 0.15:
            return max(ok, key=lambda i: rate[i])
        p = 1 if d == "normal" else 3
        wts = [rate[i] ** p if i in ok else 0 for i in range(N)]
        return self.rng.choices(range(N), weights=wts)[0]

    # ---- one round ----
    def play_turn(self, human_move: int) -> TurnRecord:
        """Human vs AI turn."""
        if self.side == "both":
            raise RuntimeError("Use resolve(red, blue) in two-player mode")
        if self.side == "red":
            return self.resolve(human_move, self.ai_choose("blue"))
        return self.resolve(self.ai_choose("red"), human_move)

    def resolve(self, red: int, blue: int) -> TurnRecord:
        if self.over:
            raise RuntimeError("Game is over")
        rc, bc = self.cost("red", red), self.cost("blue", blue)
        if self.budget_mode and (rc > self.red_budget or bc > self.blue_budget):
            raise ValueError("Not enough budget for that move")
        self.red_budget -= rc
        self.blue_budget -= bc

        r = RED_MOVES[red]
        p = red_success_prob(red, blue)
        roll = self.rng.random()
        ok = roll < p
        if ok:
            rp, bp = r["sev"], 0
            text = f"{r['name']} succeeded against {BLUE_MOVES[blue][0]}."
            lesson = r["landed"]
        else:
            rp, bp = 0, 1 + (1 if blue == r["primary"] else 0)
            text = f"{BLUE_MOVES[blue][0]} stopped {r['name']}."
            lesson = r["blocked"]
        self.red_score += rp
        self.blue_score += bp
        rec = TurnRecord(self.turn_no, red, blue, p, roll, ok, rp, bp, text, lesson,
                         rc, bc, self.red_budget, self.blue_budget)
        self.history.append(rec)
        if self.budget_mode and not self.over:          # income for the next turn
            self.red_budget = min(self.cap, self.red_budget + self.income)
            self.blue_budget = min(self.cap, self.blue_budget + self.income)
        return rec

    def winner(self) -> str:
        if self.red_score > self.blue_score:
            return "Red Team"
        if self.blue_score > self.red_score:
            return "Blue Team"
        return "Draw"

    def red_stats(self):
        st = defaultdict(lambda: [0, 0])
        for h in self.history:
            st[h.red][0] += 1
            st[h.red][1] += h.success
        return st

    def blue_stats(self):
        st = defaultdict(lambda: [0, 0])
        for h in self.history:
            st[h.blue][0] += 1
            st[h.blue][1] += (not h.success)
        return st

    def mode_text(self) -> str:
        m = {"red": "Red (attacker) vs AI", "blue": "Blue (defender) vs AI",
             "both": "Two players, one keyboard"}[self.side]
        if self.side != "both":
            m += f" ({self.difficulty.title()})"
        if self.budget_mode:
            m += f" | Budget: start {self.start_budget}, +{self.income}/turn, cap {self.cap}"
        return m

    # ---- save / load ----
    def to_dict(self) -> dict:
        v, internal, gauss = self.rng.getstate()
        return dict(
            format="red_vs_blue_save", version=2, side=self.side, difficulty=self.difficulty,
            turns=self.turns, seed=self.seed, budget_mode=self.budget_mode,
            start_budget=self.start_budget, income=self.income, cap=self.cap,
            red_score=self.red_score, blue_score=self.blue_score,
            red_budget=self.red_budget, blue_budget=self.blue_budget, started=self.started,
            history=[asdict(h) for h in self.history], rng_state=[v, list(internal), gauss])

    @classmethod
    def from_dict(cls, d: dict) -> "Game":
        if d.get("format") != "red_vs_blue_save":
            raise ValueError("Not a Red vs Blue save file")
        g = cls(side=d["side"], difficulty=d["difficulty"], turns=d["turns"], seed=d.get("seed"),
                budget_mode=d["budget_mode"], start_budget=d["start_budget"], income=d["income"],
                cap=d["cap"], history=[TurnRecord(**h) for h in d["history"]],
                red_score=d["red_score"], blue_score=d["blue_score"],
                red_budget=d["red_budget"], blue_budget=d["blue_budget"], started=d["started"])
        v, internal, gauss = d["rng_state"]
        g.rng.setstate((v, tuple(internal), gauss))
        return g

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)

    @classmethod
    def load(cls, path: str) -> "Game":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ---------------------------------------------------------------------------
# Real-world context used by the PDF report
# ---------------------------------------------------------------------------
LEARNING_OBJECTIVES = [
    "Recognise six common threat types (reconnaissance, phishing, social engineering, malware, "
    "exploitation of vulnerabilities and DDoS) and what each is trying to achieve.",
    "Learn which control best counters each threat, and why partial counters make layered defence worthwhile.",
    "See that no single control stops everything: attackers adapt to whatever gaps they find.",
    "Practise budget trade-offs (budget mode): spend limited resources where the risk is highest.",
    "Build the habit of an after-action review: look at what happened, why, and what to improve.",
]

# (game tactic, real-world meaning, typical real defences, MITRE ATT&CK reference)
REAL_RED = [
    ("Reconnaissance", "Scanning networks and collecting public information (staff names, exposed services) before an attack.",
     "Monitor for scanning, reduce public exposure, least privilege.", "Reconnaissance (TA0043), e.g. T1595 Active Scanning"),
    ("Phishing", "Fake emails, links or attachments designed to steal credentials or deliver malware.",
     "Email filtering, MFA, awareness training, an easy way to report.", "Initial Access, T1566 Phishing"),
    ("Social Engineering", "Impersonating IT, suppliers or colleagues (phone or in person) to obtain access or information.",
     "Caller verification, strict reset procedures, training.", "Initial Access / Reconnaissance, e.g. T1598 Phishing for Information"),
    ("Malware Delivery", "Malicious software installed through attachments, downloads or removable media.",
     "EDR / anti-malware, application allow-listing, least privilege.", "Execution (TA0002), e.g. T1204 User Execution"),
    ("Exploit Vulnerability", "Abusing known flaws in unpatched software, often on internet-facing systems.",
     "Vulnerability scanning, rapid patching, network segmentation.", "Initial Access, T1190 Exploit Public-Facing Application"),
    ("DDoS", "Overwhelming a service with traffic so legitimate users cannot reach it.",
     "CDN / scrubbing services, rate limiting, capacity planning, response plan.", "Impact (TA0040), T1498 Network Denial of Service"),
]

# (game defence, real-world equivalent, NIST CSF function, closest CIS Controls v8 item)
REAL_BLUE = [
    ("Network Monitoring", "SIEM / IDS, log review, a security operations centre (SOC).", "Detect",
     "13 Network Monitoring and Defense; 8 Audit Log Management"),
    ("User Training", "Security awareness programmes and phishing simulations.", "Protect",
     "14 Security Awareness and Skills Training"),
    ("Access Controls", "MFA, least privilege, identity checks, help-desk verification.", "Protect",
     "5 Account Management; 6 Access Control Management"),
    ("Endpoint Protection", "EDR, anti-malware, device hardening.", "Protect / Detect", "10 Malware Defenses"),
    ("Patch Management", "Vulnerability scanning and timely patching.", "Identify / Protect",
     "7 Continuous Vulnerability Management"),
    ("DDoS Mitigation", "CDN or scrubbing services, rate limiting, ISP coordination.", "Protect / Respond",
     "12 Network Infrastructure Management; 13 Network Monitoring and Defense"),
]

APPLICATIONS = [
    ("Security awareness training",
     "Short, hands-on sessions help staff see why reporting a suspicious email or verifying a caller matters. "
     "People remember a game round better than a policy slide."),
    ("Tabletop exercises and purple teaming",
     "Real organisations use Red Teams (simulated attackers) and Blue Teams (defenders), and 'purple teaming' where "
     "both share findings. The game uses the same vocabulary and ends with an after-action review, like this report."),
    ("Security budgeting and risk prioritisation",
     "Budget mode shows that you cannot afford every control every turn. It mirrors real decisions: cheap, broad "
     "controls (monitoring, MFA, patching) versus costly, targeted ones, and spending against the threats most "
     "likely to hurt you."),
    ("Teaching and onboarding",
     "Useful as a warm-up in introductory cybersecurity courses, student societies and new-starter induction. "
     "The mapping tables below link each move to MITRE ATT&CK, NIST CSF and CIS Controls so learners can go deeper."),
    ("Incident-response and policy planning",
     "Each 'attack landed' result is a prompt: who would notice, who responds, how do we recover? "
     "Use it to rehearse detect, respond and recover roles."),
    ("Measuring improvement over time",
     "Repeat sessions and compare metrics such as the share of turns where the best counter was used. "
     "Rising numbers suggest people are getting better at matching controls to threats."),
]

EXAMPLES = [
    ("Exploit Vulnerability vs Patch Management: WannaCry (2017)",
     "The WannaCry ransomware worm spread using a Windows file-sharing (SMB) vulnerability. Microsoft had released a "
     "patch (MS17-010) about two months earlier, yet unpatched systems worldwide were hit, including parts of the UK "
     "NHS. Prompt patching was the best counter."),
    ("DDoS vs DDoS Mitigation: Dyn (2016)",
     "A botnet of compromised internet-connected devices (Mirai) flooded the DNS provider Dyn, making many popular "
     "websites hard to reach. Traffic scrubbing, redundancy and capacity planning reduce this kind of impact."),
    ("Phishing and credential theft vs Training and Access Controls",
     "Annual breach reports, such as the Verizon Data Breach Investigations Report, consistently find that phishing, "
     "stolen credentials and other human-factor routes are among the most common ways attackers get in. Training plus "
     "MFA is the classic layered answer."),
]

WORKSHOP_STEPS = [
    "Set up (5 min): pair people up. One plays Red, one plays Blue. Use Two players mode and, for advanced groups, Budget mode.",
    "Play (10-15 min): 8-10 turns. Encourage players to explain their choice out loud after each reveal.",
    "Review (10 min): export this PDF. Look at the turn log, the performance insights and the lessons section together.",
    "Map to reality (10 min): use the mapping tables to ask which of these controls your own organisation has, and which are weak.",
    "Commit (5 min): each person writes one concrete improvement they will pursue (for example enabling MFA or a patching deadline).",
]

DISCUSSION_QUESTIONS = [
    "Which defence gave the best value for its cost, and which cheap controls covered the most threats?",
    "Where did a single missing control leave you exposed? What is the real-world equivalent in your organisation?",
    "If you had 3 more budget points per turn, what would you buy first, and why?",
    "Did the attacker adapt to your defences? How would a real attacker change tactics after being blocked?",
    "Which threat would cause the most damage in your organisation, and are you defending against it?",
]

LIMITATIONS = [
    "This is a simplified teaching simulation. The success probabilities (20% / 45% / 75%) are illustrative, not real statistics.",
    "Real attacks are multi-stage chains (for example phishing, then malware, then data theft), and attackers are not limited to six moves.",
    "Real defences overlap and run continuously. In the game, Blue picks one defence per turn.",
    "It does not replace a proper risk assessment, professional training, or penetration testing. It contains no attack instructions.",
]

FURTHER_READING = [
    "MITRE ATT&CK: attack.mitre.org (catalogue of real attacker tactics and techniques)",
    "NIST Cybersecurity Framework 2.0: nist.gov/cyberframework (Govern, Identify, Protect, Detect, Respond, Recover)",
    "CIS Critical Security Controls v8: cisecurity.org/controls (prioritised defensive actions)",
    "UK National Cyber Security Centre: ncsc.gov.uk (Cyber Essentials, 10 Steps to Cyber Security, small-business guidance)",
]


def game_insights(g: "Game") -> list[tuple[str, str]]:
    """Auto-generated observations from this particular game: list of (title, text)."""
    h = g.history
    n = len(h)
    if not n:
        return [("No turns played", "Play at least one turn to see insights.")]
    out = []
    best = sum(t.blue == RED_MOVES[t.red]["primary"] for t in h)
    part = sum(t.blue in RED_MOVES[t.red]["secondary"] for t in h)
    none = n - best - part
    out.append(("Control coverage",
                f"Blue used the best counter in {best} of {n} turns ({best / n:.0%}), a partial counter in {part} and no "
                f"real counter in {none}. In real life this is your coverage: how often a control actually matches the "
                f"threat you face."))
    landed = sum(t.success for t in h)
    exp = sum(t.prob for t in h)
    out.append(("Luck versus defence",
                f"Given the odds faced, about {exp:.1f} attacks were expected to land and {landed} did. A good defence "
                f"lowers the probability but never removes risk, and over a short game luck matters, just as in real operations."))
    pts = defaultdict(int)
    for t in h:
        pts[t.red] += t.red_pts
    top = max(pts, key=pts.get) if pts else None
    if top is not None and pts[top] > 0:
        out.append(("Most costly threat",
                    f"{RED_MOVES[top]['name']} earned Red the most points ({pts[top]}). Real-world takeaway: put extra "
                    f"attention on the threat that would hurt most, here {BLUE_MOVES[RED_MOVES[top]['primary']][0]}."))
    else:
        out.append(("Most costly threat", "No attack landed, so Blue conceded no points. Excellent coverage."))
    gaps, seen = [], set()
    for t in h:
        if t.success and t.blue != RED_MOVES[t.red]["primary"] and (t.red, t.blue) not in seen:
            seen.add((t.red, t.blue))
            gaps.append(f"{RED_MOVES[t.red]['name']} landed against {BLUE_MOVES[t.blue][0]} (best counter: "
                        f"{BLUE_MOVES[RED_MOVES[t.red]['primary']][0]})")
    if gaps:
        out.append(("Gaps to close", "; ".join(gaps[:3]) + ". In practice, check whether the best control exists and is deployed."))
    freq = defaultdict(int)
    for t in h:
        freq[t.red] += 1
    mr = max(freq, key=freq.get)
    if freq[mr] / n >= 0.4 and n >= 4:
        out.append(("Attacker predictability",
                    f"Red used {RED_MOVES[mr]['name']} in {freq[mr]} of {n} turns. Repeating one tactic lets defenders "
                    f"adapt, which is exactly what the Normal and Hard AI defenders do."))
    if g.budget_mode:
        sr, sb = sum(t.red_cost for t in h), sum(t.blue_cost for t in h)
        avg_r = sum(t.red_left for t in h) / n
        avg_b = sum(t.blue_left for t in h) / n
        extra = ""
        if avg_b > 5:
            extra = (" Blue often left budget unspent: in real life unused security budget is a missed chance to reduce "
                     "risk, but spending without a threat-informed plan is equally poor value.")
        out.append(("Budget use",
                    f"Total spent: Red {sr}, Blue {sb}. Average budget left after each turn: Red {avg_r:.1f}, Blue {avg_b:.1f}."
                    + extra))
    return out


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------
def export_pdf(game: Game, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    ss = getSampleStyleSheet()
    navy, blue = colors.HexColor("#1f2d3d"), colors.HexColor("#1f4e79")
    body = ParagraphStyle("b", parent=ss["Normal"], fontSize=10, leading=14, spaceAfter=6)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=20, textColor=navy)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13, textColor=blue, spaceBefore=12, spaceAfter=6,
                        keepWithNext=1)
    h3 = ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", textColor=blue, spaceAfter=2, spaceBefore=4,
                        keepWithNext=1)
    bullet = ParagraphStyle("bl", parent=body, leftIndent=14, bulletIndent=2, spaceAfter=3)
    cell = ParagraphStyle("c", parent=body, fontSize=8, leading=10, spaceAfter=0)
    cellh = ParagraphStyle("ch", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    small = ParagraphStyle("n", parent=body, fontSize=8.5, textColor=colors.grey)
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows, widths):
        data = [[Paragraph(esc(c), cellh if i == 0 else cell) for c in row] for i, row in enumerate(rows)]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), blue),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0bec5")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef3f8")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        return t

    def callout(text):
        t = Table([[Paragraph(text, body)]], colWidths=[W])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eaf2fb")),
                               ("BOX", (0, 0), (-1, -1), 0.8, blue),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    sec = [0]
    def H(title):
        sec[0] += 1
        return Paragraph(f"{sec[0]}. {title}", h2)

    def bullets(items):
        return [Paragraph(esc(i), bullet, bulletText="\u2022") for i in items]

    bm = game.budget_mode
    W = A4[0] - 36 * mm
    story = [Paragraph("Red vs. Blue Cybersecurity Game: After-Action Report", h1),
             Paragraph(f"Played {esc(game.started)} | Mode: <b>{esc(game.mode_text())}</b> | "
                       f"Turns played: <b>{len(game.history)}</b> of {game.turns}", body)]

    # 1. Purpose
    story.append(H("Purpose of this game"))
    story.append(callout(
        "<b>Red vs. Blue</b> is a turn-based <i>serious game</i> that teaches defensive cybersecurity thinking. "
        "One side plays the attacker (Red Team) and the other the defender (Blue Team). Success depends on matching "
        "each defence to the threat it counters, often with limited resources. The goal is not to teach attacking, "
        "but to build the judgement defenders need: <b>know the threats, know the controls, and spend wisely.</b>"))
    story.append(Spacer(1, 6))
    story.append(Paragraph("What players should take away", h3))
    story += bullets(LEARNING_OBJECTIVES)
    story.append(Paragraph("Who it is for", h3))
    story.append(Paragraph("Staff awareness sessions, students and new starters, security teams running a warm-up "
                           "exercise, and managers who want to feel the trade-offs of a limited security budget.", body))

    # 2. How the game works
    story.append(H("How the game works"))
    story += bullets([
        "Each turn Red picks one of six attacks and Blue picks one of six defences. In single-player mode the AI picks the other side.",
        "The attack's success chance depends on the defence chosen: 20% against its best counter, 45% against a partial counter, 75% otherwise.",
        "If an attack lands, Red scores its severity (1 to 3 points). If it is blocked, Blue scores 1 point, or 2 for using the best counter.",
        "Budget mode: every move has a cost; each side starts with a budget, gains income each turn (up to a cap) and can only choose affordable moves.",
        "The side with the most points after the final turn wins."])
    story.append(Spacer(1, 4))
    if bm:
        rows = [["Threat", "Attack cost", "Best counter", "Counter cost", "Partial counters", "Pts if lands"]]
        for i, r in enumerate(RED_MOVES):
            rows.append([r["name"], RED_COST[i], BLUE_MOVES[r["primary"]][0], BLUE_COST[r["primary"]],
                         ", ".join(BLUE_MOVES[j][0] for j in r["secondary"]), r["sev"]])
        wf = (0.24, 0.1, 0.22, 0.1, 0.22, 0.12)
    else:
        rows = [["Threat", "Best counter", "Partial counters", "Points if it lands"]]
        for r in RED_MOVES:
            rows.append([r["name"], BLUE_MOVES[r["primary"]][0],
                         ", ".join(BLUE_MOVES[j][0] for j in r["secondary"]), r["sev"]])
        wf = (0.28, 0.28, 0.28, 0.16)
    story.append(table(rows, [W * f for f in wf]))

    # 3. Result
    story.append(H("Result of this game"))
    story.append(table([["Red score", "Blue score", "Winner"],
                        [game.red_score, game.blue_score, game.winner() if game.over else "In progress"]],
                       [W / 3] * 3))
    rs, bs, r_c, b_c = [], [], 0, 0
    for h in game.history:
        r_c += h.red_pts; b_c += h.blue_pts
        rs.append(r_c); bs.append(b_c)
    xs = list(range(1, len(rs) + 1))
    fig, ax = plt.subplots(figsize=(7, 3))
    ax.plot(xs, rs, "-o", color="#c0392b", label="Red score")
    ax.plot(xs, bs, "-o", color="#1f6fb5", label="Blue score")
    if bm:
        ax.plot(xs, [h.red_left for h in game.history], "--", color="#e6a19b", label="Red budget left")
        ax.plot(xs, [h.blue_left for h in game.history], "--", color="#9cc3e8", label="Blue budget left")
    ax.set_xlabel("Turn"); ax.set_ylabel("Points"); ax.set_xticks(xs or [0])
    ax.grid(alpha=0.3); ax.legend(fontsize=8); ax.set_title("Score progression" + (" and budget" if bm else ""))
    buf = io.BytesIO(); fig.tight_layout(); fig.savefig(buf, format="png", dpi=160); plt.close(fig)
    buf.seek(0)
    story += [Spacer(1, 8), Image(buf, W, W * 3 / 7)]

    # 4. Turn log
    story.append(H("Turn-by-turn log"))
    if bm:
        rows = [["Turn", "Red tactic", "Blue defence", "Red chance", "Outcome", "Pts R/B", "Cost R/B", "Left R/B"]]
        wf = (0.08, 0.19, 0.19, 0.1, 0.14, 0.08, 0.11, 0.11)
    else:
        rows = [["Turn", "Red tactic", "Blue defence", "Red success chance", "Outcome", "Pts (R/B)"]]
        wf = (0.08, 0.22, 0.22, 0.15, 0.18, 0.15)
    for h in game.history:
        row = [h.turn, RED_MOVES[h.red]["name"], BLUE_MOVES[h.blue][0], f"{h.prob:.0%}",
               "Attack landed" if h.success else "Blocked", f"{h.red_pts}/{h.blue_pts}"]
        if bm:
            row += [f"{h.red_cost}/{h.blue_cost}", f"{h.red_left}/{h.blue_left}"]
        rows.append(row)
    story.append(table(rows, [W * f for f in wf]))

    # 5. Insights
    story.append(H("What this game tells us (performance insights)"))
    for title, text in game_insights(game):
        story.append(KeepTogether([Paragraph(esc(title), h3), Paragraph(esc(text), body)]))

    # 6. Tactic performance
    story.append(H("Tactic performance"))
    rs_ = game.red_stats()
    rows = [["Red tactic", "Attempts", "Landed", "Success rate"]]
    for i, r in enumerate(RED_MOVES):
        a, s = rs_[i] if i in rs_ else (0, 0)
        rows.append([r["name"], a, s, f"{s / a:.0%}" if a else "-"])
    story.append(table(rows, [W * f for f in (0.4, 0.2, 0.2, 0.2)]))
    story.append(Spacer(1, 6))
    bs_ = game.blue_stats()
    rows = [["Blue defence", "Times used", "Blocks", "Block rate"]]
    for i, b in enumerate(BLUE_MOVES):
        a, s = bs_[i] if i in bs_ else (0, 0)
        rows.append([b[0], a, s, f"{s / a:.0%}" if a else "-"])
    story.append(table(rows, [W * f for f in (0.4, 0.2, 0.2, 0.2)]))

    # 7. Lessons
    story.append(H("Lessons from this game"))
    seen = set()
    for h in game.history:
        if (h.red, h.success) in seen:
            continue
        seen.add((h.red, h.success))
        story.append(Paragraph(f"<b>{esc(RED_MOVES[h.red]['name'])}</b> "
                               f"({'landed' if h.success else 'blocked'}): {esc(h.lesson)}", body))

    # 8. Mapping to the real world
    story.append(H("From game to real world: what each move represents"))
    story.append(Paragraph("Threats (Red moves). ATT&CK references are indicative; real incidents often combine several techniques.", h3))
    rows = [["Game tactic", "Real-world meaning", "Typical real defences", "MITRE ATT&CK"]]
    rows += [list(r) for r in REAL_RED]
    story.append(table(rows, [W * f for f in (0.16, 0.32, 0.27, 0.25)]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Defences (Blue moves). Framework mappings are the closest fit, not exact equivalents.", h3))
    rows = [["Game defence", "Real-world equivalent", "NIST CSF function", "CIS Controls v8"]]
    rows += [list(r) for r in REAL_BLUE]
    story.append(table(rows, [W * f for f in (0.17, 0.33, 0.17, 0.33)]))

    # 9. Applications
    story.append(H("How this can be applied in the real world"))
    for title, text in APPLICATIONS:
        story.append(KeepTogether([Paragraph(esc(title), h3), Paragraph(esc(text), body)]))

    # 10. Examples
    story.append(H("Real-world examples that match the game"))
    for title, text in EXAMPLES:
        story.append(KeepTogether([Paragraph(esc(title), h3), Paragraph(esc(text), body)]))

    # 11. Workshop
    story.append(H("Running this as a 30 to 45 minute workshop"))
    story += [Paragraph(esc(s), bullet, bulletText=f"{i}.") for i, s in enumerate(WORKSHOP_STEPS, 1)]
    story.append(Paragraph("Discussion questions", h3))
    story += bullets(DISCUSSION_QUESTIONS)

    # 12. Limitations
    story.append(H("Limitations"))
    story += bullets(LIMITATIONS)

    # 13. Further reading
    story.append(KeepTogether([H("Further reading")] + bullets(FURTHER_READING) + [
        Spacer(1, 8),
        Paragraph("Educational simulation only. No real attack techniques are described or enabled.", small)]))

    def footer(c, d):
        c.setFont("Helvetica", 8); c.setFillColor(colors.grey)
        c.drawCentredString(A4[0] / 2, 10 * mm, f"Red vs. Blue Cyber Game report | Page {d.page}")

    SimpleDocTemplate(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=18 * mm, title="Red vs Blue Cyber Game Report").build(
        story, onFirstPage=footer, onLaterPages=footer)


# ---------------------------------------------------------------------------
# GUI (tkinter)
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    BG, FG, PANEL = "#12161c", "#e8edf2", "#1c232d"
    root = tk.Tk()
    root.title("Red vs. Blue Cybersecurity Game")
    root.configure(bg=BG)
    root.geometry("900x780")

    st = {"game": None, "phase": None, "pending": None}   # phase: red | blue | handoff | None
    side_v = tk.StringVar(value="red")
    diff_v = tk.StringVar(value="normal")
    turns_v = tk.IntVar(value=8)
    budget_v = tk.BooleanVar(value=False)

    style = ttk.Style(); style.theme_use("clam")
    for w in ("TLabel", "TFrame", "TRadiobutton", "TCheckbutton"):
        style.configure(w, background=BG, foreground=FG)
    style.configure("Move.TButton", font=("Segoe UI", 10, "bold"), padding=8)

    # ---- setup
    setup = ttk.Frame(root); setup.pack(fill="x", padx=16, pady=(12, 2))
    ttk.Label(setup, text="Red vs. Blue Cyber Game", font=("Segoe UI", 20, "bold")).grid(row=0, column=0, columnspan=9, sticky="w")
    ttk.Label(setup, text="Mode:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Radiobutton(setup, text="Red vs AI", variable=side_v, value="red").grid(row=1, column=1)
    ttk.Radiobutton(setup, text="Blue vs AI", variable=side_v, value="blue").grid(row=1, column=2)
    ttk.Radiobutton(setup, text="Two players", variable=side_v, value="both").grid(row=1, column=3)
    ttk.Label(setup, text="  AI level:").grid(row=2, column=0, sticky="w")
    ttk.Combobox(setup, textvariable=diff_v, values=["easy", "normal", "hard"], width=8, state="readonly").grid(row=2, column=1)
    ttk.Label(setup, text="Turns:").grid(row=2, column=2, sticky="e")
    ttk.Spinbox(setup, from_=3, to=20, textvariable=turns_v, width=4).grid(row=2, column=3)
    ttk.Checkbutton(setup, text="Budget mode", variable=budget_v).grid(row=2, column=4, padx=8)
    start_btn = ttk.Button(setup, text="Start / New game"); start_btn.grid(row=1, column=7, rowspan=2, padx=10)

    # ---- scoreboard
    board = ttk.Frame(root); board.pack(fill="x", padx=16, pady=2)
    turn_lbl = ttk.Label(board, text="Turn: -", font=("Segoe UI", 12, "bold")); turn_lbl.pack(side="left")
    score_lbl = ttk.Label(board, text="Red 0  |  Blue 0", font=("Segoe UI", 12, "bold")); score_lbl.pack(side="right")
    budget_lbl = ttk.Label(root, text="", font=("Segoe UI", 10), foreground="#ffd54f"); budget_lbl.pack(anchor="e", padx=16)

    prompt_lbl = ttk.Label(root, text="Press Start to begin.", font=("Segoe UI", 12, "bold"), wraplength=860)
    prompt_lbl.pack(anchor="w", padx=16, pady=(6, 2))
    info_lbl = ttk.Label(root, text="", wraplength=860, foreground="#9fb3c8"); info_lbl.pack(anchor="w", padx=16)

    moves_frame = ttk.Frame(root); moves_frame.pack(fill="x", padx=16, pady=6)
    buttons = []
    for i in range(N):
        b = ttk.Button(moves_frame, style="Move.TButton")
        b.grid(row=i // 3, column=i % 3, padx=4, pady=4, sticky="ew")
        moves_frame.columnconfigure(i % 3, weight=1)
        buttons.append(b)

    ready_btn = ttk.Button(root, text="I'm ready: reveal my moves")   # hand-off button (2P)

    feedback = tk.Label(root, text="", bg=PANEL, fg=FG, font=("Segoe UI", 11, "bold"), wraplength=860,
                        justify="left", anchor="w", padx=10, pady=8)
    feedback.pack(fill="x", padx=16, pady=4)

    log = tk.Text(root, height=11, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=16, pady=6)
    for tag, col in (("red", "#ff8a80"), ("blue", "#82b1ff"), ("lesson", "#b0bec5"), ("end", "#ffd54f")):
        log.tag_configure(tag, foreground=col)

    bottom = ttk.Frame(root); bottom.pack(fill="x", padx=16, pady=(0, 12))
    pdf_btn = ttk.Button(bottom, text="Export PDF report", state="disabled"); pdf_btn.pack(side="right")
    load_btn = ttk.Button(bottom, text="Load game"); load_btn.pack(side="left")
    save_btn = ttk.Button(bottom, text="Save game", state="disabled"); save_btn.pack(side="left", padx=6)

    # ---- helpers
    def log_add(text, tag=None):
        log.configure(state="normal"); log.insert("end", text + "\n", tag); log.see("end"); log.configure(state="disabled")

    def active_side():
        g = st["game"]
        return g.side if g.side != "both" else ("blue" if st["phase"] == "blue" else "red")

    def render():
        """Refresh scoreboard, prompt and buttons for the current state."""
        g = st["game"]
        if not g:
            return
        score_lbl.config(text=f"Red {g.red_score}  |  Blue {g.blue_score}")
        turn_lbl.config(text=f"Turn: {min(g.turn_no, g.turns)} / {g.turns}")
        budget_lbl.config(text=f"Budget:  Red {g.red_budget}   |   Blue {g.blue_budget}   (income +{g.income}/turn, cap {g.cap})"
                          if g.budget_mode else "")
        ready_btn.pack_forget()
        phase = st["phase"]
        if g.over:
            msg = f"GAME OVER: {g.winner()} wins ({g.red_score}-{g.blue_score})" if g.winner() != "Draw" \
                else f"GAME OVER: Draw ({g.red_score}-{g.blue_score})"
            prompt_lbl.config(text=msg)
            for b in buttons: b.state(["disabled"])
            pdf_btn.config(state="normal"); save_btn.config(state="normal")
            return
        if phase == "handoff":
            prompt_lbl.config(text="Choice locked in. Pass the keyboard to Player 2 (BLUE), then press the button.")
            for b in buttons: b.state(["disabled"]); b.config(text="")
            ready_btn.pack(after=moves_frame, pady=4)
            save_btn.config(state="disabled"); pdf_btn.config(state="disabled")
            return
        side = active_side()
        who = {"red": "Player 1 (RED)", "blue": "Player 2 (BLUE)"}[side] if g.side == "both" else "Your"
        hint = " Player 2, look away!" if g.side == "both" and side == "red" else ""
        prompt_lbl.config(text=(f"{who}: choose an " if who != "Your" else "Choose your ")
                          + ("attack." if side == "red" else "defence.") + hint)
        names = [r["name"] for r in RED_MOVES] if side == "red" else [b[0] for b in BLUE_MOVES]
        afford = g.affordable(side)
        for i, b in enumerate(buttons):
            label = names[i] + (f"  [cost {g.cost(side, i)}]" if g.budget_mode else "")
            b.config(text=label, command=lambda i=i: choose(i))
            b.state(["!disabled"] if i in afford else ["disabled"])
        save_btn.config(state="normal"); pdf_btn.config(state="normal" if g.history else "disabled")

    def show_turn(rec):
        tag = "red" if rec.success else "blue"
        extra = f", cost R{rec.red_cost}/B{rec.blue_cost}" if st["game"].budget_mode else ""
        log_add(f"Turn {rec.turn}: Red {RED_MOVES[rec.red]['name']}  vs  Blue {BLUE_MOVES[rec.blue][0]}  "
                f"(attack chance {rec.prob:.0%}{extra})")
        log_add(f"   {rec.text}  [Red +{rec.red_pts}, Blue +{rec.blue_pts}]", tag)
        log_add(f"   Lesson: {rec.lesson}", "lesson")
        feedback.config(text=rec.text + "\n" + rec.lesson, fg="#ff8a80" if rec.success else "#82b1ff")

    def choose(idx):
        g = st["game"]
        if not g or g.over:
            return
        try:
            if g.side == "both":
                if st["phase"] == "red":
                    st["pending"], st["phase"] = idx, "handoff"
                    info_lbl.config(text="")
                    render(); return
                rec = g.resolve(st["pending"], idx)
                st["pending"], st["phase"] = None, "red"
            else:
                rec = g.play_turn(idx)
        except ValueError as e:
            messagebox.showwarning("Not allowed", str(e)); return
        show_turn(rec)
        if g.over:
            log_add(prompt_for_end(g), "end")
        render()

    def prompt_for_end(g):
        return f"GAME OVER: {g.winner()} wins ({g.red_score}-{g.blue_score})" if g.winner() != "Draw" \
            else f"GAME OVER: Draw ({g.red_score}-{g.blue_score})"

    def reveal():
        st["phase"] = "blue"; render()

    def hover(idx):
        g = st["game"]
        if not g or g.over or st["phase"] in (None, "handoff"):
            return
        if active_side() == "red":
            r = RED_MOVES[idx]
            info_lbl.config(text=f"{r['name']} ({r['sev']} pts if it lands): {r['desc']}")
        else:
            info_lbl.config(text=f"{BLUE_MOVES[idx][0]}: {BLUE_MOVES[idx][1]}")

    def begin(g: Game, replay: bool = False):
        st["game"], st["pending"] = g, None
        st["phase"] = None if (g.over and g.side != "both") else ("red" if g.side == "both" else g.side)
        if g.over:
            st["phase"] = None
        log.configure(state="normal"); log.delete("1.0", "end"); log.configure(state="disabled")
        log_add(("Loaded game: " if replay else "New game: ") + g.mode_text() + f", {g.turns} turns.", "end")
        for rec in g.history:
            show_turn(rec)
        if g.over:
            log_add(prompt_for_end(g), "end")
        info_lbl.config(text="Hover over a move for details.")
        if not g.history:
            feedback.config(text="", fg=FG)
        render()

    def start():
        try:
            turns = max(1, int(turns_v.get()))
        except (tk.TclError, ValueError):
            turns = 8
        begin(Game(side=side_v.get(), difficulty=diff_v.get(), turns=turns, budget_mode=budget_v.get()))

    def save_game():
        g = st["game"]
        if not g: return
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="red_vs_blue_save.json",
                                            filetypes=[("Game save", "*.json")])
        if path:
            g.save(path); messagebox.showinfo("Saved", f"Game saved to:\n{path}")

    def load_game():
        path = filedialog.askopenfilename(filetypes=[("Game save", "*.json")])
        if not path: return
        try:
            g = Game.load(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Could not load", str(e)); return
        side_v.set(g.side); diff_v.set(g.difficulty); turns_v.set(g.turns); budget_v.set(g.budget_mode)
        begin(g, replay=True)

    def save_pdf():
        g = st["game"]
        if not g: return
        path = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile="red_vs_blue_report.pdf",
                                            filetypes=[("PDF", "*.pdf")])
        if path:
            try:
                export_pdf(g, path); messagebox.showinfo("Saved", f"Report saved to:\n{path}")
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Export failed", f"{e}\n\nInstall dependencies: pip install reportlab matplotlib")

    for i, b in enumerate(buttons):
        b.bind("<Enter>", lambda e, i=i: hover(i))
        b.state(["disabled"])
    start_btn.config(command=start); ready_btn.config(command=reveal)
    save_btn.config(command=save_game); load_btn.config(command=load_game); pdf_btn.config(command=save_pdf)

    # test hook: lets automated tests drive the real widgets
    root._rvb = dict(st=st, choose=choose, start=start, reveal=reveal, buttons=buttons, ready=ready_btn,
                     begin=begin, side_v=side_v, budget_v=budget_v, prompt=prompt_lbl, budget_lbl=budget_lbl,
                     log=log, save_btn=save_btn, pdf_btn=pdf_btn)
    if getattr(run_gui, "_test", None):
        run_gui._test(root); return
    root.mainloop()


# ---------------------------------------------------------------------------
# Headless demo / entry point
# ---------------------------------------------------------------------------
def run_demo(turns: int, seed: int | None, out: str, budget: bool, save: str | None):
    """AI vs AI game (hard Red vs normal Blue) written straight to PDF."""
    g = Game(side="red", difficulty="normal", turns=turns, seed=seed, budget_mode=budget)
    while not g.over:
        g.resolve(g.ai_choose("red", "hard"), g.ai_choose("blue", "normal"))
    export_pdf(g, out)
    if save:
        g.save(save)
    print(f"Demo game: Red {g.red_score} - Blue {g.blue_score} ({g.winner()}). Report: {out}"
          + (f", save: {save}" if save else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Red vs. Blue cyber game")
    ap.add_argument("--demo", action="store_true", help="AI-vs-AI game -> PDF (no GUI)")
    ap.add_argument("--budget", action="store_true", help="enable budget mode for --demo")
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="red_vs_blue_report.pdf")
    ap.add_argument("--save", default=None, help="save the demo game to this JSON file")
    ap.add_argument("--report-from", default=None, help="write a PDF report from a saved JSON game")
    a = ap.parse_args()
    if a.report_from:
        export_pdf(Game.load(a.report_from), a.out); print("Report written:", a.out)
    elif a.demo:
        run_demo(a.turns, a.seed, a.out, a.budget, a.save)
    else:
        run_gui()
