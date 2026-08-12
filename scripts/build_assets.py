#!/usr/bin/env python3
"""Gera os SVGs do perfil no branding da Overflow.

Por que existe: o GitHub renderiza SVG de README dentro de <img>, um contexto que
nao carrega fonte externa nem @font-face. Entao o texto vira path aqui, no build,
e a tipografia da marca (Space Grotesk / JetBrains Mono / Instrument Serif) chega
intacta no perfil. De quebra, os assets passam a ser servidos pelo proprio repo em
vez de APIs de terceiros que vivem fora do ar.

Uso:
    python3 scripts/build_assets.py            # sem token: dados publicos via REST
    GITHUB_TOKEN=... python3 scripts/build_assets.py   # + contribuicoes via GraphQL
"""
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.request

from fontTools.misc.transform import Transform
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
import uharfbuzz as hb

USER = os.environ.get("PROFILE_USER", "Wellychon")
ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
# fora do repo de proposito: fonte baixada e cache de build, nao artefato versionado
CACHE = pathlib.Path(tempfile.gettempdir()) / "overflow-profile-fonts"

# --- paleta Overflow (design tokens v0.1) --------------------------------
BLUE = "#0000FF"   # Overflow Blue  — cor da marca
ACID = "#E5FF00"   # Acid Signal    — sinalizacao, com moderacao
INK = "#0A0A0A"    # Deep Ink       — fundo
PAPER = "#F4F2EC"  # Bone Paper     — texto
MUTE = "#8A8A8A"   # texto secundario

GFONTS = "https://raw.githubusercontent.com/google/fonts/main/ofl"
FONT_URLS = {
    "SpaceGrotesk.ttf": f"{GFONTS}/spacegrotesk/SpaceGrotesk%5Bwght%5D.ttf",
    "JetBrainsMono.ttf": f"{GFONTS}/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf",
    "InstrumentSerif-Italic.ttf": f"{GFONTS}/instrumentserif/InstrumentSerif-Italic.ttf",
}


def fetch_fonts():
    CACHE.mkdir(exist_ok=True)
    for name, url in FONT_URLS.items():
        dest = CACHE / name
        if not dest.exists():
            with urllib.request.urlopen(url, timeout=60) as r:
                dest.write_bytes(r.read())
    return CACHE


# --- texto -> path --------------------------------------------------------
class Shaper:
    def __init__(self, path, wght=None):
        font = TTFont(path)
        if "fvar" in font and wght is not None:
            font = instancer.instantiateVariableFont(font, {"wght": wght}, inplace=False)
        buf = io.BytesIO()
        font.save(buf)
        data = buf.getvalue()
        self.tt = TTFont(io.BytesIO(data))
        self.upem = self.tt["head"].unitsPerEm
        self.glyphset = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.hbfont = hb.Font(hb.Face(data))
        self.hbfont.scale = (self.upem, self.upem)

    def _shape(self, text):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hbfont, buf, {"kern": True, "liga": True})
        return buf.glyph_infos, buf.glyph_positions

    def width(self, text, size, tracking=0.0):
        _, pos = self._shape(text)
        adv = sum(p.x_advance for p in pos)
        return adv * size / self.upem + tracking * size * max(len(pos) - 1, 0)

    def path(self, text, size, x=0.0, y=0.0, tracking=0.0):
        infos, pos = self._shape(text)
        scale = size / self.upem
        out, cx = [], 0.0
        for info, p in zip(infos, pos):
            name = self.order[info.codepoint]
            pen = SVGPathPen(self.glyphset, ntos=lambda v: f"{v:.2f}")
            t = Transform(scale, 0, 0, -scale,
                          x + (cx + p.x_offset) * scale,
                          y - p.y_offset * scale)
            self.glyphset[name].draw(TransformPen(pen, t))
            d = pen.getCommands()
            if d:
                out.append(d)
            cx += p.x_advance + tracking * self.upem
        return " ".join(out)


def text(shaper, s, size, x, y, fill, tracking=0.0, opacity=None):
    d = shaper.path(s, size, x, y, tracking)
    if not d:
        return ""
    op = f' opacity="{opacity}"' if opacity is not None else ""
    return f'<path fill="{fill}"{op} d="{d}"/>'


