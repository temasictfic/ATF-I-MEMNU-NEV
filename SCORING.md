# Atfi Memnu — Skorlama ve Eşleşme Sistemi

Bu doküman, atıf doğrulama sürecinin **uçtan uca** nasıl çalıştığını anlatır:

- **Yüksek / Orta / Düşük** rozetlerinin nasıl belirlendiği
- **Geçerli / Künye / Uydurma** etiketlerinin hangi koşulda atandığı
- **Başlık, yazarlar, dergi, DOI/arXiv, yıl** alanlarının nasıl eşleştirildiği (chip etiketi yalnızca `doi`)
- Parsing aşamasındaki **`parse_confidence`**'in nasıl hesaplandığı
- 7 veritabanı + Google Scholar sonuçlarının nasıl birleştirildiği
- 5 problem chip'inin (`!authors`, `!doi`, `!year`, `!journal`, `!title`) ne zaman tetiklendiği

> Tüm sayılar ve eşikler, dokümantasyon yazıldığı sırada doğrudan koddan doğrulanmıştır. Dosya yolları kodda kalıcı olduğu için tıklayarak gidebilirsin.

---

## 1. Genel akış

```
        PDF
         │
         ▼
   source text  (Electron renderer'da pdf.js ile çıkartılır —
                    backend artık PDF'e hiç dokunmuyor)
         │
         ▼
  ┌───────────────────────────────────────┐
  │  extract_source_fields                │
  │   1) NER extractor                    │
  │      (fine-tuned + INT8-quantized      │
  │       ONNX, SIRIS-Lab tabanlı)        │
  │   2) confidence < 0.3 → regex fallback│
  └───────────────────────────────────────┘
         │
         ▼
   ParsedSource  (+ parse_confidence  ∈ [0.0, 1.0])
         │
         ▼
  ┌───────────────────────────────────────┐
  │  Tier 1 paralel: 9 API verifier       │
  │  Tier 2 (opsiyonel): Google Scholar    │
  │  her aday → score_match()             │
  └───────────────────────────────────────┘
         │
         ▼
   best_match = max(all_results, key=score)
         │
         ├──► determine_verification_status()
         │       → status (Yüksek / Orta / Düşük)
         │       → problem_tags (5 chip)
         │
         └──► classify_decision()
                 → decision_tag (Geçerli / Künye / Uydurma)
```

---

## 2. Parsing aşaması — `parse_confidence`

### NER modeli

Üretimde kullanılan model **`citation-ner-int8`** — `SIRIS-Lab/citation-parser-ENTITY` (multilingual DistilBERT) tabanından **bizim Türkçe/non-APA korpusumuzda fine-tune edilip INT8 olarak quantize edilmiş ONNX** sürümü. ONNX Runtime ile yüklenir ([backend/services/ner_model_manager.py](backend/services/ner_model_manager.py)), model dosyaları [backend/models/citation-ner-int8/](backend/models/citation-ner-int8/) altında (`model_quantized.onnx` + tokenizer).

**Neden fine-tune?** Upstream SIRIS modeli APA-dominant korpus üzerinde eğitildiği için APA'da F1 = 0.95, ama IEEE/Vancouver/Chicago/informal Türkçe atıflarda F1 = 0.555'e düşüyordu. Bizim varyant aynı non-APA test setinde **F1 = 0.864**'e çıkar (APA'da regresyon yok — F1 = 0.967, baseline ile aynı). Detaylar: [backend/training/TRAINING_GUIDE.md](backend/training/TRAINING_GUIDE.md).

Etiketler: `TITLE`, `AUTHORS`, `PUBLICATION_YEAR`, `JOURNAL`, `DOI`, `VOLUME`, `ISSUE`, `PAGES`, `LINK_ONLINE_AVAILABILITY` …

### parse_confidence skoru

Her kaynağa, alanlarına ne kadar güvenilebileceğini gösteren 0.0–1.0 arası bir skor biçilir. **Hem NER hem regex tarafında aynı sinyaller, aynı ağırlıklar** kullanılır:

