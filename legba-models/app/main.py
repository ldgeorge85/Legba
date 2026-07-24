"""Legba Models — lightweight GPU inference service.

Serves translation, classification, relation extraction, and summarization
on a single T4 GPU via FastAPI.
"""

import hmac
import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional

import torch
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    pipeline,
)

logger = logging.getLogger("legba")

# ---------------------------------------------------------------------------
# Auth (defense-in-depth)
# ---------------------------------------------------------------------------
# These endpoints are deployment-mitigated by a 127.0.0.1 bind (the service is
# never published to a public interface — see RUNBOOK). As a second layer, an
# OPTIONAL shared secret can be required: when LEGBA_MODELS_API_SECRET is set,
# every inference endpoint requires a matching X-Models-Secret header (constant-
# time compare). When the env var is UNSET the check is a no-op so the local
# dev path is unbroken. /health stays open for liveness probes.
_MODELS_SECRET_ENV = "LEGBA_MODELS_API_SECRET"
_MODELS_SECRET_HEADER = "X-Models-Secret"


def require_models_secret(
    x_models_secret: Optional[str] = Header(default=None, alias=_MODELS_SECRET_HEADER),
) -> None:
    """Enforce the shared-secret header when one is configured.

    No-op when ``LEGBA_MODELS_API_SECRET`` is unset/empty (dev default).
    """
    configured = (os.getenv(_MODELS_SECRET_ENV) or "").strip()
    if not configured:
        return
    presented = (x_models_secret or "").strip()
    if not presented or not hmac.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing or invalid {_MODELS_SECRET_HEADER} header",
        )


# ---------------------------------------------------------------------------
# Model registry — populated at startup
# ---------------------------------------------------------------------------

MODELS = {}

# NLLB language code mapping
NLLB_LANG_CODES = {
    "ar": "ara_Arab",
    "fa": "pes_Arab",
    "he": "heb_Hebr",
    "ru": "rus_Cyrl",
    "en": "eng_Latn",
    "zh": "zho_Hans",
    "fr": "fra_Latn",
    "es": "spa_Latn",
    "de": "deu_Latn",
    "uk": "ukr_Cyrl",
    "tr": "tur_Latn",
    "pt": "por_Latn",
    "it": "ita_Latn",
    "nl": "nld_Latn",
    "pl": "pol_Latn",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "hi": "hin_Deva",
    "vi": "vie_Latn",
    "id": "ind_Latn",
    "th": "tha_Thai",
    "ur": "urd_Arab",
}

# Classification categories
CATEGORIES = [
    "armed conflict or military action",
    "government legislation or diplomatic action",
    "economic development or financial markets",
    "public health or disease outbreak",
    "environmental or climate event",
    "technology or cybersecurity",
    "natural disaster or humanitarian crisis",
    "social unrest or protest",
]

CATEGORY_MAP = {
    "armed conflict or military action": "conflict",
    "government legislation or diplomatic action": "political",
    "economic development or financial markets": "economic",
    "public health or disease outbreak": "health",
    "environmental or climate event": "environment",
    "technology or cybersecurity": "technology",
    "natural disaster or humanitarian crisis": "disaster",
    "social unrest or protest": "social",
}