# --- dados do GitHub ------------------------------------------------------
def api(url, token=None):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-profile-build",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def graphql(token):
    q = """query($login:String!){user(login:$login){
      contributionsCollection{contributionCalendar{totalContributions}
        totalCommitContributions restrictedContributionsCount}
      repositories(first:100,ownerAffiliations:OWNER,isFork:false){
        totalCount nodes{stargazerCount}}}}"""
    body = json.dumps({"query": q, "variables": {"login": USER}}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "User-Agent": f"{USER}-profile-build"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def collect():
    """Junta o que der. Sem token cai pro REST publico; nada aqui pode derrubar o build.

    So entra numero de repo publico e nao-fork: o README e publico, contagem e
    linguagem de repo privado nao vao pra vitrine.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if token in ("", "proxy-injected", None):   # sentinela do proxy de dev, nao e credencial
        token = None
    data = {"repos": 0, "stars": 0, "followers": 0, "contributions": None, "langs": []}
    try:
        u = api(f"https://api.github.com/users/{USER}", token)
        data["repos"] = u.get("public_repos", 0)
        data["followers"] = u.get("followers", 0)
    except Exception as e:                                    # noqa: BLE001
        print(f"  ! perfil: {e}", file=sys.stderr)

    langs = {}
    try:
        page, repos = 1, []
        while page <= 3:
            chunk = api(
                f"https://api.github.com/users/{USER}/repos"
                f"?per_page=100&page={page}&type=owner&sort=pushed", token)
            if not chunk:
                break
            repos += chunk
            if len(chunk) < 100:
                break
            page += 1
        public = [r for r in repos if not r.get("fork") and not r.get("private")]
        data["repos"] = len(public) or data["repos"]
        for r in public:
            data["stars"] += r.get("stargazers_count", 0)
            lang = r.get("language")
            if lang:
                langs[lang] = langs.get(lang, 0) + max(r.get("size", 0), 1)
    except Exception as e:                                    # noqa: BLE001
        print(f"  ! repos: {e}", file=sys.stderr)

    total = sum(langs.values())
    if total:
        top = sorted(langs.items(), key=lambda kv: -kv[1])[:6]
        data["langs"] = [(n, v / total * 100) for n, v in top]

    # Ambientes sem acesso a API de usuario (ex.: sandbox restrito a repo) usam um
    # seed pra nao gerar um card zerado. Em Actions o token real manda.
    seed = os.environ.get("PROFILE_SEED")
    if seed and not data["repos"] and pathlib.Path(seed).exists():
        s = json.loads(pathlib.Path(seed).read_text())
        data.update({k: s[k] for k in ("repos", "stars", "followers") if k in s})
        data["langs"] = [tuple(x) for x in s.get("langs", [])]
        print("  (dados do seed)", file=sys.stderr)

    if token:
        try:
            g = graphql(token)["data"]["user"]
            c = g["contributionsCollection"]
            data["contributions"] = c["contributionCalendar"]["totalContributions"]
        except Exception as e:                                # noqa: BLE001
            print(f"  ! graphql: {e}", file=sys.stderr)
    return data


# --- assets ---------------------------------------------------------------
def svg(w, h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{title}">'
            f'<title>{title}</title>{body}</svg>\n')


def build_header(G, M, S):
    W, H = 1200, 320
    p = [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    # quebra de grid deliberada: bloco azul sangrando pela esquerda
    p.append(f'<rect x="0" y="96" width="56" height="8" fill="{BLUE}"/>')
    p.append(text(M, "// WE ALL HAVE A PROBLEM TO SOLVE", 13, 88, 64, ACID, 0.12))
    p.append(text(G, "Wellychon", 96, 84, 168, PAPER, -0.03))
    off = 84 + G.width("Wellychon", 96, -0.03) + 22
    p.append(text(G, "Felix", 96, off, 168, BLUE, -0.03))
    p.append(f'<rect x="84" y="200" width="{W - 168}" height="1" fill="{PAPER}" opacity="0.12"/>')
    p.append(text(S, "Give us the mess.", 40, 84, 256, PAPER))
    off = 84 + S.width("Give us the mess.", 40) + 14
    p.append(text(S, "We'll give you the shape.", 40, off, 256, ACID))
    return svg(W, H, "".join(p), f"{USER} — Wellychon Felix")


def build_typing(M):
    """Linhas rotativas com efeito de digitacao. CSS dentro do SVG: roda no <img> do GitHub."""
    W, H, SIZE, TRACK = 1200, 72, 20, 0.1
    lines = [
        "CEO @ OVERFLOW",
        "AI LEAD @ FUERZA STUDIO",
        "SOFTWARE SOB MEDIDA, NÃO TEMPLATE",
        "GIVE US THE MESS. WE'LL GIVE YOU THE SHAPE.",
    ]
    n = len(lines)
    dur = 4.0                      # segundos por linha
    total = dur * n
    css, body = [], [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    for i, line in enumerate(lines):
        w = M.width(line, SIZE, TRACK)
        x = (W - w) / 2
        start = (i * dur) / total * 100
        typed = start + (dur * 0.45) / total * 100     # fim da digitacao
        hold = start + (dur * 0.86) / total * 100      # comeca a sair
        end = start + dur / total * 100
        # transform e opacity ficam em @keyframes separados de proposito: com os dois
        # no mesmo bloco, o keyframe de opacity parte o intervalo do transform e o
        # caret salta pro fim da linha em vez de acompanhar a digitacao.
        eps = 0.02
        css.append(f"""
@keyframes rev{i}{{
 0%,{start:.3f}%{{width:0}}
 {typed:.3f}%,{hold:.3f}%{{width:{w:.1f}px}}
 {end:.3f}%,100%{{width:0}}}}
@keyframes car{i}{{
 0%,{start:.3f}%{{transform:translateX(0)}}
 {typed:.3f}%,{hold:.3f}%{{transform:translateX({w:.1f}px)}}
 {end:.3f}%,100%{{transform:translateX(0)}}}}
@keyframes vis{i}{{
 0%,{start:.3f}%{{opacity:0}}
 {start + eps:.3f}%,{end - eps:.3f}%{{opacity:1}}
 {end:.3f}%,100%{{opacity:0}}}}
#c{i} rect{{animation:rev{i} {total}s steps({len(line)},end) infinite}}
#k{i}{{animation:car{i} {total}s steps({len(line)},end) infinite,
 vis{i} {total}s linear infinite}}""")
        body.append(
            f'<clipPath id="c{i}"><rect x="{x:.1f}" y="0" width="0" height="{H}"/></clipPath>'
            f'<g clip-path="url(#c{i})">'
            f'{text(M, line, SIZE, x, 44, ACID, TRACK)}</g>'
            f'<rect id="k{i}" x="{x:.1f}" y="26" width="10" height="22" fill="{BLUE}"/>')
    return svg(W, H, f"<style>{''.join(css)}</style>" + "".join(body),
               "CEO @ Overflow · AI Lead @ Fuerza Studio")


def build_stack(M):
    """Tags no primitivo da marca: mono uppercase, borda 1px, radius 0."""
    items = ["PYTHON", "TYPESCRIPT", "REACT", "NEXT.JS", "ASTRO",
             "SUPABASE", "POSTGRES", "PLAYWRIGHT", "DOCKER", "CLAUDE"]
    W, SIZE, TRACK = 1200, 13, 0.12
    padx, gap, bh, rowgap = 16, 10, 34, 10
    avail = W - 168
    widths = [(it, M.width(it, SIZE, TRACK) + padx * 2) for it in items]

    def pack(limit):
        rows, cur, curw = [], [], 0.0
        for it, w in widths:
            if cur and curw + gap + w > limit:
                rows.append((cur, curw))
                cur, curw = [], 0.0
            curw += (gap if cur else 0) + w
            cur.append((it, w))
        if cur:
            rows.append((cur, curw))
        return rows

    # aperta a largura util ate as linhas ficarem equilibradas: uma tag orfa
    # na ultima linha parece acidente, e o sistema nao erra por acidente
    rows = pack(avail)
    if len(rows) > 1:
        for limit in range(int(avail), int(avail * 0.55), -8):
            cand = pack(limit)
            if len(cand) == len(rows) and len(cand[-1][0]) >= len(cand[0][0]) - 1:
                rows = cand
                break

    H = 40 + len(rows) * bh + (len(rows) - 1) * rowgap + 40
    p = [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    y = 40
    for row, _ in rows:
        x = 84.0
        for i, (label, w) in enumerate(row):
            accent = (y == 40 and i == 0)      # um unico sinal acid
            fg = INK if accent else PAPER
            if accent:
                p.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{bh}" fill="{ACID}"/>')
            else:
                p.append(f'<rect x="{x + .5:.1f}" y="{y + .5}" width="{w - 1:.1f}" '
                         f'height="{bh - 1}" fill="none" stroke="{PAPER}" '
                         f'stroke-opacity="0.28" stroke-width="1"/>')
            p.append(text(M, label, SIZE, x + padx, y + bh / 2 + 4.5, fg, TRACK))
            x += w + gap
        y += bh + rowgap
    return svg(W, H, "".join(p), "stack")


def build_stats(G, M, data):
    W, H = 1200, 244
    p = [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    p.append(text(M, "// STATS", 13, 84, 54, ACID, 0.12))
    p.append(f'<rect x="84" y="72" width="{W - 168}" height="1" fill="{PAPER}" opacity="0.12"/>')

    cells = [("REPOS", data["repos"]), ("STARS", data["stars"]),
             ("FOLLOWERS", data["followers"])]
    if data["contributions"] is not None:
        cells.append(("CONTRIBUTIONS", data["contributions"]))
    x = 84
    for label, value in cells:
        p.append(text(G, str(value), 56, x, 152, PAPER, -0.025))
        p.append(text(M, label, 10, x, 178, MUTE, 0.10))
        x += 168

    # linguagens: acid sinaliza a primeira, azul segura o resto
    if data["langs"]:
        bx, bw, by = 700, W - 168 - (700 - 84), 118
        p.append(text(M, "// TOP LANGUAGES", 13, bx, 54, ACID, 0.12))
        cx = float(bx)
        for i, (name, pct) in enumerate(data["langs"]):
            seg = bw * pct / 100
            fill, op = (ACID, "1") if i == 0 else (BLUE, f"{max(0.9 - i * 0.13, 0.28):.2f}")
            p.append(f'<rect x="{cx:.1f}" y="{by}" width="{max(seg - 2, 1):.1f}" '
                     f'height="14" fill="{fill}" opacity="{op}"/>')
            cx += seg
        ly = by + 44
        for i, (name, pct) in enumerate(data["langs"]):
            col = bx + (i % 2) * 240
            row = ly + (i // 2) * 26
            fill, op = (ACID, "1") if i == 0 else (BLUE, f"{max(0.9 - i * 0.13, 0.28):.2f}")
            p.append(f'<rect x="{col}" y="{row - 8}" width="8" height="8" '
                     f'fill="{fill}" opacity="{op}"/>')
            p.append(text(M, f"{name.upper()} {pct:.1f}%", 11, col + 16, row, PAPER, 0.08))
    return svg(W, H, "".join(p), "stats")


def build_footer(M, wordmark_paths):
    W, H = 1200, 150
    p = [f'<rect width="{W}" height="{H}" fill="{INK}"/>',
         f'<rect x="0" y="0" width="{W}" height="4" fill="{BLUE}"/>']
    # wordmark oficial: escala uniforme a partir do viewBox 392x74 — sem esticar nem inclinar
    s = 168 / 392
    p.append(f'<g transform="translate(84 46) scale({s:.5f})" '
             f'fill="{PAPER}">{wordmark_paths}</g>')
    p.append(text(M, "WE ALL HAVE A PROBLEM TO SOLVE", 13, 84, H - 34, ACID, 0.12))
    right = "WELLYCHONFELIX.COM"
    p.append(text(M, right, 13, W - 84 - M.width(right, 13, 0.12), H - 34, MUTE, 0.12))
    return svg(W, H, "".join(p), "overflow — we all have a problem to solve")


def main():
    print("→ fontes")
    fonts = fetch_fonts()
    G = Shaper(fonts / "SpaceGrotesk.ttf", wght=700)
    M = Shaper(fonts / "JetBrainsMono.ttf", wght=500)
    S = Shaper(fonts / "InstrumentSerif-Italic.ttf")

    # o wordmark oficial usa <path ...></path>; extrai so o 'd' e remonta limpo
    wm = (ROOT / "assets" / "wordmark-src.svg").read_text(encoding="utf-8")
    ds = re.findall(r'<path\b[^>]*\bd="([^"]+)"', wm)
    if not ds:
        raise SystemExit("wordmark: nenhum path encontrado em assets/wordmark-src.svg")
    wordmark_paths = "".join(f'<path d="{d}"/>' for d in ds)

    print("→ dados")
    data = collect()
    print(f"  repos={data['repos']} stars={data['stars']} "
          f"followers={data['followers']} contrib={data['contributions']} "
          f"langs={[n for n, _ in data['langs']]}")

    ASSETS.mkdir(exist_ok=True)
    out = {
        "header.svg": build_header(G, M, S),
        "typing.svg": build_typing(M),
        "stack.svg": build_stack(M),
        "stats.svg": build_stats(G, M, data),
        "footer.svg": build_footer(M, wordmark_paths),
    }
    for name, content in out.items():
        (ASSETS / name).write_text(content, encoding="utf-8")
        print(f"  {name} ({len(content) // 1024} KB)")


if __name__ == "__main__":
    main()
