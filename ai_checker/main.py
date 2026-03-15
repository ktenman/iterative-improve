import re
import sys
import json
import time
import torch
import numpy as np
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from transformers import GPT2LMHeadModel, GPT2Tokenizer

RULES = [
    {"id": "COMMA_LIST_4PLUS", "pattern": r'(?:,\s*\w[\w\s]*){3,}\s+and\s+', "weight": 3},
    {"id": "PURPOSE_TAIL", "pattern": r'\bfor\s+\w+(?:\s+\w+)?\s+and\s+\w+\.\s*$', "weight": 2},
    {"id": "THEY_ALSO", "pattern": r'\bThey\s+also\b', "weight": 3},
    {"id": "FIRST_SECOND", "pattern": r'\bFirst[,.].*\bSecond[,.]', "weight": 3},
    {"id": "BOTH_DEPLOYED", "pattern": r'\bboth\s+\w+ed\b', "weight": 2},
    {"id": "WILL_PRIMARILY", "pattern": r'\bwill\s+primarily\b', "weight": 2},
    {"id": "FOCUSES_ON", "pattern": r'\bfocuses\s+on\b.*,.*,', "weight": 3},
    {"id": "WHICH_CLAUSE", "pattern": r',\s*which\s+\w+s\s+\w+', "weight": 2},
    {"id": "VERIFY_COMPLIANCE", "pattern": r'\b(?:verify|ensure)\s+(?:compliance|conformance)\b', "weight": 3},
    {"id": "PARALLEL_SVO", "pattern": r'[A-Z]\w+\s+\w+s\s+\w[\w\s]{3,20}\.\s+[A-Z]\w+\s+\w+s\s+\w[\w\s]{3,20}\.', "weight": 2},
    {"id": "ESTABLISHED_BY", "pattern": r'\b(?:established|defined)\s+by\s+the\b', "weight": 2},
    {"id": "MUST_SATISFY", "pattern": r'\bmust\s+(?:satisfy|meet|adhere|comply)\b', "weight": 2},
    {"id": "TRIPLE_VERB", "pattern": r'\b\w+(?:es|s)\s+\w[\w\s]+,\s+\w+(?:es|s)\s+\w[\w\s]+\s+and\s+\w+(?:es|s)\s+', "weight": 2},
    {"id": "ARE_ASSESSED", "pattern": r'\bare\s+\w+ed\s+as\s+well\b', "weight": 2},
    {"id": "FOR_X_CRITERIA", "pattern": r'\bFor\s+\w+,?\s+the\s+(?:criteria|guidelines|requirements)\b', "weight": 2},
    {"id": "TWO_KEY", "pattern": r'\b(?:two|three|four)\s+(?:key|main|primary|core)\b', "weight": 2},
    {"id": "DURING_WP_TESTS", "pattern": r'\bDuring\s+WP\d+,\s+\w+\s+tests?\s+(?:covered|validated|checked)\b', "weight": 2},
    {"id": "TECH_POWERS", "pattern": r'\b\w+\s+(?:powers?|handles?)\s+(?:the\s+)?\w+\s+\w+', "weight": 2},
    {"id": "PAREN_ABBREV_LIST", "pattern": r'\([A-Z]{2,}\)[,\s]+\w+[^.]{5,}\([A-Z]{2,}\)', "weight": 2},
]

QB_WORD_LIMIT = 1100


def split_sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) >= 10]


def pattern_score(sentence):
    hits = []
    for rule in RULES:
        if not rule["pattern"]:
            words = sentence.split()
            if len(words) <= 10 and sentence.endswith('.') and not any(c in sentence for c in ['(', '/', '<', '>']):
                hits.append(rule["id"])
        elif re.search(rule["pattern"], sentence):
            hits.append(rule["id"])
    weight = sum(r["weight"] for r in RULES if r["id"] in hits)
    return weight, hits


class RobertaEngine:
    name = "RoBERTa"
    short = "Rob"

    def __init__(self, device):
        print("  Loading RoBERTa (roberta-base-openai-detector)...")
        self.tokenizer = AutoTokenizer.from_pretrained("roberta-base-openai-detector")
        self.model = AutoModelForSequenceClassification.from_pretrained("roberta-base-openai-detector")
        self.model = self.model.to(device).eval()
        self.device = device

    def score(self, text):
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            logits = self.model(**inputs).logits
        return torch.softmax(logits, dim=-1)[0][0].item()