def load_models():
    """Load all models to GPU. Called once at startup."""
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"Loading models on device: {device}")

    # 1. NLLB-200 translation — upgraded 600M-distilled -> 1.3B-distilled
    #    (2026-07-09) for higher proper-noun / place-name fidelity on the
    #    non-Latin war-beat (ru/ar/uk/he/fa/…). Better translation directly
    #    lifts downstream spaCy-NER entity yield (that pipeline is translate->
    #    English->NER). ~+1.4GB VRAM in fp16; T4 GPU0 has ~6.5GB headroom.
    logger.info("Loading NLLB-200-distilled-1.3B ...")
    nllb_id = "facebook/nllb-200-distilled-1.3B"
    MODELS["nllb_tokenizer"] = AutoTokenizer.from_pretrained(nllb_id)
    MODELS["nllb_model"] = AutoModelForSeq2SeqLM.from_pretrained(nllb_id).half().to(device)

    # 2. Zero-shot classifier (DeBERTa — no fine-tuning needed)
    logger.info("Loading DeBERTa zero-shot classifier ...")
    MODELS["classifier"] = pipeline(
        "zero-shot-classification",
        model="MoritzLaurer/deberta-v3-base-zeroshot-v2.0",
        device=device,
    )

    # 3. GLiREL zero-shot relation extraction (replaces REBEL)
    logger.info("Loading GLiREL-large ...")
    from glirel import GLiREL
    glirel_model = GLiREL.from_pretrained("jackboyla/glirel-large-v0")
    if device == "cuda":
        glirel_model = glirel_model.to(device)
    MODELS["glirel"] = glirel_model

    # 4. T5-small summarization
    logger.info("Loading T5-small ...")
    t5_id = "google-t5/t5-small"
    MODELS["t5_tokenizer"] = AutoTokenizer.from_pretrained(t5_id)
    MODELS["t5_model"] = AutoModelForSeq2SeqLM.from_pretrained(t5_id).half().to(device)

    # 5. spaCy transformer NER (GPU-accelerated)
    logger.info("Loading spaCy en_core_web_trf ...")
    try:
        import spacy
        spacy.require_gpu(0)
        nlp = spacy.load("en_core_web_trf")
        _ = nlp("Warm-up test.")
        MODELS["spacy_nlp"] = nlp
        logger.info("spaCy trf loaded on GPU")
    except Exception as e:
        logger.warning(f"spaCy trf failed to load: {e}. NER endpoint will be unavailable.")
        MODELS["spacy_nlp"] = None

    MODELS["device"] = device
    logger.info("All models loaded.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_models()
    yield
    # Cleanup
    MODELS.clear()
    torch.cuda.empty_cache()


app = FastAPI(title="Legba Models", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class TranslateRequest(BaseModel):
    text: str
    source_lang: str = Field(description="ISO 639-1 code: ar, fa, he, ru, etc.")
    target_lang: str = "en"


class TranslateResponse(BaseModel):
    translated: str
    source_lang: str
    target_lang: str
    ms: float


class ClassifyRequest(BaseModel):
    text: str
    labels: Optional[list[str]] = Field(
        default=None,
        description="Category labels to classify against. If omitted, uses default categories.",
    )


class ClassifyResponse(BaseModel):
    category: str
    confidence: float
    scores: dict[str, float]
    ms: float


# Default relation labels for zero-shot extraction (geopolitical / news domain)
DEFAULT_RELATION_LABELS = [
    "leader of",
    "member of",
    "located in",
    "headquarters in",
    "founded by",
    "part of",
    "ally of",
    "opponent of",
    "conflict with",
    "signed agreement with",
    "sanctioned by",
    "spokesperson for",
    "operates in",
    "supplies to",
    "border with",
    "capital of",
    "controls",
    "parent organization of",
    "subsidiary of",
    "employed by",
]


class ExtractRequest(BaseModel):
    text: str
    labels: Optional[list[str]] = Field(
        default=None,
        description="Relation labels to extract. If omitted, uses default geopolitical/news labels.",
    )
    threshold: float = Field(
        default=0.3,
        description="Minimum confidence score for extracted relations.",
    )


class Triple(BaseModel):
    subject: str
    predicate: str
    object: str
    # Character offsets in the input text (Optional for backwards compatibility:
    # older clients ignore unknown fields; new clients can use these directly
    # instead of re-locating the entity via substring search). When the spaCy
    # entity that contributed the head/tail can be identified by token span,
    # these are set to its `start_char` / `end_char`; otherwise None.
    subject_start: Optional[int] = None
    subject_end: Optional[int] = None
    object_start: Optional[int] = None
    object_end: Optional[int] = None


class ExtractResponse(BaseModel):
    triples: list[Triple]
    ms: float


class NerRequest(BaseModel):
    text: str


class NerEntity(BaseModel):
    text: str
    label: str
    start: int
    end: int


class NerResponse(BaseModel):
    entities: list[NerEntity]
    actors: list[str]
    locations: list[str]
    ms: float


class SummarizeRequest(BaseModel):
    texts: list[str]
    max_length: int = 64


class SummarizeResponse(BaseModel):
    summary: str
    ms: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    gpu_available = torch.cuda.is_available()
    models_loaded = len(MODELS) > 0
    gpu_mem = {}
    if gpu_available:
        gpu_mem = {
            "allocated_mb": round(torch.cuda.memory_allocated() / 1024 / 1024, 1),
            "reserved_mb": round(torch.cuda.memory_reserved() / 1024 / 1024, 1),
        }
    return {
        "status": "ok" if models_loaded else "loading",
        "gpu": gpu_available,
        "gpu_memory": gpu_mem,
        "models_loaded": models_loaded,
    }


@app.post(
    "/translate",
    response_model=TranslateResponse,
    dependencies=[Depends(require_models_secret)],
)
def translate(req: TranslateRequest):
    t0 = time.perf_counter()

    src_code = NLLB_LANG_CODES.get(req.source_lang)
    tgt_code = NLLB_LANG_CODES.get(req.target_lang, "eng_Latn")
    if not src_code:
        supported = list(NLLB_LANG_CODES.keys())
        raise ValueError(f"Unsupported source_lang '{req.source_lang}'. Supported: {supported}")

    tokenizer = MODELS["nllb_tokenizer"]
    model = MODELS["nllb_model"]
    device = MODELS["device"]

    tokenizer.src_lang = src_code
    inputs = tokenizer(req.text, return_tensors="pt", truncation=True, max_length=512).to(device)

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            forced_bos_token_id=tokenizer.convert_tokens_to_ids(tgt_code),
            max_new_tokens=512,
        )

    translated = tokenizer.decode(output_ids[0], skip_special_tokens=True)
    ms = (time.perf_counter() - t0) * 1000

    return TranslateResponse(
        translated=translated,
        source_lang=req.source_lang,
        target_lang=req.target_lang,
        ms=round(ms, 1),
    )


@app.post(
    "/classify",
    response_model=ClassifyResponse,
    dependencies=[Depends(require_models_secret)],
)
def classify(req: ClassifyRequest):
    t0 = time.perf_counter()

    if req.labels:
        # Client-supplied labels — return as-is (no mapping)
        result = MODELS["classifier"](
            req.text,
            candidate_labels=req.labels,
            multi_label=False,
        )
        top_label = result["labels"][0]
        scores = {
            label: round(score, 4)
            for label, score in zip(result["labels"], result["scores"])
        }
    else:
        # Default categories with verbose→short mapping
        result = MODELS["classifier"](
            req.text,
            candidate_labels=CATEGORIES,
            multi_label=False,
        )
        top_label = CATEGORY_MAP.get(result["labels"][0], "other")
        scores = {
            CATEGORY_MAP.get(label, label): round(score, 4)
            for label, score in zip(result["labels"], result["scores"])
        }

    confidence = result["scores"][0]
    ms = (time.perf_counter() - t0) * 1000

    return ClassifyResponse(
        category=top_label,
        confidence=round(confidence, 4),
        scores=scores,
        ms=round(ms, 1),
    )


@app.post(
    "/extract",
    response_model=ExtractResponse,
    dependencies=[Depends(require_models_secret)],
)
async def extract(req: ExtractRequest):
    """Extract relation triples using GLiREL zero-shot relation extraction.

    Uses spaCy NER to identify entities, then GLiREL to classify relations
    between entity pairs. Must be async to run on the main event loop thread
    where spaCy/CuPy's CUDA context was initialized.
    """
    t0 = time.perf_counter()

    # Re-activate CuPy/thinc GPU context for this call
    try:
        import spacy
        spacy.require_gpu(0)
    except Exception:
        pass

    nlp = MODELS.get("spacy_nlp")
    glirel_model = MODELS.get("glirel")
    if nlp is None or glirel_model is None:
        return ExtractResponse(triples=[], ms=0.0)

    # Step 1: spaCy NER to get entities and tokens
    input_text = req.text[:2000]
    doc = nlp(input_text)
    tokens = [token.text for token in doc]

    # Build NER list in GLiREL format: [start_tok, end_tok (inclusive), TYPE, text]
    # and an index from (start_tok, end_tok_inclusive) -> spaCy Span so we can
    # recover character offsets + verbatim source text from GLiREL output.
    ner = []
    span_by_toks: dict[tuple[int, int], "spacy.tokens.Span"] = {}
    for ent in doc.ents:
        start_tok = ent.start
        end_tok = ent.end - 1  # GLiREL uses inclusive end index
        ner.append([start_tok, end_tok, ent.label_, ent.text])
        span_by_toks[(start_tok, end_tok)] = ent

    if len(ner) < 2:
        ms = (time.perf_counter() - t0) * 1000
        return ExtractResponse(triples=[], ms=round(ms, 1))

    # Step 2: GLiREL relation extraction
    labels = req.labels if req.labels else DEFAULT_RELATION_LABELS

    with torch.no_grad():
        relations = glirel_model.predict_relations(
            tokens,
            labels,
            threshold=req.threshold,
            ner=ner,
            top_k=1,
        )

    # Step 3: Convert to Triple format.
    #
    # GLiREL returns each relation with head/tail represented as a list of
    # token strings (head_text/tail_text) and the corresponding token-index
    # span (head_pos/tail_pos = [start_tok, end_tok_inclusive]). We resolve
    # those back to the originating spaCy entity so we can return the verbatim
    # source-text substring (preserves punctuation/whitespace fidelity, unlike
    # `" ".join(tokens)`) plus exact character offsets.
    #
    # Fallback path for any future GLiREL versions that omit *_pos: match the
    # joined token-text against the ner list we just built. As a last resort
    # we fall back to the historical `" ".join(...)` form so callers still get
    # a non-empty subject/object.
    def _resolve_entity(rel: dict, slot: str):
        pos_key, text_key = f"{slot}_pos", f"{slot}_text"
        pos = rel.get(pos_key)
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            try:
                key = (int(pos[0]), int(pos[1]))
            except (TypeError, ValueError):
                key = None
            if key and key in span_by_toks:
                span = span_by_toks[key]
                return span.text, span.start_char, span.end_char
        # Fallback: match joined token text against the input ner list.
        raw = rel.get(text_key)
        joined = " ".join(raw) if isinstance(raw, list) else (raw or "")
        for (s_tok, e_tok), span in span_by_toks.items():
            if span.text == joined:
                return span.text, span.start_char, span.end_char
        return joined, None, None

    triples = []
    for rel in sorted(relations, key=lambda x: x["score"], reverse=True):
        subj_text, subj_start, subj_end = _resolve_entity(rel, "head")
        obj_text, obj_start, obj_end = _resolve_entity(rel, "tail")
        triples.append(Triple(
            subject=subj_text,
            predicate=rel["label"],
            object=obj_text,
            subject_start=subj_start,
            subject_end=subj_end,
            object_start=obj_start,
            object_end=obj_end,
        ))

    ms = (time.perf_counter() - t0) * 1000
    return ExtractResponse(triples=triples, ms=round(ms, 1))


@app.post(
    "/summarize",
    response_model=SummarizeResponse,
    dependencies=[Depends(require_models_secret)],
)
def summarize(req: SummarizeRequest):
    t0 = time.perf_counter()

    tokenizer = MODELS["t5_tokenizer"]
    model = MODELS["t5_model"]
    device = MODELS["device"]

    combined = " | ".join(req.texts)
    input_text = f"summarize: {combined}"

    inputs = tokenizer(input_text, return_tensors="pt", truncation=True, max_length=512).to(device)

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_length=req.max_length,
            num_beams=4,
            length_penalty=1.0,
            early_stopping=True,
        )

    summary = tokenizer.decode(output_ids[0], skip_special_tokens=True)
    ms = (time.perf_counter() - t0) * 1000

    return SummarizeResponse(summary=summary, ms=round(ms, 1))


