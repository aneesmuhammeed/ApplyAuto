import re
import numpy as np
from typing import Dict, Any, Optional, List, Tuple
from database import (
    normalize_question_key, generate_question_key, touch_saved_answer,
    get_persisted_embeddings, save_persisted_embedding
)
from field_rules import get_field_match_by_rules
try:
    from widget_executors import match_option_candidate
except ImportError:
    from backend.widget_executors import match_option_candidate

class LookupStage:
    EXACT_MEMORY = "exact_memory"
    SIMILARITY_EMBEDDINGS = "similarity_embeddings"
    RESUME_PROFILE = "resume_profile"
    NEEDS_INPUT = "needs_input"

# -----------------------------------------------------------------------------
# 1. FAST-EMBED SINGLETON & FALLBACK HANDLING
# -----------------------------------------------------------------------------
_EMBEDDING_LOAD_ATTEMPTED = False
_EMBEDDING_MODEL = None

def get_embedding_model():
    """
    Retrieve or initialize the cached local TextEmbedding model.
    Falls back to exact-only matching with a clear log line if the model fails to load.
    """
    global _EMBEDDING_MODEL, _EMBEDDING_LOAD_ATTEMPTED
    if _EMBEDDING_LOAD_ATTEMPTED:
        return _EMBEDDING_MODEL

    _EMBEDDING_LOAD_ATTEMPTED = True
    try:
        from fastembed import TextEmbedding
        _EMBEDDING_MODEL = TextEmbedding()
        print("[FieldLookup] FastEmbed TextEmbedding model initialized successfully.")
    except Exception as e:
        print(f"[FieldLookup Warning] FastEmbed model failed to load ({e}). Falling back to exact-only memory matching.")
        _EMBEDDING_MODEL = None
    return _EMBEDDING_MODEL

# -----------------------------------------------------------------------------
# 2. PERSISTENT EMBEDDING CACHE (KEYED BY NORMALIZED PATTERN)
# -----------------------------------------------------------------------------
_PATTERN_EMB_CACHE: Dict[str, np.ndarray] = {}
_PERSISTENCE_INITIALIZED = False

def init_persistent_cache_if_needed():
    """Load precomputed embeddings from SQLite into memory on first access."""
    global _PATTERN_EMB_CACHE, _PERSISTENCE_INITIALIZED
    if not _PERSISTENCE_INITIALIZED:
        try:
            persisted = get_persisted_embeddings()
            _PATTERN_EMB_CACHE.update(persisted)
            if persisted:
                print(f"[FieldLookup] Loaded {len(persisted)} precomputed pattern embedding(s) from persistent SQLite cache.")
        except Exception as e:
            print(f"[FieldLookup Warning] Failed loading persisted embeddings: {e}")
        _PERSISTENCE_INITIALIZED = True

def get_or_create_pattern_embedding(pattern: str, emb_model) -> Optional[np.ndarray]:
    """
    Returns the embedding vector for a pattern, caching it in memory and SQLite
    keyed by normalized pattern so fastembed does not re-embed on each restart.
    """
    global _PATTERN_EMB_CACHE
    init_persistent_cache_if_needed()

    norm_pat = normalize_question_key(pattern)
    if not norm_pat:
        return None

    if norm_pat in _PATTERN_EMB_CACHE:
        return _PATTERN_EMB_CACHE[norm_pat]

    if emb_model is None:
        return None

    try:
        vec = np.array(list(emb_model.embed([pattern]))[0], dtype=np.float32)
        _PATTERN_EMB_CACHE[norm_pat] = vec
        try:
            save_persisted_embedding(norm_pat, vec)
        except Exception as se:
            print(f"[FieldLookup Warning] Could not persist embedding for '{norm_pat}': {se}")
        return vec
    except Exception as e:
        print(f"[FieldLookup Error] Generating embedding for pattern '{pattern}': {e}")
        return None

def compute_cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """Compute cosine similarity between two 1D vectors."""
    dot = np.dot(vec_a, vec_b)
    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))

# -----------------------------------------------------------------------------
# 3. OPTION VALIDATION & REJECTION
# -----------------------------------------------------------------------------
def find_best_option_match(target_val: str, options: List[str]) -> Optional[str]:
    """
    Match a resolved value to the closest matching option in a dropdown or radio list
    using exact, normalized, and token-based fuzzy matching with tie rejection.
    Returns None if no option matches or if there is an ambiguous tie.
    Strictly rejects answers that do not match one of the available options.
    """
    if not options or not target_val:
        return None

    candidates = [(opt, str(opt).strip()) for opt in options if opt is not None and str(opt).strip()]
    if not candidates:
        return None

    match = match_option_candidate(str(target_val), candidates)
    if match:
        return match[1]
    return None