class DesklibEngine:
    name = "Desklib"
    short = "DL"

    def __init__(self, device):
        print("  Loading Desklib (DeBERTa-v3-large)...")
        import torch.nn as nn
        from safetensors.torch import load_file
        from huggingface_hub import hf_hub_download
        from transformers import AutoConfig, DebertaV2Model
        model_id = "desklib/ai-text-detector-v1.01"
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        config = AutoConfig.from_pretrained(model_id)
        raw_state = load_file(hf_hub_download(model_id, "model.safetensors"))
        base_state, cls_weights = {}, {}
        for k, v in raw_state.items():
            if k.startswith("classifier."):
                cls_weights[k] = v
            elif k.startswith("model."):
                base_state[k[6:]] = v
            else:
                base_state[k] = v
        self.base = DebertaV2Model(config)
        self.base.load_state_dict(base_state, strict=False)
        self.head = nn.Linear(config.hidden_size, 1)
        self.head.weight.data = cls_weights["classifier.weight"]
        self.head.bias.data = cls_weights["classifier.bias"]
        self.base = self.base.to(device).eval()
        self.head = self.head.to(device).eval()
        self.device = device

    def score(self, text):
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=768, padding=True)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            hidden = self.base(**inputs).last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1)
            return torch.sigmoid(self.head(pooled)).item()


class GPT2Engine:
    name = "GPT-2"
    short = "G2"

    def __init__(self):
        print("  Loading GPT-2 (perplexity detector)...")
        self.tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
        self.model = GPT2LMHeadModel.from_pretrained("gpt2").eval()

    def score(self, text):
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=1024)
        ids = inputs["input_ids"]
        if ids.shape[1] < 2:
            return 0.0
        with torch.no_grad():
            logits = self.model(ids).logits
        probs = torch.softmax(logits, dim=-1)
        token_probs = [probs[0, i - 1, ids[0, i].item()].item() for i in range(1, ids.shape[1])]
        return float(np.mean(token_probs)) if token_probs else 0.0


