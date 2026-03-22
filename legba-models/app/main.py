"""Legba Models — lightweight GPU inference service.

Serves translation, classification, relation extraction, and summarization
on a single T4 GPU via FastAPI.
"""

import time
import logging
from contextlib import asynccontextmanager
from typing import Optional

import torch
from fastapi import FastAPI
from pydantic import BaseModel, Field
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    pipeline,
)

logger = logging.getLogger("legba")

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

    # 1. NLLB-200 translation
    logger.info("Loading NLLB-200-distilled-600M ...")
    nllb_id = "facebook/nllb-200-distilled-600M"
    MODELS["nllb_tokenizer"] = AutoTokenizer.from_pretrained(nllb_id)
    MODELS["nllb_model"] = AutoModelForSeq2SeqLM.from_pretrained(nllb_id).half().to(device)

    # 2. Zero-shot classifier (DeBERTa — no fine-tuning needed)
    logger.info("Loading DeBERTa zero-shot classifier ...")
    MODELS["classifier"] = pipeline(
        "zero-shot-classification",
        model="MoritzLaurer/deberta-v3-base-zeroshot-v2.0",
        device=device,
    )

    # 3. REBEL relation extraction
    logger.info("Loading REBEL-large ...")
    rebel_id = "Babelscape/rebel-large"
    MODELS["rebel_tokenizer"] = AutoTokenizer.from_pretrained(rebel_id)
    MODELS["rebel_model"] = AutoModelForSeq2SeqLM.from_pretrained(rebel_id).half().to(device)

    # 4. T5-small summarization
    logger.info("Loading T5-small ...")
    t5_id = "google-t5/t5-small"
    MODELS["t5_tokenizer"] = AutoTokenizer.from_pretrained(t5_id)
    MODELS["t5_model"] = AutoModelForSeq2SeqLM.from_pretrained(t5_id).half().to(device)

    # 5. spaCy transformer NER (GPU-accelerated)
    logger.info("Loading spaCy en_core_web_trf ...")
    try:
        import spacy
        spacy.require_gpu()
        MODELS["spacy_nlp"] = spacy.load("en_core_web_trf")
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


class ExtractRequest(BaseModel):
    text: str


class Triple(BaseModel):
    subject: str
    predicate: str
    object: str


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


@app.post("/translate", response_model=TranslateResponse)
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


@app.post("/classify", response_model=ClassifyResponse)
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


def _parse_rebel_triplets(generated_text: str) -> list[Triple]:
    """Parse REBEL's generated text into triples.

    REBEL actual output format: <triplet> SUBJECT <subj> OBJECT <obj> RELATION
    The tokens between markers are: head=subject, after <subj>=object, after <obj>=relation.
    """
    triples = []
    parts = generated_text.strip().replace("<s>", "").replace("<pad>", "").replace("</s>", "")
    for segment in parts.split("<triplet>"):
        segment = segment.strip()
        if not segment:
            continue
        if "<subj>" in segment and "<obj>" in segment:
            head_rest = segment.split("<subj>")
            subject = head_rest[0].strip()
            obj_rest = head_rest[1].split("<obj>")
            obj = obj_rest[0].strip()
            relation = obj_rest[1].strip()
            if subject and obj and relation:
                triples.append(Triple(subject=subject, predicate=relation, object=obj))
    return triples


@app.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest):
    t0 = time.perf_counter()

    tokenizer = MODELS["rebel_tokenizer"]
    model = MODELS["rebel_model"]
    device = MODELS["device"]

    inputs = tokenizer(req.text, return_tensors="pt", truncation=True, max_length=512).to(device)

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_length=256,
            num_beams=3,
        )

    generated = tokenizer.batch_decode(output_ids, skip_special_tokens=False)[0]
    triples = _parse_rebel_triplets(generated)

    ms = (time.perf_counter() - t0) * 1000

    return ExtractResponse(triples=triples, ms=round(ms, 1))


@app.post("/summarize", response_model=SummarizeResponse)
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


@app.post("/ner", response_model=NerResponse)
def ner(req: NerRequest):
    """Extract named entities using spaCy transformer model (GPU)."""
    t0 = time.perf_counter()

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
