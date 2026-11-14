import re
import numpy as np
from typing import Dict, Any, Optional, List, Tuple
from database import (
    normalize_question_key, generate_question_key, touch_saved_answer
)
from field_rules import get_field_match_by_rules

class LookupStage:
    EXACT_MEMORY = "exact_memory"
    SIMILARITY_EMBEDDINGS = "similarity_embeddings"
    RESUME_PROFILE = "resume_profile"
    NEEDS_INPUT = "needs_input"

# Lazy-loaded singleton for fastembed TextEmbedding
_EMBEDDING_MODEL = None

def get_embedding_model():
    """Retrieve or initialize the cached local TextEmbedding model."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        try:
            from fastembed import TextEmbedding
            _EMBEDDING_MODEL = TextEmbedding()
        except Exception as e:
            print(f"[FieldLookup] Warning: FastEmbed failed to load: {e}")
            _EMBEDDING_MODEL = None
    return _EMBEDDING_MODEL

# In-memory vector cache for saved_answers to avoid redundant embedding calls
_SAVED_ANSWERS_EMB_CACHE: Dict[str, np.ndarray] = {}

def compute_cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """Compute cosine similarity between two 1D vectors."""
    dot = np.dot(vec_a, vec_b)
    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))

def find_best_option_match(target_val: str, options: List[str]) -> Optional[str]:
    """Match a resolved value to the closest matching option in a dropdown or radio list."""
    if not options or not target_val:
        return target_val
        
    t_clean = str(target_val).strip().lower()
    
    # 1. Exact match
    for opt in options:
        if opt.strip().lower() == t_clean:
            return opt
            
    # 2. Boolean normalization (Yes/No)
    if t_clean in ["yes", "true", "y"]:
        for opt in options:
            if opt.strip().lower() in ["yes", "true", "y", "authorized", "eligible"]:
                return opt
    elif t_clean in ["no", "false", "n"]:
        for opt in options:
            if opt.strip().lower() in ["no", "false", "n", "not authorized"]:
                return opt
                
    # 3. Substring match
    for opt in options:
        opt_low = opt.strip().lower()
        if t_clean in opt_low or opt_low in t_clean:
            return opt
            
    return target_val

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
      2. Similarity match (embeddings, with a high threshold >= 0.85)
      3. The resume profile (verified facts from profile)
      4. Ask you (human in the loop, needs_input = True)

    Returns:
        (resolved_value, stage, metadata)
    """
    if not label or not label.strip():
        return None, LookupStage.NEEDS_INPUT, {"reason": "Empty label"}

    saved_answers = saved_answers or []
    profile = profile or {}
    norm_query = normalize_question_key(label)
    query_key = generate_question_key(label, field_type=field_type, company_name=company_name)

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
                        touch_saved_answer(item.get("id"))
                        matched_val = item["answer"]
                        if options:
                            matched_val = find_best_option_match(matched_val, options)
                        return matched_val, LookupStage.EXACT_MEMORY, {
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
                    touch_saved_answer(item.get("id"))
                    matched_val = item["answer"]
                    if options:
                        matched_val = find_best_option_match(matched_val, options)
                    return matched_val, LookupStage.EXACT_MEMORY, {
                        "scope": "global",
                        "matched_item": item
                    }

    # -------------------------------------------------------------
    # STEP 2: Similarity Match (Embeddings, with high threshold)
    # -------------------------------------------------------------
    emb_model = get_embedding_model()
    if emb_model and saved_answers:
        try:
            q_emb = np.array(list(emb_model.embed([label]))[0])

            best_item = None
            best_score = -1.0

            # Filter candidates: company-specific (if match) or global
            candidates = []
            for item in saved_answers:
                scope = item.get("scope", "global")
                if scope in ["company", "this company"]:
                    if company_name and (item.get("company_name") or "").lower().strip() == company_name.lower().strip():
                        candidates.append(item)
                else:
                    candidates.append(item)

            for item in candidates:
                item_id = item.get("id")
                pattern = item.get("question_pattern") or item.get("normalized_text", "")
                if not pattern:
                    continue

                if item_id and item_id in _SAVED_ANSWERS_EMB_CACHE:
                    item_emb = _SAVED_ANSWERS_EMB_CACHE[item_id]
                else:
                    item_emb = np.array(list(emb_model.embed([pattern]))[0])
                    if item_id:
                        _SAVED_ANSWERS_EMB_CACHE[item_id] = item_emb

                score = compute_cosine_similarity(q_emb, item_emb)
                if score > best_score:
                    best_score = score
                    best_item = item

            if best_item and best_score >= similarity_threshold:
                touch_saved_answer(best_item.get("id"))
                matched_val = best_item["answer"]
                if options:
                    matched_val = find_best_option_match(matched_val, options)
                return matched_val, LookupStage.SIMILARITY_EMBEDDINGS, {
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
            if options:
                matched_val = find_best_option_match(matched_val, options)
            return matched_val, LookupStage.RESUME_PROFILE, {
                "source": "resume_profile"
            }

    # -------------------------------------------------------------
    # STEP 4: Ask You (Human in the loop)
    # -------------------------------------------------------------
    return None, LookupStage.NEEDS_INPUT, {
        "reason": "Not found in exact memory, embedding similarity (threshold not met), or resume profile"
    }
