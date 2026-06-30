"""
merge.py — Identity resolution, chronological mutation, and scoring.

Assembles fully realised data dicts and instantiates CanonicalProfile models.
All mutation happens here — zero mutation inside the Pydantic model.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.normalize import (
    NormResult, normalize_country, normalize_date,
    normalize_phone, normalize_skill, normalize_year,
)
from src.schema import CanonicalProfile

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_W: dict[str, float] = {"csv": 0.90, "ats_json": 0.90, "github": 0.85, "resume": 0.70}
_CORROB = 0.10
_NORM_FAIL_P = 0.15
_CONFLICT_CAP = 0.40
_CHRONO_P = 0.15
_LEDGER = Path(__file__).resolve().parent.parent / "data" / "output" / "conflict_ledger.log"
_LEGAL_RE = re.compile(
    r"\s*,?\s*\b(inc|llc|ltd|corp|co|plc|gmbh|sa|ag|pty|pvt|limited"
    r"|incorporated|corporation|company)\.?\s*$", re.I,
)


def _ws(src: str) -> float:
    return _W.get(src, 0.70)


def _clamp(v: float) -> float:
    return round(min(1.0, max(0.0, v)), 4)


def _conf(nr: NormResult | None, src: str, corrob: int = 0) -> float:
    p = 0.0
    if nr and not nr.normalized:
        p += _NORM_FAIL_P
    if nr and nr.penalty != 0.0:
        p += abs(nr.penalty)
        p += abs(nr.penalty)
    return _clamp(_ws(src) + _CORROB * corrob - p)


# ── 1. Entity resolution (union-find) ───────────────────────────────────────

def _norm_emails(emails: list[str]) -> list[str]:
    return [e.strip().lower() for e in emails if e and e.strip()]


def _norm_phones(phones: list[str]) -> list[str]:
    out = []
    for p in phones:
        r = normalize_phone(p)
        if r.normalized:
            out.append(r.value)
    return out


def _group_fragments(frags: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    n = len(frags)
    idx_map: dict[str, set[int]] = defaultdict(set)
    for i, f in enumerate(frags):
        if str(f.get("source_identifier", "")).startswith("failed:"):
            continue
        for e in _norm_emails(f.get("emails", [])):
            idx_map[f"e:{e}"].add(i)
        for p in _norm_phones(f.get("phones", [])):
            idx_map[f"p:{p}"].add(i)
        rid = f.get("raw_extracted_id")
        id_synthetic = f.get("id_is_synthetic", False)
        if rid and str(rid).strip() and not id_synthetic:
            idx_map[f"id:{str(rid).strip()}"].add(i)

    par = list(range(n))

    def find(x: int) -> int:
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            par[ra] = rb

    for ids in idx_map.values():
        it = list(ids)
        for j in range(1, len(it)):
            union(it[0], it[j])

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        if str(frags[i].get("source_identifier", "")).startswith("failed:"):
            continue
        groups[find(i)].append(i)
    return [[frags[i] for i in v] for v in groups.values()]


def _candidate_id(group: list[dict[str, Any]]) -> str:
    # Deterministic candidate ID generation:
    # 1. Collect all normalized emails across group, sort alphabetically, hash the first one.
    emails = []
    for f in group:
        emails.extend(_norm_emails(f.get("emails", [])))
    emails = sorted(list(set(emails)))
    if emails:
        return hashlib.sha256(emails[0].encode()).hexdigest()[:16]

    # 2. Collect all E.164 phones across group, sort alphabetically, hash the first one.
    phones = []
    for f in group:
        phones.extend(_norm_phones(f.get("phones", [])))
    phones = sorted(list(set(phones)))
    if phones:
        return hashlib.sha256(phones[0].encode()).hexdigest()[:16]

    # 3. Collect all raw IDs across group, sort alphabetically, hash the first one.
    rids = []
    for f in group:
        rid = str(f.get("raw_extracted_id", "")).strip()
        if rid:
            rids.append(rid)
    rids = sorted(list(set(rids)))
    if rids:
        return hashlib.sha256(rids[0].encode()).hexdigest()[:16]

    return hashlib.sha256(b"unknown").hexdigest()[:16]


# ── 2. Conflict ledger ──────────────────────────────────────────────────────

def _log_conflict(field: str, dropped: Any, d_src: str, kept_src: str) -> None:
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    with open(_LEDGER, "a", encoding="utf-8") as fh:
        fh.write(
            f"[{ts}] CONFLICT field={field} dropped={dropped!r} "
            f"from={d_src} kept={kept_src}\n"
        )


# ── 3. Scalar resolution ────────────────────────────────────────────────────

def _resolve_scalar(
    field: str, cands: list[tuple[Any, str, NormResult | None]],
) -> tuple[Any, float, list[str], str]:
    """Returns (value, confidence, sources, method)."""
    # Fix #10: Drop any candidate whose resolved value is None before processing.
    cands = [
        (v, s, nr) for v, s, nr in cands
        if (nr.value if nr else v) is not None
    ]
    if not cands:
        return None, 0.0, [], "empty"
    cands.sort(key=lambda x: _ws(x[1]), reverse=True)
    norms = [(nr.value if nr else v, v, s, nr) for v, s, nr in cands]
    uniq = {str(nv).strip().lower() if isinstance(nv, str) else str(nv)
            for nv, *_ in norms if nv is not None}
    wv, _, ws, wnr = norms[0]
    if len(uniq) <= 1:
        corr = len({s for _, _, s, _ in norms}) - 1
        return wv, _conf(wnr, ws, max(0, corr)), \
            list(dict.fromkeys(s for _, _, s, _ in norms)), \
            "corroborated" if corr > 0 else "single_source"
    for _, raw, s, _ in norms[1:]:
        _log_conflict(field, raw, s, ws)
    return wv, min(_CONFLICT_CAP, _conf(wnr, ws)), [ws], "conflict_resolved"


# ── 4. Experience / Education normalisation ──────────────────────────────────

def _strip_legal(n: str) -> str:
    return _LEGAL_RE.sub("", n).strip().lower()


def _norm_exp(e: dict[str, Any], src: str) -> dict[str, Any]:
    if "raw_lines" in e and "company" not in e:
        return {"company": "Unspecified", "title": "Unspecified",
                "start": None, "end": None,
                "summary": " | ".join(e.get("raw_lines", [])),
                "_src": [src], "_conf": _ws(src)}
    snr, enr = normalize_date(e.get("start")), normalize_date(e.get("end"))
    sv, ev, pen = snr.value, enr.value, 0.0
    if sv and ev and isinstance(sv, str) and isinstance(ev, str) and ev < sv:
        ev, pen = None, _CHRONO_P
    c = _ws(src) - pen
    if not snr.normalized:
        c -= _NORM_FAIL_P
    if not enr.normalized:
        c -= _NORM_FAIL_P
    return {"company": e.get("company", "Unspecified"),
            "title": e.get("title", "Unspecified"),
            "start": sv, "end": ev,
            "summary": e.get("summary", ""),
            "_src": [src], "_conf": max(0.0, c)}


def _norm_edu(e: dict[str, Any], src: str) -> dict[str, Any]:
    if "raw_lines" in e and "institution" not in e:
        return {"institution": "Unspecified", "degree": "Unspecified",
                "field": "Unspecified", "end_year": None,
                "_src": [src], "_conf": _ws(src)}
    ynr = normalize_year(e.get("end_year"))
    c = _ws(src) - (0.0 if ynr.normalized else _NORM_FAIL_P)
    return {"institution": e.get("institution", "Unspecified"),
            "degree": e.get("degree", "Unspecified"),
            "field": e.get("field", "Unspecified"),
            "end_year": ynr.value,
            "_src": [src], "_conf": max(0.0, c)}


def _get_months(e: dict[str, Any], as_of: datetime.date) -> tuple[int, int]:
    s = _to_month(e.get("start"), as_of, is_start=True)
    en = _to_month(e.get("end"), as_of, is_start=False)
    
    en_val = en if en is not None else (as_of.year * 12 + as_of.month)
    s_val = s if s is not None else (en if en is not None else 0)
    
    return s_val, en_val


def _dedup_exp(
    entries: list[dict[str, Any]], as_of: datetime.date,
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for e in entries:
        comp_key = _strip_legal(e.get("company", ""))
        title_key = _strip_legal(e.get("title", ""))
        if comp_key == "unspecified":
            deduped.append(e)
            continue
        s_val, en_val = _get_months(e, as_of)

        dup_idx = -1
        for idx, ex in enumerate(deduped):
            if (
                _strip_legal(ex.get("company", "")) == comp_key
                and _strip_legal(ex.get("title", "")) == title_key
            ):
                s_ex, en_ex = _get_months(ex, as_of)
                if max(s_val, s_ex) <= min(en_val, en_ex):
                    dup_idx = idx
                    break

        if dup_idx != -1:
            old_entry = deduped[dup_idx]
            
            starts = [x for x in (old_entry.get("start"), e.get("start")) if x]
            merged_start = min(starts) if starts else None

            if old_entry.get("end") is None or e.get("end") is None:
                merged_end = None
            else:
                merged_end = max(old_entry.get("end"), e.get("end"))

            if e.get("_conf", 0.0) > old_entry.get("_conf", 0.0):
                new_entry = e.copy()
            else:
                new_entry = old_entry.copy()
                
            new_entry["start"] = merged_start
            new_entry["end"] = merged_end
            
            # Combine sources safely as lists
            cur_srcs = new_entry.get("_src", [])
            other_srcs = old_entry.get("_src", []) if new_entry is e else e.get("_src", [])
            if not isinstance(cur_srcs, list): cur_srcs = [cur_srcs]
            if not isinstance(other_srcs, list): other_srcs = [other_srcs]
            
            new_entry["_src"] = list(dict.fromkeys(cur_srcs + other_srcs))
                
            deduped[dup_idx] = new_entry
        else:
            deduped.append(e)
    return deduped


# ── 5. Years-of-experience (non-overlapping union) ──────────────────────────

def _to_month(d: str | None, as_of: datetime.date, is_start: bool = False) -> int | None:
    if d is None:
        if is_start:
            return None
        return as_of.year * 12 + as_of.month
    if re.fullmatch(r"\d{4}-\d{2}", d):
        y, m = d.split("-")
        return int(y) * 12 + int(m)
    if re.fullmatch(r"\d{4}", d):
        return int(d) * 12 + 1
    return None


def calc_years_exp(
    exp: list[dict[str, Any]], *, as_of: datetime.date | None = None,
) -> float | None:
    if as_of is None:
        as_of = datetime.date.today()
    ivs: list[tuple[int, int]] = []
    for e in exp:
        s = _to_month(e.get("start"), as_of, is_start=True)
        en = _to_month(e.get("end"), as_of, is_start=False)
        if s is not None and en is not None and en >= s:
            # Add 1 to make the end month inclusive
            ivs.append((s, en + 1))
    if not ivs:
        return None
    ivs.sort()
    merged = [ivs[0]]
    for s, e in ivs[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return round(sum(e - s for s, e in merged) / 12.0, 2)


# ── 6. Links merge ──────────────────────────────────────────────────────────

def _norm_url(url: str) -> str:
    """Normalize a URL for equality comparison.

    Strips scheme (http/https), leading 'www.', and trailing slash so that
    e.g. 'https://github.com/aayush' and 'github.com/Aayush' compare equal.
    """
    s = url.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    if s.lower().startswith("www."):
        s = s[4:]
    s = s.rstrip("/")
    return s.lower()


def _merge_links(
    pairs: list[tuple[dict[str, Any] | None, str]],
) -> tuple[dict[str, Any], float, list[str]]:
    merged: dict[str, Any] = {
        "linkedin": None, "github": None, "portfolio": None, "other": [],
    }
    kv: dict[str, list[tuple[Any, str]]] = defaultdict(list)
    srcs: list[str] = []
    confs: list[float] = []
    for ld, src in pairs:
        if not ld:
            continue
        if src not in srcs:
            srcs.append(src)
        for k, v in ld.items():
            if v is not None:
                kv[k].append((v, src))
    for k, vals in kv.items():
        if k == "other":
            all_o: list[str] = []
            for v, _ in vals:
                if isinstance(v, list):
                    all_o.extend(v)
                elif isinstance(v, str):
                    all_o.append(v)
            merged["other"] = list(dict.fromkeys(all_o))
        elif len(vals) == 1:
            merged[k] = vals[0][0]
            confs.append(_ws(vals[0][1]))
        else:
            # Fix #11: Normalize URLs before equality comparison.
            uniq = {_norm_url(str(v)) for v, _ in vals}
            if len(uniq) == 1:
                merged[k] = vals[0][0]
                corr = len({s for _, s in vals}) - 1
                confs.append(_clamp(_ws(vals[0][1]) + _CORROB * corr))
            else:
                vals.sort(key=lambda x: _ws(x[1]), reverse=True)
                merged[k] = vals[0][0]
                confs.append(_CONFLICT_CAP)
                for v, s in vals[1:]:
                    _log_conflict(f"links.{k}", v, s, vals[0][1])
    avg = sum(confs) / len(confs) if confs else 0.0
    return merged, avg, srcs


# ── 7. Skills merge ─────────────────────────────────────────────────────────

def _merge_skills(
    pairs: list[tuple[list[str], str]],
) -> tuple[list[dict[str, Any]], float]:
    smap: dict[str, dict[str, Any]] = {}
    for skills, src in pairs:
        for raw in skills:
            nr = normalize_skill(raw)
            key = nr.value.strip().lower()
            c = _conf(nr, src)
            if key in smap:
                if src not in smap[key]["sources"]:
                    smap[key]["sources"].append(src)
                    smap[key]["confidence"] = _clamp(smap[key]["confidence"] + _CORROB)
            else:
                smap[key] = {"name": nr.value, "confidence": c, "sources": [src]}
    skills = list(smap.values())
    avg = sum(s["confidence"] for s in skills) / len(skills) if skills else 0.0
    return skills, avg


# ── 8. Assembly ──────────────────────────────────────────────────────────────

def merge_fragments(
    frag_dicts: list[dict[str, Any]],
    *,
    as_of: datetime.date | None = None,
) -> tuple[list[CanonicalProfile], int]:
    """Main entry: merge accumulated fragment dicts → CanonicalProfile list.

    Returns
    -------
    tuple[list[CanonicalProfile], int]
        A tuple of (profiles, failed_candidate_count).  The count tracks
        groups that could not produce even a minimal degraded profile.
    """
    if as_of is None:
        as_of = datetime.date.today()
    groups = _group_fragments(frag_dicts)
    profiles: list[CanonicalProfile] = []
    failed_count = 0
    for g in groups:
        cid = _candidate_id(g)
        try:
            profiles.append(_assemble(g, as_of=as_of))
        except Exception as exc:
            logger.error("merge: assembly failed for %s — %s", cid, exc)
            # Fix #13: Attempt a minimal degraded profile instead of dropping.
            try:
                degraded = _assemble_degraded(g, cid, as_of=as_of)
                profiles.append(degraded)
                logger.warning(
                    "merge: produced degraded profile for %s", cid,
                )
            except Exception as exc2:
                logger.error(
                    "merge: degraded assembly also failed for %s — %s",
                    cid, exc2,
                )
                failed_count += 1
    logger.info(
        "merge: %d profiles from %d groups (%d failed)",
        len(profiles), len(groups), failed_count,
    )
    return profiles, failed_count


def _assemble(
    group: list[dict[str, Any]], *, as_of: datetime.date,
) -> CanonicalProfile:
    cid = _candidate_id(group)
    fm: dict[str, Any] = {}
    prov: list[dict[str, Any]] = []

    def _t(field: str, c: float, srcs: list[str], method: str, val: Any) -> None:
        if val is None or val == [] or val == {}:
            return
        if field in ("location", "links") and isinstance(val, dict):
            if all(v is None or v == [] for v in val.values()):
                return
        fm[field] = {"confidence": c, "sources": srcs, "method": method}
        prov.append({"field": field, "source": srcs, "method": method})

    # ── name ──
    nc = [(f["full_name"], f["source_identifier"], None)
          for f in group if f.get("full_name")]
    nv, nc_, ns, nm = _resolve_scalar("full_name", nc)
    _t("full_name", nc_, ns, nm, nv)

    # ── emails ──
    emails: list[str] = []
    esrcs: list[str] = []
    for f in group:
        for e in _norm_emails(f.get("emails", [])):
            if e not in emails:
                emails.append(e)
            s = f["source_identifier"]
            if s not in esrcs:
                esrcs.append(s)
    # Fix 7: Use highest source weight for base confidence
    ebase = max([_ws(s) for s in esrcs]) if esrcs else 0.0
    _t("emails", _clamp(ebase + _CORROB * max(0, len(esrcs) - 1)), esrcs, "union", emails)

    # ── phones ──
    phones: list[str] = []
    psrcs: list[str] = []
    pp = 0.0
    for f in group:
        for p in f.get("phones", []):
            nr = normalize_phone(p)
            if nr.value and nr.value not in phones:
                phones.append(nr.value)
            if not nr.normalized:
                pp = max(pp, _NORM_FAIL_P)
            s = f["source_identifier"]
            if s not in psrcs:
                psrcs.append(s)
    pbase = max([_ws(s) for s in psrcs]) if psrcs else 0.0
    _t("phones", _clamp(pbase + _CORROB * max(0, len(psrcs) - 1) - pp), psrcs, "e164", phones)

    # ── location ──
    cities, regions, countries = [], [], []
    for f in group:
        lv = f.get("location_raw")
        if not lv or not isinstance(lv, str):
            continue
        src = f["source_identifier"]
        nr = normalize_country(lv)
        cc = nr.value
        if cc:
            countries.append((cc, src, nr))
        
        parts = [p.strip() for p in lv.split(",") if p.strip()]
        if len(parts) == 1:
            if not cc:
                cities.append((parts[0], src, None))
        elif len(parts) == 2:
            cities.append((parts[0], src, None))
            cr = normalize_country(parts[1])
            if not cr.normalized or cr.value != cc:
                regions.append((parts[1], src, None))
        elif len(parts) >= 3:
            cities.append((parts[0], src, None))
            regions.append((parts[1], src, None))

    city_v, city_c, city_s, city_m = _resolve_scalar("location.city", cities)
    reg_v, reg_c, reg_s, reg_m = _resolve_scalar("location.region", regions)
    coun_v, coun_c, coun_s, coun_m = _resolve_scalar("location.country", countries)
    
    location: dict[str, Any] = {"city": city_v, "region": reg_v, "country": coun_v}
    
    loc_confs = [c for c in (city_c, reg_c, coun_c) if c > 0.0]
    lconf = sum(loc_confs) / len(loc_confs) if loc_confs else 0.0
    ls = list(dict.fromkeys(city_s + reg_s + coun_s))
    lm = "component_resolution"
    _t("location", lconf, ls, lm, location)

    # ── headline ──
    hc = [(f["headline"], f["source_identifier"], None)
          for f in group if f.get("headline")]
    hv, hconf, hs, hm = _resolve_scalar("headline", hc)
    _t("headline", hconf, hs, hm, hv)

    # ── skills ──
    sp = [(f.get("skills_raw", []), f["source_identifier"])
          for f in group if f.get("skills_raw")]
    skills, sk_conf = _merge_skills(sp)
    _t("skills", sk_conf, list(dict.fromkeys(s for _, s in sp)), "taxonomy", skills)

    # ── links ──
    lp = [(f.get("links_raw"), f["source_identifier"]) for f in group]
    links, lk_conf, lk_srcs = _merge_links(lp)
    _t("links", lk_conf, lk_srcs, "dict_union", links)

    # ── experience ──
    all_exp: list[dict[str, Any]] = []
    for f in group:
        for e in f.get("experience_raw", []):
            all_exp.append(_norm_exp(e, f["source_identifier"]))
    all_exp = _dedup_exp(all_exp, as_of)
    exp_confs = [e.pop("_conf", 0.0) for e in all_exp]
    exp_srcs: list[str] = []
    for s_val in [e.pop("_src", []) for e in all_exp]:
        if isinstance(s_val, list):
            exp_srcs.extend(s_val)
        elif isinstance(s_val, str) and s_val:
            exp_srcs.append(s_val)
    exp_srcs_uniq = list(dict.fromkeys(exp_srcs))
    _t("experience",
       sum(exp_confs) / len(exp_confs) if exp_confs else 0.0,
       exp_srcs_uniq, "chrono_dedup", all_exp)

    # ── education ──
    all_edu: list[dict[str, Any]] = []
    for f in group:
        for e in f.get("education_raw", []):
            all_edu.append(_norm_edu(e, f["source_identifier"]))
    edu_confs = [e.pop("_conf", 0.0) for e in all_edu]
    edu_srcs: list[str] = []
    for s_val in [e.pop("_src", []) for e in all_edu]:
        if isinstance(s_val, list):
            edu_srcs.extend(s_val)
        elif isinstance(s_val, str) and s_val:
            edu_srcs.append(s_val)
    _t("education",
       sum(edu_confs) / len(edu_confs) if edu_confs else 0.0,
       list(dict.fromkeys(edu_srcs)), "year_norm", all_edu)

    # ── years_experience ──
    # Fix #12: Derive confidence from contributing entries, not a hardcoded 0.85.
    # Also penalise year-only dates that silently assume January.
    yrs = calc_years_exp(all_exp, as_of=as_of)
    if yrs is not None:
        # Identify which entries contributed to the interval union
        contrib_confs: list[float] = []
        for i, e in enumerate(all_exp):
            s = _to_month(e.get("start"), as_of, is_start=True)
            en = _to_month(e.get("end"), as_of, is_start=False)
            if s is not None and en is not None and en >= s:
                c = exp_confs[i] if i < len(exp_confs) else 0.0
                # Apply extra penalty for year-only dates (month imprecision)
                sd = e.get("start")
                ed = e.get("end")
                if sd and re.fullmatch(r"\d{4}", sd):
                    c = max(0.0, c - 0.1)
                if ed and re.fullmatch(r"\d{4}", ed):
                    c = max(0.0, c - 0.1)
                contrib_confs.append(c)
        yrs_conf = sum(contrib_confs) / len(contrib_confs) if contrib_confs else 0.0
    else:
        yrs_conf = 0.0
    _t("years_experience", yrs_conf, exp_srcs_uniq, "interval_union", yrs)

    # ── overall_confidence ──
    field_confs = [v["confidence"] for v in fm.values()]
    overall = sum(field_confs) / len(field_confs) if field_confs else 0.0
    overall = _clamp(round(overall, 4))

    # ── Assemble dict, then instantiate model (NO mutation inside model) ──
    assembled = {
        "candidate_id": cid,
        "full_name": nv or "Unknown",
        "emails": emails,
        "phones": phones,
        "location": location,
        "links": links,
        "headline": hv,
        "years_experience": yrs,
        "skills": skills,
        "experience": all_exp,
        "education": all_edu,
        "overall_confidence": overall,
        "provenance": prov,
        "field_metadata": fm,
    }
    return CanonicalProfile(**assembled)


def _assemble_degraded(
    group: list[dict[str, Any]], cid: str, *, as_of: datetime.date,
) -> CanonicalProfile:
    """Produce a minimal degraded profile when full assembly raises a ValueError.

    Preserves scalar fields (name, emails, phones, location, headline) and
    clears array fields (skills, experience, education) that may have caused
    the validation failure.  Sets overall_confidence near 0.0.
    """
    emails: list[str] = []
    for f in group:
        for e in _norm_emails(f.get("emails", [])):
            if e not in emails:
                emails.append(e)

    phones: list[str] = []
    for f in group:
        for p in f.get("phones", []):
            nr = normalize_phone(p)
            if nr.value and nr.value not in phones:
                phones.append(nr.value)

    names = [f.get("full_name") for f in group if f.get("full_name")]
    full_name = names[0] if names else "Unknown"

    return CanonicalProfile(
        candidate_id=cid,
        full_name=full_name,
        emails=emails,
        phones=phones,
        location={"city": None, "region": None, "country": None},
        links={"linkedin": None, "github": None, "portfolio": None, "other": []},
        headline=None,
        years_experience=None,
        skills=[],
        experience=[],
        education=[],
        overall_confidence=0.01,
        provenance=[],
        field_metadata={"degraded": {"confidence": 0.01, "sources": [], "method": "degraded"}},
    )
