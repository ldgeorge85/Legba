"""Pre-download all models to HF cache on first startup."""

from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    pipeline,
)

MODELS = {
    "nllb": "facebook/nllb-200-distilled-600M",
    "classifier": "MoritzLaurer/deberta-v3-base-zeroshot-v2.0",
    "rebel": "Babelscape/rebel-large",
    "t5": "google-t5/t5-small",
}


def download_all():
    print("[legba] Downloading NLLB-200 (translation) ...")
    AutoTokenizer.from_pretrained(MODELS["nllb"])
    AutoModelForSeq2SeqLM.from_pretrained(MODELS["nllb"])

    print("[legba] Downloading DeBERTa zero-shot (classification) ...")
    AutoTokenizer.from_pretrained(MODELS["classifier"])
    AutoModelForSequenceClassification.from_pretrained(MODELS["classifier"])

    print("[legba] Downloading REBEL-large (relation extraction) ...")
    AutoTokenizer.from_pretrained(MODELS["rebel"])
    AutoModelForSeq2SeqLM.from_pretrained(MODELS["rebel"])

    print("[legba] Downloading T5-small (summarization) ...")
    AutoTokenizer.from_pretrained(MODELS["t5"])
    AutoModelForSeq2SeqLM.from_pretrained(MODELS["t5"])

    print("[legba] Downloading spaCy en_core_web_trf (NER) ...")
    try:
        import subprocess
        subprocess.check_call(["python3", "-m", "spacy", "download", "en_core_web_trf"])
    except Exception as e:
        print(f"[legba] WARNING: spaCy trf download failed: {e}")

    print("[legba] All models cached.")


if __name__ == "__main__":
    download_all()