| Sinyal | Ağırlık | Şart |
|---|---|---|
| DOI veya arXiv ID var | **+0.40** | regex tarafında sadece DOI |
| Anlamlı yazar listesi | **+0.20** | her yazar ≥ 2 karakter |
| Yıl makul aralıkta | **+0.15** | regex: 1900–2030 / NER: 1900–2099 |
| Başlık ≥ 10 karakter | **+0.15** | — |
| Source (dergi/konferans) ≥ 3 karakter | **+0.10** | — |

**Maksimum 1.0'a clamp edilir.**

### 0.3 eşiğinin üç anlamı

`parse_confidence < 0.3` durumunda kod üç farklı yerde davranış değiştirir:

1. **NER → regex geçişi** ([source_extractor.py:34](backend/services/source_extractor.py#L34)) — NER sonucu güveniliyorsa kabul, değilse regex extractor çalışır
2. **Composite skor düşürmesi** ([match_scorer.py:50–53](backend/services/match_scorer.py#L50-L53)) — yazar bileşeni atılır, title-only fallback'e geçilir
3. **Logging uyarısı** ([verification_orchestrator.py:301–307](backend/services/verification_orchestrator.py#L301-L307)) — düşük güvenli parse'lar logda işaretlenir

Kaynak kod: [source_extractor.py:714–743](backend/services/source_extractor.py#L714-L743), [ner_extractor.py:260–282](backend/services/ner_extractor.py#L260-L282)

---

## 3. Alan-bazlı eşleşme algoritmaları

### 3.1 Başlık (title)

| Adım | Detay |
|---|---|
| Normalizasyon | sadece `.lower()` — noktalama veya Türkçe karakter dönüşümü **yok** |
| Skor | `0.6 · fuzz.token_sort_ratio + 0.4 · fuzz.ratio` (0.0–1.0) |
| Eşik | `TITLE_MATCH_THRESHOLD = 0.85` — `!title` chip'i ve karar kuralı için |
| Composite ağırlığı | base'in **%75'i** (yazar varsa); yazar yoksa **%100** fallback |

Token-sort + ratio karışımı, kelime sırası farklılıklarını ve karakter düzeyi tipo'ları aynı anda yakalar.

Kaynak: [match_scorer.py:22–29](backend/services/match_scorer.py#L22-L29), [match_scorer.py:113](backend/services/match_scorer.py#L113)

### 3.2 Yazarlar (authors)

#### Normalizasyon (`normalize_name`)

```
"Şehit Öztürk"  →  "sehit ozturk"
"M. Yılmaz"     →  "m yilmaz"
```

Türkçe/diakritik dönüşüm tablosu (`ı→i`, `ş→s`, `ğ→g`, `ç→c`, `ö→o`, `ü→u`, `ß→ss`, `ø→o` …) → NFKD Unicode normalize → birleşen diakritikleri sil → lowercase → alfanümerik dışını sil → boşlukları toparla.

Kaynak: [author_matcher.py:101–118](backend/services/author_matcher.py#L101-L118)

#### Atıf biçimi parse'ı

Aynı `parse_authors()` fonksiyonu **5 farklı atıf biçimini** ele alır:

| Biçim | Örnek | İpucu |
|---|---|---|
| APA-virgül | `Smith, John A.` | virgülle ayrım |
| Vancouver | `Smith JA` | son token 1–3 büyük harf |
| Google Scholar | `JM Keller` | ilk token 2–3 baş harf |
| IEEE | `J. A. Smith` | öndeki noktalı baş harfler |
| Particle soyad | `Jan van der Berg` | sağa doğru `van/de/von` ekleme |

Kaynak: [author_matcher.py:148–229](backend/services/author_matcher.py#L148-L229)

#### Soyad eşleşmesi (`_last_names_match`)

```
Önce tam eşitlik
Yoksa  fuzz.ratio ≥ 90    (kısa soyad: min(len) ≤ 6)
       fuzz.ratio ≥ 85    (uzun soyad)
Çok kelimeli soyad fallback:
       fuzz.token_set_ratio ≥ 90  ve ortak ≥3-char token
```

Kaynak: [author_matcher.py:232–252](backend/services/author_matcher.py#L232-L252)

#### Initial doğrulama + birleşik soyad fallback

`Smith, J.` ≠ `Smith, K.` ayrımı yapılır. **İstisnalar:**

- Crossref'in birleşik soyadı kaynağa parça düşürdüğü vaka: `Sorkhabi, Majid Memarian` ↔ kaynak `M. Memarian` → soyad given-name içinde geçiyorsa eşleşme
- OCR tipoları: initial örtüşmesi varsa `fuzz.ratio(soyad) ≥ 78` ile tolerans

Kaynak: [author_matcher.py:269–317](backend/services/author_matcher.py#L269-L317)

#### Toplu eşleşme kuralı (`authors_match`)

```
küçük listenin boyutuna göre:
    ≤ 2 yazar  →  hepsi eşleşmeli  (%100)
    > 2 yazar  →  yarısı eşleşmeli (≥%50)

≥ 2 ortak soyad varken initial check devre dışı
(false-positive olasılığı pratik olarak sıfırdır)
```

- **Sıraya duyarlı değil** — her kaynak yazarı aday listede aranır
- "et al." için ayrı temizlik yok; yarı-eşleşme kuralı zaten ele alır
- Composite ağırlığı: **base'in %25'i**

Kaynak: [author_matcher.py:331–411](backend/services/author_matcher.py#L331-L411)

### 3.3 Dergi / source (`_venues_match`)

Dergiler kısaltma, parantezli alt başlık, vol/issue gürültüsü yüzünden basit string match yapamaz. **3 kademeli strateji:**

1. **Canonicalize** ([match_scorer.py:361–382](backend/services/match_scorer.py#L361-L382))
   - lowercase
   - parantez içini at: `Nature (London)` → `nature`
   - prefix at: `In: Proceedings of the…`, `Proc. of…` vb.
   - suffix at: vol/issue/pp/edition/yıl
   - alt başlık at (`:` sonrası)
   - ISO-4 kısaltmaları genişlet token bazında: `j → journal`, `proc → proceedings`, `intl → international`, `lett → letters`, `eng → engineering`, `med → medical` …

2. **token_sort_ratio fuzzy:**
   ```
   token_sort_ratio ≥ 0.75
   ```
   `token_set_ratio` ve `partial_ratio` bilerek **dahil değil** — ikisi de 1-2 token örtüştüğünde kesişim stringi birleştirilmiş stringin alt kümesi olduğundan oranı şişiriyor.

3. **Acronym kontrolü** — Bir taraf kısa/all-caps ise diğer tarafın kelime baş harfleriyle karşılaştırılır:
   ```
   "CVPR" ↔ "Computer Vision and Pattern Recognition"
   ```

- Composite ağırlığı: **+0.10 bonus** (base'e dahil değil)

Kaynak: [match_scorer.py:393–449](backend/services/match_scorer.py#L393-L449)

### 3.4 DOI

Normalizasyon (`normalize_doi`):

```
"https://doi.org/10.1234/Foo.Bar  "
   → strip, lowercase
   → "https?://(dx.)?doi.org/" prefix at
   → "doi:" prefix at
   → iç boşlukları sil (OCR satır kırılması)
   → sondaki .,;:)]}\"' karakterlerini at
   = "10.1234/foo.bar"
```

| | |
|---|---|
| Eşleşme | **tam eşitlik** (fuzzy yok) |
| Skor | 1.0 veya 0.0 |
| Composite | +0.10 bonus |

Kaynak: [doi_extractor.py:48–59](backend/utils/doi_extractor.py#L48-L59), [match_scorer.py:86–96](backend/services/match_scorer.py#L86-L96)

### 3.5 arXiv ID

3 yakalama pattern'i:

```
10.48550/arxiv.<id>            (DOI alias)
arxiv.org/abs|pdf/<id>          (URL)
arXiv: <id>                     (bare)

id formu:  \d{4}\s*\.\s*\d{4,5}(?:v\d+)?
```

İç boşluk toleranslı — wrap-bozuk OCR'larda `2403. 12345` → `2403.12345` olarak alınır.

Eşleşme: version suffix (`v1`, `v2` …) strip edildikten sonra **tam eşitlik**:

```
"2010.11929" (kaynak)  ↔  "2010.11929v2" (Crossref)  →  EŞLEŞİR
```

| | |
|---|---|
| Skor | 1.0 veya 0.0 |
| Composite | +0.10 bonus (DOI ile ortak `url_match` bayrağı) |

Kaynak: [doi_extractor.py:19–23](backend/utils/doi_extractor.py#L19-L23), [match_scorer.py:280–288](backend/services/match_scorer.py#L280-L288)

### 3.6 Yıl

```
diff = |source.year - candidate.year|

diff = 0  →  1.0     (tam eşit)
diff = 1  →  0.5     (preprint vs published)
diff > 1  →  0.0
```

| | |
|---|---|
| Tolerans | ±1 yıl |
| Composite | +0.10 bonus (diff ≤ 1 ise) |
| `!year` chip | diff > 1 ise tetiklenir |

Kaynak: [match_scorer.py:35–43](backend/services/match_scorer.py#L35-L43)

---

## 4. Composite skor formülü

```
base = title_score                                  if parse_confidence < 0.3 OR authors == []
     = title_score * 0.75 + author_score * 0.25    otherwise

bonus = 0
       + 0.10  if (src.year ve cand.year ve |diff| ≤ 1)
       + 0.10  if (src.source ve cand.journal ve _venues_match)
       + 0.10  if ((src.doi VEYA src.arxiv_id) ve url_match)

composite = clamp(base + bonus, 0.0, 1.0)
```

- Maksimum: **1.0** (3 bonus + tam title-author = 1.30 ama clamp eder)
- Minimum: **0.0**
- `MatchResult.score` 4 ondalığa yuvarlanır

Kaynak: [match_scorer.py:48–70](backend/services/match_scorer.py#L48-L70)

---

## 5. Status bantları — Yüksek / Orta / Düşük

`composite` skoru üç banda düşer. Bant **kart rengini ve i18n etiketini** belirler:

| Composite skor | Internal | Türkçe etiket | Renk |
|---|---|---|---|
| **≥ 0.75** | `high` | **Yüksek** | yeşil `#22c55e` |
| **0.50 – 0.75** | `medium` | **Orta** | turuncu `#f59e0b` |
| **< 0.50** | `low` | **Düşük** | kırmızı `#ef4444` |

> Status **yalnızca composite skora bakar** — ek "title-only gate" yoktur.

Kaynak: [match_scorer.py:119–120](backend/services/match_scorer.py#L119-L120), [match_scorer.py:197–202](backend/services/match_scorer.py#L197-L202)
i18n: [tr.json:312–318](src/renderer/src/lib/i18n/locales/tr.json#L312-L318)
Renkler: [VerificationPage.tsx:23–29](src/renderer/src/lib/components/verification/VerificationPage.tsx#L23-L29)

---

## 6. Karar sınıflandırması — Geçerli / Künye / Uydurma

`classify_decision()` per-sinyal predikatlara göre üçlü karar verir.

### Predikat kuralı (alanlardan biri eksikse)

```
iki taraf da var ve eşleşiyor   →  matches
iki taraf da yok                 →  matches  (karşılaştıracak şey yok)
sadece bir tarafta var           →  matches DEĞİL
iki taraf da var ve uyuşmuyor   →  matches DEĞİL
```

> Not: `iki taraf da yok → matches` durumu
dergi ve doi için yalnızca predikat/çip seviyesinde
> "uyuşmazlık yok" anlamına gelir. **Künye/Uydurma** ayrımında tek başına
> pozitif kanıt sayılmaz; bu durumda alan, karar için **matches** kabul edilmez.

Title için aynı kural değil — sürekli skoru kullanır:

```
title_matches  =  title_similarity ≥ 0.85
```

### Karar ağacı

```
hepsi eşleşiyor {author, title, source}
                                                  →  "valid"      →  Geçerli

title_matches
   VEYA  (author_matches VE biri {source, doi})
                                                  →  "citation"   →  Künye

Yıl karar ağacına dahil değildir; skor ve `!year` chip'i için
karşılaştırılmaya devam eder, ancak kararı değiştirmez.

aksi halde                                        →  "fabricated" →  Uydurma
```

Kaynak: [match_scorer.py:208–272](match_scorer.py#L208-L272)
i18n tooltip'leri: [tr.json:320–325](src/renderer/src/lib/i18n/locales/tr.json#L320-L325)

> **Önemli:** Status (Yüksek/Orta/Düşük) **kıyaslama gücüne** bakar; karar (Geçerli/Künye/Uydurma) **alanların tutarlılığına** bakar. İkisi bağımsız boyutlardır — düşük skorlu bir kart `Künye` olabilir, yüksek skorlu bir kart `Künye` olabilir.

---

## 7. Problem etiketleri (chip'ler)

5 chip status bandından bağımsızdır — her sinyalin gerçeğine göre yanar:

| Chip | Tetiklenme şartı |
|---|---|
| `!authors` | iki tarafta yazar var ve `authors_match` false; **veya** sadece bir tarafta var |
| `!year` | iki tarafta yıl var ve `\|diff\| > 1`; **veya** sadece bir tarafta var |
| `!journal` | iki tarafta venue var ve `_venues_match` false; **veya** sadece bir tarafta var |
| `!doi` | iki tarafta identifier var ve `url_match` false; **veya** sadece bir tarafta var (DOI veya arXiv ID — chip tek başına `doi` olarak görünür) |
| `!title` | best_match var ve `title_similarity < 0.85` |

Eksik kaynak alanı `!`-tetiklemez — karşılaştıracak bir şey yok demektir.

Kaynak: [match_scorer.py:150–194](backend/services/match_scorer.py#L150-L194)
i18n açıklamalar: [tr.json:333–339](src/renderer/src/lib/i18n/locales/tr.json#L333-L339)

---

## 8. Tier 1 — Paralel API verifier'ları

| Verifier | Dosya |
|---|---|
| Crossref | [backend/verifiers/crossref.py](backend/verifiers/crossref.py) |
| OpenAlex | [backend/verifiers/openalex.py](backend/verifiers/openalex.py) |
| OpenAIRE | [backend/verifiers/openaire.py](backend/verifiers/openaire.py) |
| arXiv | [backend/verifiers/arxiv.py](backend/verifiers/arxiv.py) |
| Semantic Scholar | [backend/verifiers/semantic_scholar.py](backend/verifiers/semantic_scholar.py) |
| Europe PMC | [backend/verifiers/europe_pmc.py](backend/verifiers/europe_pmc.py) |
| TRDizin | [backend/verifiers/trdizin.py](backend/verifiers/trdizin.py) |
| PubMed | [backend/verifiers/pubmed.py](backend/verifiers/pubmed.py) |
| Open Library | [backend/verifiers/open_library.py](backend/verifiers/open_library.py) |

**Davranış:**

- Her verifier API'den dönen sonuçların **ilk 5 tanesini** alır (`results[:5]`)
- Her aday doğrudan `score_match(source, candidate)`'e geçer — **DB-spesifik eşik veya algoritma override'ı yoktur**
- Tüm verifier'lar paralel çalışır (asyncio gather)
- **Strong-match early exit:** `score ≥ 1.0` **ve** `title_similarity ≥ 0.85` (TITLE_MATCH_THRESHOLD) → kalan DB'ler iptal. Title floor, bonus'ların (yıl + venue + DOI = 0.30) zayıf bir title'ı doyuma taşıyıp early exit'i tetiklemesini engeller.
- **Best-match seçimi:** tüm DB'lerden gelen tüm sonuçların en yükseği — `max(all_results, key=lambda m: m.score)`
- **Retry pass:** Timeout / rate-limit alan DB'ler 5 sn sonra bir kez daha denenir

Kaynak: [verification_orchestrator.py:248–415](backend/services/verification_orchestrator.py#L248-L415)

---

## 9. Tier 2 — Google Scholar (kullanıcı tetikli, webview)

Tier 1 yetersiz kalan kaynaklar için Electron webview üzerinden `scholar.google.com` taraması. Backend HTTP ile gitmez — gerçek tarayıcı oturumu kullanılır (CAPTCHA + cookie nedeniyle).

### Frontend akışı ([scholar-scanner.ts](src/renderer/src/lib/services/scholar-scanner.ts))

1. **Sorgu URL:**
   ```
   https://scholar.google.com/scholar?lookup=0&q=<source_text[:300]>
   ```
   `lookup=0` Scholar'ın "best result" tek-sonuç sayfasını engeller, tam liste döndürmesini zorlar.

2. **DOM polling:** Sayfa yüklenir yüklenmez 100 ms aralıklarla 2.5 sn'ye kadar yoklanır. Sonuç ya da CAPTCHA tespit edilir edilmez kısa devre yapar.

3. **Extract:** Tüm görünür sonuçlar (`.gs_r.gs_or.gs_scl`, `.gs_ri`, `[data-cid]` selector'ları) çekilir:
   - title (`[ALINTI]`, `[PDF]` gibi marker'lar temizlenir)
   - authors (`.gs_a`'dan)
   - year (regex `\b(19|20)\d{2}\b`)
   - doi (link `href`'lerinden ilk match)
   - snippet (`.gs_rs`)
   - cid (`data-cid`)
   - **`scraped_truncated`** bayrağı: title veya `.gs_a` içinde `…` (U+2026) varsa true

4. **Top-5'ten en benzer 1 tanesi seçilir** ([scholar-scanner.ts:194–208](src/renderer/src/lib/services/scholar-scanner.ts#L194-L208), [scholar-scanner.ts:430–448](src/renderer/src/lib/services/scholar-scanner.ts#L430-L448)):
   ```ts
   quickTitleScore(candTitle, refText):
     a = lowercase + alphanumeric tokens of candTitle
     b = lowercase + alphanumeric tokens of refText
     overlap = |a ∩ b|
     return overlap / max(|a|, |b|)
   ```
   > Bu **otoriter skor değil** — sadece "hangi adaya APA enrichment için ekstra istek atalım" kararı için ucuz token-overlap ranker'ı. Eskiden top 5'in **hepsi** APA için fetch ediliyordu; bu Scholar trafiğini ~6× artırıp CAPTCHA sıklığını uçurduğu için tek aday'a düşürüldü.

5. **APA enrichment** (sadece seçilen 1 aday için, sadece `scraped_truncated && cid` ise):
   ```
   GET /scholar?q=info:<cid>:scholar.google.com/&output=cite&hl=en
   → APA citation string
   ```

6. **Backend'e POST:** Tüm adaylar (APA-enriched top + diğerleri scraped haliyle) `/verify/score-scholar`'a gönderilir.

### Backend akışı ([api/verification.py:414–500](backend/api/verification.py#L414-L500))

1. **Source full text yeniden NER ile parse edilir** — Tier 1'de yapılan parse'la sembolik olmak için (yalnız title üzerinden değil, tam kaynak metni)

2. **Her aday için** ([api/verification.py:435–471](backend/api/verification.py#L435-L471)):
   - **APA varsa** → NER ile zenginleştirilir (`parse_confidence ≥ 0.3` koşuluyla); title/authors/year/doi/journal alanları APA'dan üzerine yazılır. Scholar başlıkları çok kısaltıldığı için bu adım, scoring doğruluğunu ciddi yükseltir.
   - **APA yoksa** → scraped alanlar olduğu gibi kullanılır
   - Authors `clean_scholar_authors()` ile temizlenir (Scholar'ın trailing `…`, virgülsüz initial birleştirmesi vb.)
   - Aday `score_match()`'e geçer — **diğer DB'lerle birebir aynı algoritma**

3. **Best Scholar seçimi:**
   ```python
   best_scholar = max(scholar_matches, key=lambda m: m.score)
   ```

4. **Best-match güncelleme:** `best_scholar.score > existing.best_match.score` ise yeni best_match olur, status + decision + chip'ler yeniden hesaplanır

5. **Boş aday durumu:** Aday gelmese bile `databases_searched`'e `"Google Scholar"` eklenir — UI'da "Google Scholar" linki gözüksün diye

### Rate limit + CAPTCHA (`ScholarRateLimiter`)

- Base **4 sn + 0–3 sn jitter** her sorgu arası
- **CAPTCHA sonrası ilk 5 istek:** 8–12 sn'ye yavaşlar
- **≥ 2 CAPTCHA:** 10–15 sn'ye çıkar
- CAPTCHA tespit edilince overlay webview'a kullanıcıya gösterilir; kullanıcı çözünce extract overlay'den okunur, scoring akışı kalan kuyrukla devam eder

Kaynak: [scholar-scanner.ts:149–184](src/renderer/src/lib/services/scholar-scanner.ts#L149-L184)

---

## 10. Hızlı kaynak tablosu

| Alan | Algoritma | Eşik | Composite etkisi |
|---|---|---|---|
| **Başlık** | `0.6·token_sort + 0.4·ratio` | 0.85 (chip/decision) | base'in %75'i |
| **Yazarlar** | normalize + soyad fuzzy + initial check | %85 (uzun) / %90 (kısa) | base'in %25'i |
| **Dergi** | canonicalize + `max(token_sort, token_set)` | 0.60 | +0.10 bonus |
| **DOI** | tam eşitlik (normalize sonrası) | — | +0.10 bonus |
| **arXiv** | tam eşitlik (version strip sonrası) | — | +0.10 bonus |
| **Yıl** | diff 0/1 → 1.0/0.5/0.0 | ±1 | +0.10 bonus |

| Status bandı | Composite skor | Türkçe |
|---|---|---|
| `high` | ≥ 0.75 | **Yüksek** |
| `medium` | 0.50 – 0.75 | **Orta** |
| `low` | < 0.50 | **Düşük** |

| Karar | Koşul | Türkçe |
|---|---|---|
| `valid` | author + title + source hepsi match | **Geçerli** |
| `citation` | title match VEYA (author + biri {source, doi}) | **Künye** |
| `fabricated` | aksi halde | **Uydurma** |

---

## 11. Kritik dosyalar

| Amaç | Dosya |
|---|---|
| Composite skor + status bantları + decision | [backend/services/match_scorer.py](backend/services/match_scorer.py) |
| Yazar parse + eşleşme | [backend/services/author_matcher.py](backend/services/author_matcher.py) |
| Regex extractor + parse confidence | [backend/services/source_extractor.py](backend/services/source_extractor.py) |
| NER extractor + parse confidence | [backend/services/ner_extractor.py](backend/services/ner_extractor.py) |
| Tier 1 orkestrasyon (paralel + best-match) | [backend/services/verification_orchestrator.py](backend/services/verification_orchestrator.py) |
| DOI/arXiv yakalama + normalize | [backend/utils/doi_extractor.py](backend/utils/doi_extractor.py) |
| Google Scholar webview scanner | [src/renderer/src/lib/services/scholar-scanner.ts](src/renderer/src/lib/services/scholar-scanner.ts) |
| Google Scholar backend scoring | [backend/api/verification.py](backend/api/verification.py) (`/verify/score-scholar`) |
| Türkçe etiket eşlemesi | [src/renderer/src/lib/i18n/locales/tr.json](src/renderer/src/lib/i18n/locales/tr.json) |
| Status renkleri (UI) | [src/renderer/src/lib/components/verification/VerificationPage.tsx](src/renderer/src/lib/components/verification/VerificationPage.tsx) |
