"""Phase C: keyword vs vector vs hybrid retrieval over the fiscal-note index, measured.

    python -m txfr.retrieval build      # tx-session-v2 = tx-session + 256-dim text-embedding-3-small vectors + semantic config
    python -m txfr.retrieval compare    # 30 queries x 4 modes → docs/eval/retrieval.json

Why 256 dimensions: text-embedding-3 models accept a `dimensions` argument (Matryoshka training keeps the first
dimensions meaningful). 11,224 docs x 256 floats ≈ 11.5 MB, inside the free tier's 50 MB with the 6.7 MB of text.
Vectors are computed here and pushed (not integrated vectorization): the free tier can't hold a managed identity
for the search service to call Foundry, and Foundry has API keys disabled. Production would use integrated
vectorization (indexer skill + query-time vectorizer) on Basic or higher.

Test set: 30 questions about enacted 2025 bills, each with the bill(s) that answer it.
    paraphrase (20)  everyday words for what the caption says formally ("school vouchers" → "education savings account")
    exact (10)       bill numbers and formal terms: keyword search's home ground
Score: hit@6 (a right bill among the 6 results the agent sees) and MRR (1 / rank of the first right result).
"""
from __future__ import annotations

import argparse
import json
import os
import time

from txfr.common import ROOT

SRC_INDEX, INDEX = "tx-session", "tx-session-v2"
DIMS = 256
SEMANTIC = "txfr-semantic"
MODES = ["keyword", "vector", "hybrid", "hybrid_semantic"]

TESTS = [  # (query, acceptable 89R bills, kind)
    ("Did Texas pass school vouchers?", ["SB2"], "paraphrase"),
    ("property tax relief for homeowners through a bigger homestead exemption", ["SB4", "SB23"], "paraphrase"),
    ("extra homestead tax break for seniors and people with disabilities", ["SB23"], "paraphrase"),
    ("tax credit for company research and development spending", ["SB2206"], "paraphrase"),
    ("tax break on equipment and inventory businesses use to make money", ["HB9"], "paraphrase"),
    ("new state agency for cybersecurity", ["HB150"], "paraphrase"),
    ("Alzheimer's and dementia research funding institute", ["SB5"], "paraphrase"),
    ("getting rid of the lottery commission", ["SB3070"], "paraphrase"),
    ("rules for how state government uses artificial intelligence", ["HB149", "SB1964", "HB2818"], "paraphrase"),
    ("money for armed security and safety at schools", ["SB260"], "paraphrase"),
    ("funding for students with disabilities in public schools", ["SB568"], "paraphrase"),
    ("online school and remote learning for kids", ["SB569"], "paraphrase"),
    ("health care access for people in rural counties", ["HB18"], "paraphrase"),
    ("pay and discipline for judges", ["SB293"], "paraphrase"),
    ("plugging abandoned oil and gas wells", ["SB1150"], "paraphrase"),
    ("preventing wildfires and helping volunteer firefighters", ["SB34"], "paraphrase"),
    ("ending car emissions testing", ["HB5033"], "paraphrase"),
    ("bail reform and keeping dangerous defendants in jail before trial", ["SB9"], "paraphrase"),
    ("recovering money from Medicaid fraud", ["HB142"], "paraphrase"),
    ("paying for water pipes and reservoirs", ["SB7"], "paraphrase"),
    ("HB 2 public school finance", ["HB2"], "exact"),
    ("SB 2206 franchise tax credit", ["SB2206"], "exact"),
    ("Texas Cyber Command", ["HB150"], "exact"),
    ("education savings account program", ["SB2"], "exact"),
    ("business court", ["HB40"], "exact"),
    ("Texas Windstorm Insurance Association premium tax", ["HB2517"], "exact"),
    ("Dementia Prevention and Research Institute of Texas", ["SB5"], "exact"),
    ("Employees Retirement System law enforcement custodial officers service retirement", ["SB1737"], "exact"),
    ("Texas Armed Services Scholarship Program", ["HB300"], "exact"),
    ("diesel fuel tax auxiliary power units refund", ["SB771"], "exact"),
]


def _search_client(index: str):
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    return SearchClient(os.environ["SEARCH_ENDPOINT"], index, DefaultAzureCredential())


def _embedder():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    tp = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    cli = AzureOpenAI(azure_endpoint=os.environ["OPENAI_ENDPOINT"], azure_ad_token_provider=tp, api_version="2024-10-21")
    model = os.environ.get("EMBED_DEPLOYMENT", "text-embedding-3-small")
    usage = {"tokens": 0}

    def embed(texts: list[str]) -> list[list[float]]:
        for attempt in range(6):
            try:
                r = cli.embeddings.create(model=model, input=[t[:8000] or " " for t in texts], dimensions=DIMS)
                usage["tokens"] += r.usage.total_tokens
                from txfr import finops
                finops.add(embedding_tokens=r.usage.total_tokens)
                return [d.embedding for d in r.data]
            except Exception as e:  # noqa: BLE001 - 429s: back off
                if "429" not in str(e) or attempt == 5:
                    raise
                time.sleep(2 ** attempt)
    embed.usage = usage
    return embed


