#!/usr/bin/env python3
"""Generate the README architecture diagram, one SVG per colour scheme.

Two files rather than one self-switching file: GitHub strips <style> blocks
from inline SVG, so a `prefers-color-scheme` media query inside the document is
silently dropped. The README pairs them with <picture>, which GitHub honours.

A README diagram is studied, not narrated, so it is COMPLETE rather than
minimal: the queue, the stores and the polling loop are all on it, because
they are how a message actually travels. (The video slides for these projects
simplify; that is a separate deliverable with its own rules.) Left out only
what does not change behaviour: CloudFront and the frontend bucket, and
Cognito, which survives as the label on the browser's hop.

The same geometry draws aws-agentcore-sandbox's diagram -- the two projects
exist to be compared, so every box sits in the same place in both, and what
differs is visible as a difference. Only the VARIANT section changes between
the two copies of this script; keep the rest in step.

Run:  python make_diagram.py
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------------------
# Shared geometry and style
# ------------------------------------------------------------------------------
# No mobile sizing: this is read on a monitor, at full width in a README.
NW, NH = 420, 110                          # node width, height
GUTTER = 140                               # between columns: room for a label
MARGIN = 40
COL = [MARGIN + i * (NW + GUTTER) for i in range(3)]
ROW = [40, 240, 440, 640]
W = MARGIN * 2 + NW * 3 + GUTTER * 2
H = ROW[3] + NH + 40
TITLE_PX, SUB_PX, EDGE_PX = 26, 17, 16

THEMES = {
    "dark": {
        "bg": "#0d1117", "panel": "#161b22", "text": "#e6edf3",
        "muted": "#8b949e", "line": "#58a6ff", "local": "#39c5bb",
        "purple": "#c39be8", "navy": "#c8d6e8", "blue": "#58a6ff",
        "amber": "#f2c163", "store": "#7ee787",
    },
    "light": {
        "bg": "#ffffff", "panel": "#f6f8fa", "text": "#1f2328",
        "muted": "#59636e", "line": "#0969da", "local": "#137e77",
        "purple": "#8250df", "navy": "#475467", "blue": "#0969da",
        "amber": "#9a6700", "store": "#1a7f37",
    },
}

# Lucide icon paths, on a 24-unit grid.
ICONS = {
    "bot": ['<path d="M12 8V4H8"/>',
            '<rect width="16" height="12" x="4" y="8" rx="2"/>',
            '<path d="M2 14h2"/>', '<path d="M20 14h2"/>',
            '<path d="M15 13v2"/>', '<path d="M9 13v2"/>'],
    "monitor": ['<rect width="20" height="14" x="2" y="3" rx="2"/>',
                '<line x1="8" x2="16" y1="21" y2="21"/>',
                '<line x1="12" x2="12" y1="17" y2="21"/>'],
    "route": ['<circle cx="6" cy="19" r="3"/>', '<circle cx="18" cy="5" r="3"/>',
              '<path d="M12 19h4.5a3.5 3.5 0 0 0 0-7h-8a3.5 3.5 0 0 1 0-7H12"/>'],
    "zap": ['<path d="M4 14h7l-1 8 10-12h-7l1-8z"/>'],
    "server": ['<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/>',
               '<rect width="20" height="8" x="2" y="14" rx="2" ry="2"/>',
               '<line x1="6" x2="6.01" y1="6" y2="6"/>',
               '<line x1="6" x2="6.01" y1="18" y2="18"/>'],
    "terminal": ['<polyline points="4 17 10 11 4 5"/>',
                 '<line x1="12" x2="20" y1="19" y2="19"/>'],
    "inbox": ['<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/>',
              '<path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89'
              'A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'],
    "database": ['<ellipse cx="12" cy="5" rx="9" ry="3"/>',
                 '<path d="M3 5V19A9 3 0 0 0 21 19V5"/>',
                 '<path d="M3 12A9 3 0 0 0 21 12"/>'],
    "history": ['<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>',
                '<path d="M3 3v5h5"/>', '<path d="M12 7v5l4 2"/>'],
}

NODES = {}


def _r(nid):
    """Return (left, top, right, bottom, centre-x, centre-y) for a node."""
    x, y = NODES[nid][0], NODES[nid][1]
    return x, y, x + NW, y + NH, x + NW / 2, y + NH / 2


def _right(a, b):
    """Horizontal hop, a's right edge to b's left edge."""
    return f"M{_r(a)[2]},{_r(a)[5]} L{_r(b)[0] - 5},{_r(b)[5]}"


def _left(a, b):
    """Horizontal hop, a's left edge to b's right edge."""
    return f"M{_r(a)[0]},{_r(a)[5]} L{_r(b)[2] + 5},{_r(b)[5]}"


def _down(a, b):
    """Vertical hop between two nodes sharing a column."""
    x = _r(a)[4]
    return f"M{x},{_r(a)[3]} L{x},{_r(b)[1] - 5}"


def _out_up(a, b):
    """Right out of a, then up into b's underside (b is in the next column)."""
    return f"M{_r(a)[2]},{_r(a)[5]} H{_r(b)[4]} V{_r(b)[3] + 5}"


def _out_down(a, b):
    """Right out of a, then down into b's top (b is in the next column)."""
    return f"M{_r(a)[2]},{_r(a)[5]} H{_r(b)[4]} V{_r(b)[1] - 5}"


def gutter_x(c):
    """Centre of the gutter to the right of column c."""
    return COL[c] + NW + GUTTER / 2


def between(a, b):
    """Label baseline centred on a vertical hop between two nodes."""
    return (_r(a)[3] + _r(b)[1]) / 2 + 6