def quillbot_session():
    from playwright.sync_api import sync_playwright
    import requests
    print("  Getting QuillBot session...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        page = ctx.new_page()
        page.goto("https://quillbot.com/ai-content-detector", wait_until="networkidle")
        page.wait_for_timeout(2000)
        try:
            page.click("button:has-text('Accept')", timeout=3000)
        except Exception:
            pass
        cookies = {c["name"]: c["value"] for c in ctx.cookies() if "quillbot" in c.get("domain", "")}
        browser.close()
    print(f"  Got {len(cookies)} cookies")
    return cookies


def quillbot_scan(text, cookies):
    import requests
    sentences = split_sentences(text)
    total_words = len(text.split())
    chunks = [text] if total_words <= QB_WORD_LIMIT else _chunk_by_sentences(sentences, QB_WORD_LIMIT)
    if len(chunks) > 1:
        print(f"  QuillBot: {total_words} words -> {len(chunks)} chunks")
    all_results = {}
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://quillbot.com",
        "Referer": "https://quillbot.com/ai-content-detector",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    for i, chunk in enumerate(chunks):
        resp = requests.post(
            "https://quillbot.com/api/ai-detector/score",
            json={"text": chunk}, headers=headers, cookies=cookies, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and "data" in data:
            for c in data["data"].get("value", {}).get("chunks", []):
                ct = c.get("text", "").strip()
                for sent in split_sentences(ct):
                    all_results[sent[:80]] = {
                        "type": c.get("type", "HUMAN"),
                        "aiScore": c.get("aiScore", 0),
                    }
        if i < len(chunks) - 1:
            time.sleep(2)
    return all_results


def _chunk_by_sentences(sentences, limit):
    chunks, current, words = [], [], 0
    for sent in sentences:
        wc = len(sent.split())
        if words + wc > limit and current:
            chunks.append(" ".join(current))
            current, words = [sent], wc
        else:
            current.append(sent)
            words += wc
    if current:
        chunks.append(" ".join(current))
    return chunks


def combined_risk(scores, pat_score):
    points = 0.0
    rob = scores.get("Rob", 0)
    if rob > 0.70:
        points += 2
    elif rob > 0.50:
        points += 1
    dl = scores.get("DL", 0)
    if dl > 0.75:
        points += 2
    elif dl > 0.55:
        points += 1
    g2 = scores.get("G2", 0)
    if g2 > 0.15:
        points += 1
    elif g2 > 0.10:
        points += 0.5
    if pat_score >= 3:
        points += 2
    elif pat_score >= 2:
        points += 1
    qb_type = scores.get("qb_type", "?")
    if qb_type in ("AI", "AI-PARAPHRASED"):
        points += 3
    elif scores.get("qb_ai", 0) > 0.5:
        points += 1
    if points >= 4:
        return "HIGH"
    if points >= 2:
        return "MED"
    return "LOW"


def analyze(text, engines, qb_results=None):
    sentences = split_sentences(text)
    results = []
    total = len(sentences)
    for i, sent in enumerate(sentences):
        scores = {e.short: e.score(sent) for e in engines}
        pat, pat_hits = pattern_score(sent)
        if qb_results:
            key = sent[:80]
            match = qb_results.get(key)
            if not match:
                for qk, qv in qb_results.items():
                    if sent[:40] in qk or qk[:40] in sent:
                        match = qv
                        break
            if match:
                scores["qb_type"] = match.get("type", "?")
                scores["qb_ai"] = match.get("aiScore", 0)
        results.append({
            "sentence": sent,
            "scores": scores,
            "pat_score": pat,
            "pat_hits": pat_hits,
            "risk": combined_risk(scores, pat),
        })
        if total > 50 and (i + 1) % 30 == 0:
            print(f"  {i + 1}/{total} sentences...")
    return results


def print_results(results, engines, show_all=False):
    has_qb = any("qb_type" in r["scores"] for r in results)
    hdr = " ".join(f"{e.short:<5}" for e in engines)
    qb_hdr = "QB             " if has_qb else ""
    print(f"\n{'=' * 110}")
    print(f"{'RISK':<6} {hdr} {'Pat':<4} {qb_hdr}SENTENCE")
    print(f"{'=' * 110}")
    for r in results:
        if not show_all and r["risk"] == "LOW":
            continue
        marker = {" HIGH": " <<<< FLAG", "MED": " << warn"}.get(r["risk"], "")
        if r["risk"] == "HIGH":
            marker = " <<<< FLAG"
        elif r["risk"] == "MED":
            marker = " << warn"
        else:
            marker = ""
        sc = " ".join(f"{r['scores'].get(e.short, 0):.0%}  " for e in engines)
        qb = ""
        if has_qb:
            qt = r["scores"].get("qb_type", "?")[:12]
            qa = r["scores"].get("qb_ai", 0)
            qb = f"{qt:<12} {qa:.0%} "
        mx = 60 if has_qb else 70
        preview = r["sentence"][:mx] + ("..." if len(r["sentence"]) > mx else "")
        print(f"{r['risk']:<6} {sc}{r['pat_score']:<4} {qb}{preview}{marker}")
        if r["pat_hits"]:
            print(f"       patterns: {', '.join(r['pat_hits'])}")
    high = sum(1 for r in results if r["risk"] == "HIGH")
    med = sum(1 for r in results if r["risk"] == "MED")
    total = len(results)
    avgs = {e.short: np.mean([r["scores"].get(e.short, 0) for r in results]) for e in engines}
    avg_str = " | ".join(f"{e.name}: {avgs[e.short]:.0%}" for e in engines)
    print(f"\n{'=' * 110}")
    print(f"SUMMARY: {total} sentences | HIGH: {high} | MED: {med} | CLEAN: {total - high - med}")
    print(f"  Averages: {avg_str}")
    if has_qb:
        qf = sum(1 for r in results if r["scores"].get("qb_type") in ("AI", "AI-PARAPHRASED"))
        print(f"  QuillBot AI-flagged: {qf}")
    print(f"{'=' * 110}\n")


def main():
    show_all = "--all" in sys.argv
    json_out = "--json" in sys.argv
    use_full = "--full" in sys.argv
    use_desklib = "--desklib" in sys.argv or use_full
    use_gpt2 = "--gpt2" in sys.argv or use_full
    use_quillbot = "--quillbot" in sys.argv or use_full
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: uv run ai-check <textfile> [options]")
        print()
        print("Engines (default: RoBERTa + patterns):")
        print("  --desklib    Add Desklib DeBERTa-v3-large")
        print("  --gpt2       Add GPT-2 perplexity")
        print("  --full       All local engines (RoBERTa + Desklib + GPT-2)")
        print("  --quillbot   Add QuillBot API (needs playwright)")
        print()
        print("Output:")
        print("  --all        Show all sentences including CLEAN")
        print("  --json       JSON output")
        sys.exit(1)
    with open(args[0]) as f:
        text = f.read()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Device: {device}\nLoading engines...")
    engines = [RobertaEngine(device)]
    if use_desklib:
        engines.append(DesklibEngine(device))
    if use_gpt2:
        engines.append(GPT2Engine())
    print(f"  Active: {', '.join(e.name for e in engines)} + patterns")
    qb_results = None
    if use_quillbot:
        try:
            cookies = quillbot_session()
            qb_results = quillbot_scan(text, cookies)
            print(f"  QuillBot returned {len(qb_results)} sentence scores")
        except Exception as e:
            print(f"  QuillBot failed: {e}")
    results = analyze(text, engines, qb_results)
    if json_out:
        out = []
        for r in results:
            entry = {"sentence": r["sentence"], "risk": r["risk"], "pattern_score": r["pat_score"], "pattern_hits": r["pat_hits"]}
            for e in engines:
                entry[f"{e.name.lower()}_pct"] = round(r["scores"].get(e.short, 0) * 100, 1)
            if "qb_type" in r["scores"]:
                entry["quillbot_type"] = r["scores"]["qb_type"]
                entry["quillbot_ai_pct"] = round(r["scores"].get("qb_ai", 0) * 100, 1)
            out.append(entry)
        print(json.dumps(out, indent=2))
    else:
        print_results(results, engines, show_all=show_all)
    with open("/tmp/ai_check_results.json", "w") as f:
        json.dump([{
            "sentence": r["sentence"], "risk": r["risk"],
            **{e.short: round(r["scores"].get(e.short, 0) * 100, 1) for e in engines},
            "pattern": r["pat_score"],
        } for r in results], f, indent=2)


if __name__ == "__main__":
    main()