def build():
    from azure.identity import DefaultAzureCredential
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (HnswAlgorithmConfiguration, SearchField, SearchFieldDataType,
                                                       SemanticConfiguration, SemanticField, SemanticPrioritizedFields,
                                                       SemanticSearch, VectorSearch, VectorSearchProfile)
    ic = SearchIndexClient(os.environ["SEARCH_ENDPOINT"], DefaultAzureCredential())
    src = ic.get_index(SRC_INDEX)
    fields = [f for f in src.fields if f.name != "content_vector"] + [
        SearchField(name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, vector_search_dimensions=DIMS, vector_search_profile_name="hnsw",
                    stored=False)]   # not retrievable/stored: halves the vector storage
    idx = src
    idx.name, idx.fields, idx.e_tag = INDEX, fields, None
    idx.vector_search = VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw-alg")],
                                     profiles=[VectorSearchProfile(name="hnsw", algorithm_configuration_name="hnsw-alg")])
    idx.semantic_search = SemanticSearch(configurations=[SemanticConfiguration(
        name=SEMANTIC, prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"), content_fields=[SemanticField(field_name="content")]))])
    ic.create_or_update_index(idx)

    docs, sc = [], _search_client(SRC_INDEX)
    for d in sc.search("*", top=100000):
        docs.append({k: v for k, v in d.items() if not k.startswith("@")})
    print(f"read {len(docs)} docs from {SRC_INDEX}")
    embed, dst = _embedder(), _search_client(INDEX)
    for i in range(0, len(docs), 256):
        batch = docs[i:i + 256]
        vecs = embed([f"{d.get('title') or ''}\n{d.get('content') or ''}" for d in batch])
        for d, v in zip(batch, vecs):
            d["content_vector"] = v
        for j in range(0, len(batch), 128):
            dst.upload_documents(batch[j:j + 128])
        if i % 2560 == 0:
            print(f"  {i + len(batch)}/{len(docs)} embedded, {embed.usage['tokens']:,} tokens")
    print(f"done: {len(docs)} docs, {embed.usage['tokens']:,} embedding tokens "
          f"(≈ ${embed.usage['tokens'] / 1e6 * 0.02:.3f} at $0.02 / 1M)")


def search(sc, embed, query: str, mode: str, top: int = 6, flt: str | None = None):
    from azure.search.documents.models import VectorizedQuery
    kw = {"top": top, "filter": flt, "select": ["id", "bill", "session", "doc_type", "title", "content", "source_url"]}
    if mode in ("vector", "hybrid", "hybrid_semantic"):
        kw["vector_queries"] = [VectorizedQuery(vector=embed([query])[0], k_nearest_neighbors=50 if mode != "vector" else top,
                                                fields="content_vector")]
    if mode == "hybrid_semantic":
        kw.update(query_type="semantic", semantic_configuration_name=SEMANTIC)
    return list(sc.search(None if mode == "vector" else query, **kw))


def compare():
    sc, embed = _search_client(INDEX), _embedder()
    rows, flt = [], "session eq '89R'"
    for q, bills, kind in TESTS:
        row = {"query": q, "kind": kind, "expected": bills}
        for m in MODES:
            t = time.time()
            try:
                hits = search(sc, embed, q, m, flt=flt)
                ranks = [i + 1 for i, h in enumerate(hits) if h.get("bill") in bills]
                row[m] = {"hit": bool(ranks), "rr": round(1 / ranks[0], 3) if ranks else 0.0,
                          "ms": round((time.time() - t) * 1000), "top": [h.get("bill") for h in hits]}
            except Exception as e:  # noqa: BLE001 - e.g. semantic ranker unavailable on this tier
                row[m] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        rows.append(row)
        print(f"{kind:10} {q[:60]:60} " + " ".join(
            f"{m}={'✓' if row[m].get('hit') else ('!' if 'error' in row[m] else '·')}" for m in MODES))
    summary = {}
    for m in MODES:
        for kind in ("paraphrase", "exact", "all"):
            sel = [r[m] for r in rows if (kind == "all" or r["kind"] == kind) and "error" not in r[m]]
            if sel:
                summary.setdefault(m, {})[kind] = {
                    "hit@6": round(sum(x["hit"] for x in sel) / len(sel), 2),
                    "mrr": round(sum(x["rr"] for x in sel) / len(sel), 2),
                    "median_ms": sorted(x["ms"] for x in sel)[len(sel) // 2], "n": len(sel)}
            elif kind == "all":
                summary[m] = {"error": next(r[m]["error"] for r in rows if "error" in r[m])}
    out = ROOT / "docs" / "eval" / "retrieval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "queries": rows}, indent=2))
    print(json.dumps(summary, indent=2))


def main():
    from txfr.common import load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "compare"])
    a = ap.parse_args()
    build() if a.cmd == "build" else compare()


if __name__ == "__main__":
    main()