# ------------------------------------------------------------------------------
# VARIANT -- the only section that differs from aws-agentcore-sandbox's copy
# ------------------------------------------------------------------------------
# The request path runs down the middle column: API -> SQS -> worker, with the
# worker as the hub -- the model to its left, the MicroVM below it, its results
# climbing the right column to DynamoDB + S3, where the API polls for them.
# Amber goes to one node only: the sandbox, the subject of the project.
#
# id: (x, y, colour key, icon, title, subtitle)
NODES.update({
    "web":     (COL[0], ROW[0], "navy", "monitor", "Web App", "chat UI, polls for results"),
    "api":     (COL[1], ROW[0], "blue", "route", "API", "API Gateway + Lambda"),
    "store":   (COL[2], ROW[0], "store", "database", "DynamoDB + S3",
                "questions, traces, files, history"),
    "queue":   (COL[1], ROW[1], "blue", "inbox", "SQS", "one message per question"),
    "agent":   (COL[1], ROW[2], "blue", "zap", "Worker Lambda", "Converse tool loop, 15 min max"),
    "model":   (COL[0], ROW[2], "purple", "bot", "Model", "picked per chat, on Bedrock"),
    "sandbox": (COL[1], ROW[3], "amber", "terminal", "Lambda MicroVM",
                "Python + bash, suspends when idle"),
})

# Vertical-hop labels sit left of their line, right-anchored; horizontal-hop
# labels sit above their line in the gutter; the results line is labelled to
# the left of its climb, in the empty slot beside the queue.
# id: (path, colour key, label, label_x, label_y, anchor)
EDGES = {
    "e_web":   (_right("web", "api"), "line", "Cognito JWT",
                gutter_x(0), _r("web")[5] - 12, "middle"),
    "e_poll":  (_right("api", "store"), "store", "write + poll",
                gutter_x(1), _r("api")[5] - 12, "middle"),
    "e_queue": (_down("api", "queue"), "line", "enqueue",
                _r("api")[4] - 16, between("api", "queue"), "end"),
    "e_agent": (_down("queue", "agent"), "line", "trigger",
                _r("queue")[4] - 16, between("queue", "agent"), "end"),
    "e_model": (_left("agent", "model"), "purple", "Converse",
                gutter_x(0), _r("agent")[5] - 12, "middle"),
    "e_sand":  (_down("agent", "sandbox"), "local", "HTTPS + scoped token",
                _r("agent")[4] - 16, between("agent", "sandbox"), "end"),
    "e_store": (_out_up("agent", "store"), "store", "trace, answer, files, history",
                _r("store")[4] - 16, (ROW[1] + ROW[1] + NH) / 2 + 6, "end"),
}

ALT = ("A web app calls an API that writes each question to DynamoDB and S3 and "
       "enqueues it on SQS; a worker Lambda runs the Converse tool loop, calling "
       "the conversation's model on Bedrock and running code in a per-conversation Lambda MicroVM, "
       "and writes results back to DynamoDB and S3, which the web app polls")

# ------------------------------------------------------------------------------
# Rendering
# ------------------------------------------------------------------------------
FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"


def node_svg(nid, t):
    """Render one service box: rounded panel, icon, title, subtitle."""
    x, y, key, icon, title, sub = NODES[nid]
    colour = t[key]
    scale = 1.25
    isz = 24 * scale
    ix, iy = x + 20, y + (NH - isz) / 2
    tx = x + 20 + isz + 14
    return "".join([
        f'<rect x="{x}" y="{y}" width="{NW}" height="{NH}" rx="10" ry="10" '
        f'fill="{t["panel"]}" stroke="{colour}" stroke-width="2"/>',
        f'<g transform="translate({ix},{iy}) scale({scale})" fill="none" '
        f'stroke="{colour}" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">' + "".join(ICONS[icon]) + "</g>",
        f'<text x="{tx}" y="{y + NH/2 - 4}" font-family="{FONT}" '
        f'font-size="{TITLE_PX}" font-weight="600" fill="{t["text"]}">{title}</text>',
        f'<text x="{tx}" y="{y + NH/2 + 22}" font-family="{FONT}" '
        f'font-size="{SUB_PX}" fill="{t["muted"]}">{sub}</text>',
    ])


def edge_svg(eid, t):
    """Render one arrow plus its label."""
    d, key, label, lx, ly, anchor = EDGES[eid]
    colour = t[key]
    out = (f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2" '
           f'marker-end="url(#a_{eid})"/>')
    if label:
        out += (f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" '
                f'font-family="{FONT}" font-size="{EDGE_PX}" font-weight="600" '
                f'fill="{colour}">{label}</text>')
    return out


def build(t):
    """Assemble the diagram for one theme."""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}" role="img" aria-label="{ALT}">', "<defs>"]
    # One marker per edge: a shared marker cannot carry per-edge colour.
    for eid, spec in EDGES.items():
        out.append(
            f'<marker id="a_{eid}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{t[spec[1]]}"/></marker>')
    out.append("</defs>")
    out.append(f'<rect width="{W}" height="{H}" fill="{t["bg"]}"/>')
    for eid in EDGES:
        out.append(edge_svg(eid, t))
    for nid in NODES:
        out.append(node_svg(nid, t))
    out.append("</svg>")
    return "".join(out)


def main():
    for name, theme in THEMES.items():
        path = os.path.join(HERE, f"architecture-{name}.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(build(theme))
        print(f"  architecture-{name}.svg")


if __name__ == "__main__":
    main()