# -----------------------------------------------------------------------------
# 4. EXACT-MATCH-ONLY CATEGORIES (YES/NO, SALARY, NOTICE, LEGAL)
# -----------------------------------------------------------------------------
EXACT_ONLY_PATTERNS = [
    # 1. Salary / CTC / Compensation (current vs expected vs rate)
    r'\b(salary|compensation|ctc|pay|rate|remuneration|wage|stipend)\b',
    # 2. Notice period / Availability (days vs period)
    r'\b(notice\s*period|available\s*to\s*join|how\s*soon|notice\s*days|joining\s*date)\b',
    # 3. Legal / Compliance / Citizenship / Work authorization / Clearances
    r'\b(visa|sponsorship|authorized\s*to\s*work|work\s*authorization|citizenship|alien|legally\s*authorized)\b',
    r'\b(criminal|felony|misdemeanor|background\s*check|drug\s*test|security\s*clearance)\b',
    r'\b(ssn|passport|national\s*id|social\s*security|disability|veteran)\b',
    # 4. Yes/No and Relocation/Travel Questions
    r'\b(willing\s*to\s*(relocate|travel)|relocation|relocate|travel)\b',
    r'\b(are\s*you\s*(willing|able|authorized|currently|legally))\b',
    r'\b(do\s*you\s*(have|require|need|possess|hold))\b',
    r'\b(have\s*you\s*(ever|previously|been))\b',
    r'\b(will\s*you\s*(now|require|in\s*the\s*future))\b'
]
EXACT_ONLY_REGEX = re.compile('|'.join(EXACT_ONLY_PATTERNS), re.IGNORECASE)

def is_exact_match_only(label: str, field_type: str = "text", options: Optional[List[str]] = None) -> bool:
    """
    Determines if a field must be matched using EXACT NORMALIZED match only,
    completely bypassing embedding similarity.
    Enforced for yes/no, salary, notice-period, and legal/compliance questions
    where embedding cosine similarity produces dangerous false positives
    (e.g. 'willing to relocate' vs 'willing to travel', 'current CTC' vs 'expected CTC').
    """
    if not label:
        return False
    lbl = label.lower().strip()
    if EXACT_ONLY_REGEX.search(lbl):
        return True

    # Check if field_type or options indicate binary/boolean or radio choices
    if field_type in ["radio", "checkbox"] and options:
        opt_set = {str(o).strip().lower() for o in options if o is not None}
        if {"yes", "no"}.issubset(opt_set) or opt_set.issubset({"yes", "no", "true", "false", "y", "n", "prefer not to say", "not applicable"}):
            return True

    return False

