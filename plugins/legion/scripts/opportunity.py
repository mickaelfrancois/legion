"""Opportunites hors-perimetre de legion : materialiser en issues GitHub de suivi les
ameliorations/anomalies reperees pendant une battle mais **hors du scope** de la feature
traitee. C'est la 3e voie de capitalisation du REFLECT, a cote du learning code (memoire
projet) et du RETEX outillage (`plugin_retex.py`, journal central).

Distinct de `plugin_retex.py` : ici la cible est le **repo cible** (pas le plugin), la
sortie est une **issue GitHub** (pas un journal JSONL), et il n'y a **pas de tombstone** —
la fermeture native de l'issue GitHub joue ce role (une opportunite traitee/refusee =
issue fermee, jamais recreee).

Comme `base_freshness.py`, le coeur est **pur** (ni reseau ni disque) : `_fingerprint`,
`_normalize`, `dedup`, `render` se testent hermetiquement via `--self-test`. Les appels
`gh` (creer le label, lister/creer les issues) restent a l'orchestrateur (`retro.md`),
jamais ici (doctrine `ARCHITECTURE.md` §8 : les scripts n'appellent pas le reseau).

Anti-doublon a deux filets (spec §Anti-doublon) :
- **Fingerprint** (dur, deterministe) : `sha1(normalize(zone)|normalize(observation))[:12]`,
  embarque dans le corps via `<!-- legion-opportunity: <fp> -->`. Un candidat dont le fp
  figure deja dans une issue (ouverte **ou fermee**) est un doublon => saute.
- **Semantique** (advisory) : chevauchement des mots du titre avec une issue **ouverte**.
  Signale (`probable`) pour que l'humain tranche — ne saute jamais tout seul (evite les
  faux positifs).

Sous-commandes :
    python opportunity.py dedup   [--file <in.json>]                  # stdin sinon
    python opportunity.py render  --file <candidate.json> --battle B --origin-issue N
    python opportunity.py --self-test

`dedup` : entree `{ "candidates": [...], "issues": [...] }`, sortie
`{ "to_create": [...], "duplicates": [...], "probable": [...] }`.
Un candidat : `{ title, zone, kind?, observation, out_of_scope?, lead?, phase? }`
(requis : title, zone, observation — sinon ignore). Une issue (de
`gh issue list --json number,title,body,state`) : `{ number, title, body, state }`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

_REQUIRED = ("title", "zone", "observation")

# Suffixe `:ligne` ou `:ligne:colonne` — neutralise dans le fingerprint (C1 option B) :
# les numeros de ligne derivent entre battles autant que les horodatages deja exclus.
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?")

# Marqueur embarque dans le corps d'issue — pivot de l'idempotence anti-doublon.
_MARKER_RE = re.compile(r"<!--\s*legion-opportunity:\s*([0-9a-f]{12})\s*-->")

# Mots vides FR/EN ignores par le filet semantique (chevauchement de titres).
_STOPWORDS = frozenset(
    "les des une dans pour avec sans sur par au aux the and for with les que qui "
    "issue battle legion hors scope perimetre".split()
)

# --------------------------------------------------------------------------- #
# Coeur pur — fingerprint, normalisation, marqueur                            #
# --------------------------------------------------------------------------- #

def _normalize(text: str) -> str:
    """Normalise pour le fingerprint : retire les suffixes `:ligne(:col)`, ecrase les
    espaces, casefold. Deux formulations equivalentes a une ligne pres donnent la meme
    graine — donc le meme fp — a travers les battles (spec : reproductible cross-battle)."""
    s = _LINE_SUFFIX_RE.sub("", str(text or ""))
    return " ".join(s.split()).casefold()


def _fingerprint(zone: str, observation: str) -> str:
    """Fp stable d'une opportunite : `sha1(normalize(zone)|normalize(observation))[:12]`.
    **Ni horodatage ni id de battle** dans la graine (reproductible cross-battle)."""
    seed = f"{_normalize(zone)}|{_normalize(observation)}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _marker(fp: str) -> str:
    return f"<!-- legion-opportunity: {fp} -->"


def _markers_in(text: str) -> set[str]:
    """Tous les fingerprints presents dans un texte (corps d'issue)."""
    return set(_MARKER_RE.findall(str(text or "")))


def _tokens(title: str) -> set[str]:
    """Mots significatifs d'un titre (>= 4 lettres, hors stopwords) pour le filet
    semantique. Casefold + retrait de la ponctuation."""
    words = re.findall(r"[^\W\d_]{4,}", str(title or ""), flags=re.UNICODE)
    return {w.casefold() for w in words if w.casefold() not in _STOPWORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    """Chevauchement relatif au plus petit ensemble : |a∩b| / min(|a|,|b|). 0 si l'un
    est vide. Advisory — sert seulement a signaler un doublon **probable**."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# Seuil de chevauchement au-dela duquel un titre est signale comme doublon probable.
_PROBABLE_THRESHOLD = 0.6


def _valid(candidate: dict) -> dict | None:
    """Retourne le candidat complete de son `fingerprint`, ou None si un champ requis
    manque (title/zone/observation) — calque `plugin_retex._normalize`."""
    if not isinstance(candidate, dict):
        return None
    if any(not str(candidate.get(k, "")).strip() for k in _REQUIRED):
        return None
    out = dict(candidate)
    out["fingerprint"] = _fingerprint(candidate["zone"], candidate["observation"])
    return out


# --------------------------------------------------------------------------- #
# dedup — classe les candidats vs les issues existantes                       #
# --------------------------------------------------------------------------- #

def dedup(candidates: list, issues: list) -> dict:
    """Classe chaque candidat valide en `to_create` / `duplicates` / `probable`.

    - `duplicates` : fp deja present dans une issue (ouverte **ou fermee**) — saute dur.
    - `to_create`  : fp neuf ; dedup intra-lot (deux candidats de meme fp => une entree).
    - `probable`   : sous-ensemble **advisory** de `to_create` dont le titre chevauche
                     celui d'une issue **ouverte** (>= seuil) — l'humain tranche.
    """
    issues = issues if isinstance(issues, list) else []
    known_fps: set[str] = set()
    open_titles: list[tuple[int, str, set[str]]] = []
    for iss in issues:
        if not isinstance(iss, dict):
            continue
        known_fps |= _markers_in(iss.get("body", ""))
        if str(iss.get("state", "")).lower() == "open":
            open_titles.append(
                (iss.get("number"), iss.get("title", ""), _tokens(iss.get("title", "")))
            )

    to_create: list[dict] = []
    duplicates: list[dict] = []
    probable: list[dict] = []
    seen_in_batch: set[str] = set()

    for raw in candidates if isinstance(candidates, list) else []:
        cand = _valid(raw)
        if cand is None:
            continue
        fp = cand["fingerprint"]
        if fp in known_fps:
            duplicates.append(cand)
            continue
        if fp in seen_in_batch:
            continue  # dedup intra-lot : deux candidats identiques => une seule issue
        seen_in_batch.add(fp)
        to_create.append(cand)

        cand_tokens = _tokens(cand["title"])
        overlaps = [
            {"issue_number": num, "issue_title": title,
             "score": round(_overlap(cand_tokens, toks), 3)}
            for (num, title, toks) in open_titles
            if _overlap(cand_tokens, toks) >= _PROBABLE_THRESHOLD
        ]
        if overlaps:
            probable.append({"fingerprint": fp, "title": cand["title"],
                             "overlaps": overlaps})

    return {"to_create": to_create, "duplicates": duplicates, "probable": probable}


# --------------------------------------------------------------------------- #
# render — corps d'issue Markdown FR (source unique du format + du marqueur)   #
# --------------------------------------------------------------------------- #

def render(candidate: dict, battle: str | None, origin_issue: str | None) -> str:
    """Corps d'issue Markdown FR pour un candidat. Le marqueur fingerprint est
    **toujours** present et bien forme (tout le filet anti-doublon en depend). La phase
    est portee par le candidat (une battle a des candidats de phases differentes)."""
    cand = _valid(candidate)
    if cand is None:
        raise ValueError("candidat incomplet : title, zone et observation sont requis")
    fp = cand["fingerprint"]
    phase = str(cand.get("phase") or "?")
    kind = str(cand.get("kind") or "amélioration")
    origin = f"#{origin_issue}" if origin_issue else "(inconnue)"
    lead = str(cand.get("lead") or "_À investiguer par l'agent qui reprendra l'issue._")
    why = str(cand.get("out_of_scope")
              or "Repérée pendant une autre battle ; hors du périmètre de la feature traitée.")
    lines = [
        f"_Issue de suivi générée par legion — battle `{battle or '?'}`, "
        f"repérée en phase **{phase}**, issue d'origine {origin}._",
        "",
        "## Origine",
        f"Opportunité **{kind}** repérée dans la zone `{cand['zone']}` pendant la battle "
        f"`{battle or '?'}` (issue d'origine {origin}), en phase **{phase}**.",
        "",
        "## Problème / amélioration",
        cand["observation"],
        "",
        "## Pourquoi hors périmètre",
        why,
        "",
        "## Piste de résolution",
        lead,
        "",
        "## Prochaine étape",
        "`/legion:recon <ce-numéro>` pour affûter, puis `/legion:battle start <ce-numéro>`.",
        "",
        _marker(fp),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _read_json(path: str | None) -> object:
    """Lit un JSON depuis `--file` ou, a defaut, stdin."""
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    if not args:
        print("usage: opportunity.py dedup [--file F] | render --file F "
              "[--battle B] [--origin-issue N] | --self-test", file=sys.stderr)
        return 1

    def opt(name: str) -> str | None:
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

    cmd = args[0]

    if cmd == "dedup":
        try:
            data = _read_json(opt("--file"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"lecture de l'entree impossible: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, dict):
            print("dedup attend un objet { candidates, issues }", file=sys.stderr)
            return 2
        result = dedup(data.get("candidates", []), data.get("issues", []))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if cmd == "render":
        try:
            cand = _read_json(opt("--file"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"lecture du candidat impossible: {exc}", file=sys.stderr)
            return 2
        try:
            print(render(cand, opt("--battle"), opt("--origin-issue")))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    print(f"commande inconnue: {cmd}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# self-test — hermetique (stdlib seule, aucun reseau/disque)                  #
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    # --- fingerprint deterministe : meme (zone, observation) => meme fp, 12 hex ---
    fp1 = _fingerprint("Billing.Api/X.cs", "logique de TVA dans la couche Api")
    fp2 = _fingerprint("Billing.Api/X.cs", "logique de TVA dans la couche Api")
    assert fp1 == fp2 and len(fp1) == 12 and all(c in "0123456789abcdef" for c in fp1), fp1

    # --- stable cross-battle : le contexte (phase/battle/title) n'entre pas dans le fp ---
    c_a = {"title": "T1", "zone": "z/a.cs", "observation": "obs", "phase": "review"}
    c_b = {"title": "AUTRE TITRE", "zone": "z/a.cs", "observation": "obs", "phase": "test"}
    assert _valid(c_a)["fingerprint"] == _valid(c_b)["fingerprint"]

    # --- normalisation : casse + espaces ---
    assert _fingerprint("Foo.cs", "A  B") == _fingerprint("foo.cs", "a b")
    # --- normalisation : numeros de ligne neutralises (C1 option B) ---
    assert _fingerprint("Foo.cs:42", "bug ici Foo.cs:42") == _fingerprint("Foo.cs:58", "bug ici Foo.cs:58")
    # deux problemes distincts dans le meme fichier restent distincts (phrasé different)
    assert _fingerprint("Foo.cs", "fuite memoire") != _fingerprint("Foo.cs", "race condition")

    # --- candidat incomplet ignore ---
    assert _valid({"title": "t", "zone": "z"}) is None            # observation manquante
    assert _valid({"zone": "z", "observation": "o"}) is None       # title manquant
    assert _valid({"title": " ", "zone": "z", "observation": "o"}) is None  # blanc

    # --- dedup : fp match issue OUVERTE => duplicates ---
    cand = {"title": "TVA dans Api", "zone": "Billing.Api/X.cs",
            "observation": "logique de TVA dans la couche Api", "phase": "review"}
    fp = _valid(cand)["fingerprint"]
    open_iss = {"number": 7, "title": "vieux", "body": f"blah {_marker(fp)}", "state": "OPEN"}
    r = dedup([cand], [open_iss])
    assert not r["to_create"] and len(r["duplicates"]) == 1, r
    assert r["duplicates"][0]["fingerprint"] == fp

    # --- dedup : fp match issue FERMEE => duplicates (idempotent vs traite/refuse) ---
    closed_iss = {"number": 8, "title": "vieux", "body": _marker(fp), "state": "CLOSED"}
    r = dedup([cand], [closed_iss])
    assert not r["to_create"] and len(r["duplicates"]) == 1, r

    # --- dedup : no-match => to_create, porte son fingerprint ---
    r = dedup([cand], [{"number": 9, "title": "sans rapport", "body": "rien", "state": "OPEN"}])
    assert len(r["to_create"]) == 1 and r["to_create"][0]["fingerprint"] == fp
    assert not r["duplicates"]

    # --- dedup : intra-lot, 2 candidats identiques => une seule entree ---
    r = dedup([cand, dict(cand)], [])
    assert len(r["to_create"]) == 1, r

    # --- dedup : candidat incomplet dans le lot => ignore, pas de crash ---
    r = dedup([cand, {"title": "x"}], [])
    assert len(r["to_create"]) == 1, r

    # --- marqueur absent/malforme dans une issue => ignore sans crash ---
    assert _markers_in("aucun marqueur ici") == set()
    assert _markers_in("<!-- legion-opportunity: xyz -->") == set()   # pas 12 hex => ignore
    assert _markers_in(f"a {_marker(fp)} b") == {fp}

    # --- filet semantique : titre chevauche une issue OUVERTE => probable (advisory) ---
    cand_sem = {"title": "Refactor du calculateur de facturation TVA",
                "zone": "z/new.cs", "observation": "phrasé totalement different ici",
                "phase": "review"}
    open_sim = {"number": 10, "title": "Refactor calculateur facturation",
                "body": "corps", "state": "OPEN"}
    r = dedup([cand_sem], [open_sim])
    assert len(r["to_create"]) == 1, r                 # advisory : reste a creer
    assert len(r["probable"]) == 1 and r["probable"][0]["overlaps"], r

    # --- filet semantique : issue FERMEE de titre proche => NON signalee (open-only) ---
    closed_sim = {"number": 11, "title": "Refactor calculateur facturation",
                  "body": "corps", "state": "CLOSED"}
    r = dedup([cand_sem], [closed_sim])
    assert not r["probable"], r

    # --- render : corps contient marqueur+fp, Origine, observation, Prochaine etape ---
    body = render(cand, "2026-07-02-GH-42", "42")
    assert _marker(fp) in body, body
    assert "## Origine" in body and "## Prochaine étape" in body
    assert "/legion:recon" in body and "/legion:battle start" in body
    assert cand["observation"] in body
    assert _markers_in(body) == {fp}                   # boucle render->parse coherente

    # --- render : candidat incomplet => ValueError (jamais de corps sans fp) ---
    try:
        render({"title": "x"}, "b", "1")
        assert False, "render aurait du lever ValueError"
    except ValueError:
        pass

    print("OK: opportunity self-test passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Reconfigure stdin aussi : `dedup` lit un JSON accentue depuis stdin quand `--file`
    # est absent ; sans ca, une console cp1252 corrompt les accents a la lecture.
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # accents FR : lecture stdin + console cp1252
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