@app.post(
    "/ner",
    response_model=NerResponse,
    dependencies=[Depends(require_models_secret)],
)
async def ner(req: NerRequest):
    """Extract named entities using spaCy transformer model (GPU).

    Must be async to run on the main event loop thread where
    spaCy/CuPy's CUDA context was initialized.
    """
    t0 = time.perf_counter()

    # Re-activate CuPy/thinc GPU context for this call
    try:
        import spacy
        spacy.require_gpu(0)
    except Exception:
        pass

    nlp = MODELS.get("spacy_nlp")
    if nlp is None:
        return NerResponse(entities=[], actors=[], locations=[], ms=0.0)

    doc = nlp(req.text[:2000])

    entities = []
    actors = set()
    locations = set()

    for ent in doc.ents:
        entities.append(NerEntity(
            text=ent.text,
            label=ent.label_,
            start=ent.start_char,
            end=ent.end_char,
        ))
        if ent.label_ in ("PERSON", "ORG", "NORP"):
            actors.add(ent.text)
        elif ent.label_ in ("GPE", "LOC", "FAC"):
            locations.add(ent.text)

    ms = (time.perf_counter() - t0) * 1000

    return NerResponse(
        entities=entities,
        actors=sorted(actors),
        locations=sorted(locations),
        ms=round(ms, 1),
    )