# -----------------------------------------------------------------------------
# 5. CORE FIELD LOOKUP ENGINE
# -----------------------------------------------------------------------------
async def lookup_field_value(
    label: str,
    field_type: str = "text",
    saved_answers: List[Dict[str, Any]] = None,
    profile: Dict[str, Any] = None,
    company_name: str = "",
    options: List[str] = None,
    similarity_threshold: float = 0.85
) -> Tuple[Optional[str], str, Dict[str, Any]]:
    """
    Lookup order for each field:
      1. Exact normalized match in memory (saved_answers)
      2. Similarity match (embeddings, with high threshold >= 0.85)
         - Completely bypassed for yes/no, salary, notice-period, and legal questions
      3. The resume profile (verified facts from profile)
      4. Ask you (human in the loop, needs_input = True)

    Strictly rejects answers that do not match one of the available options when options are provided.
    """
    if not label or not label.strip():
        return None, LookupStage.NEEDS_INPUT, {"reason": "Empty label"}

    saved_answers = saved_answers or []
    profile = profile or {}
    norm_query = normalize_question_key(label)

    has_options = bool(options and len(options) > 0)

    # -------------------------------------------------------------
    # STEP 1: Exact Normalized Match in Memory
    # -------------------------------------------------------------
    if saved_answers:
        # A. Company-specific exact match (highest priority)
        if company_name:
            c_low = company_name.lower().strip()
            for item in saved_answers:
                item_scope = item.get("scope", "global")
                item_comp = (item.get("company_name") or "").lower().strip()
                if item_scope in ["company", "this company"] and item_comp == c_low:
                    item_norm = item.get("normalized_text") or normalize_question_key(item.get("question_pattern", ""))
                    item_key = (item.get("question_key") or "").lower().strip()
                    if item_norm == norm_query or (item_key and item_key == norm_query):
                        cand_ans = item["answer"]
                        if has_options:
                            matched_opt = find_best_option_match(cand_ans, options)
                            if matched_opt is None:
                                # Reject: answer does not match any valid option in dropdown
                                continue
                            cand_ans = matched_opt

                        touch_saved_answer(item.get("id"))
                        return cand_ans, LookupStage.EXACT_MEMORY, {
                            "scope": "this company",
                            "company_name": company_name,
                            "matched_item": item
                        }

        # B. Global exact match
        for item in saved_answers:
            item_scope = item.get("scope", "global")
            if item_scope == "global" or not item_scope:
                item_norm = item.get("normalized_text") or normalize_question_key(item.get("question_pattern", ""))
                item_key = (item.get("question_key") or "").lower().strip()
                if item_norm == norm_query or (item_key and item_key == norm_query):
                    cand_ans = item["answer"]
                    if has_options:
                        matched_opt = find_best_option_match(cand_ans, options)
                        if matched_opt is None:
                            # Reject: answer does not match any valid option in dropdown
                            continue
                        cand_ans = matched_opt

                    touch_saved_answer(item.get("id"))
                    return cand_ans, LookupStage.EXACT_MEMORY, {
                        "scope": "global",
                        "matched_item": item
                    }

    # -------------------------------------------------------------
    # STEP 2: Similarity Match (Embeddings, with high threshold >= 0.85)
    # Bypassed completely for yes/no, salary, notice-period, and legal questions
    # -------------------------------------------------------------
    exact_only = is_exact_match_only(label, field_type=field_type, options=options)
    emb_model = get_embedding_model()

    if not exact_only and emb_model is not None and saved_answers:
        try:
            q_emb = get_or_create_pattern_embedding(label, emb_model)
            if q_emb is not None:
                best_item = None
                best_score = -1.0

                # Filter candidates: company-specific (if matching) or global
                candidates = []
                for item in saved_answers:
                    scope = item.get("scope", "global")
                    if scope in ["company", "this company"]:
                        if company_name and (item.get("company_name") or "").lower().strip() == company_name.lower().strip():
                            candidates.append(item)
                    else:
                        candidates.append(item)

                for item in candidates:
                    pattern = item.get("question_pattern") or item.get("normalized_text", "")
                    if not pattern:
                        continue

                    # Also skip candidate if the candidate pattern itself is exact-match-only
                    if is_exact_match_only(pattern, field_type=item.get("field_type", "text")):
                        continue

                    item_emb = get_or_create_pattern_embedding(pattern, emb_model)
                    if item_emb is None:
                        continue

                    score = compute_cosine_similarity(q_emb, item_emb)
                    if score > best_score:
                        best_score = score
                        best_item = item

                if best_item and best_score >= similarity_threshold:
                    cand_ans = best_item["answer"]
                    if has_options:
                        matched_opt = find_best_option_match(cand_ans, options)
                        if matched_opt is not None:
                            touch_saved_answer(best_item.get("id"))
                            return matched_opt, LookupStage.SIMILARITY_EMBEDDINGS, {
                                "similarity_score": round(best_score, 4),
                                "threshold": similarity_threshold,
                                "matched_pattern": best_item.get("question_pattern"),
                                "matched_item": best_item
                            }
                    else:
                        touch_saved_answer(best_item.get("id"))
                        return cand_ans, LookupStage.SIMILARITY_EMBEDDINGS, {
                            "similarity_score": round(best_score, 4),
                            "threshold": similarity_threshold,
                            "matched_pattern": best_item.get("question_pattern"),
                            "matched_item": best_item
                        }
        except Exception as e:
            print(f"[FieldLookup] Embeddings match error: {e}")

    # -------------------------------------------------------------
    # STEP 3: The Resume Profile
    # -------------------------------------------------------------
    if profile:
        profile_match = get_field_match_by_rules(label, profile)
        if profile_match is not None and str(profile_match).strip():
            matched_val = str(profile_match)
            if has_options:
                matched_opt = find_best_option_match(matched_val, options)
                if matched_opt is not None:
                    return matched_opt, LookupStage.RESUME_PROFILE, {
                        "source": "resume_profile"
                    }
            else:
                return matched_val, LookupStage.RESUME_PROFILE, {
                    "source": "resume_profile"
                }

    # -------------------------------------------------------------
    # STEP 4: Ask You (Human in the loop)
    # -------------------------------------------------------------
    return None, LookupStage.NEEDS_INPUT, {
        "reason": "Not found in exact memory, eligible embedding similarity (threshold not met or exact-match-only), or resume profile"
    }
