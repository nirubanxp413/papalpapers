# Step 0: Encyclical Index Crawl

Catalog every papal document listed on [papalencyclicals.net](https://www.papalencyclicals.net) and produce a CSV index for downstream markdown processing.

## Goal

Produce `data/encyclicals.csv` with one row per document:

| Column | Description |
|--------|-------------|
| `pope` | Pope name as shown on the site |
| `title` | Document title (Latin or English) |
| `subtitle` | English descriptive subtitle, if present |
| `published_date` | ISO date (`YYYY-MM-DD`) when known |
| `doc_type` | Section type: `encyclical`, `apostolic_exhortation`, `other`, etc. |
| `format` | `html` or `pdf` |
| `link` | Primary English document URL |
| `source` | `papalencyclicals` or `vatican` |
| `pope_url` | Index page URL for this pope |
| `notes` | Dedup flags, parse warnings |

## Why not Firecrawl?

The site is WordPress and exposes a public REST API. No JavaScript rendering or paid crawl service is required.

## Data sources

### 1. WordPress posts API (historical corpus)

```
GET https://www.papalencyclicals.net/wp-json/wp/v2/posts?categories={id}&per_page=100&page={n}
```

~508 posts total. Each post provides `title`, `date`, `link`, and `categories[]`. Most links are `.htm` files hosted on papalencyclicals.net with full text inline.

Pope → category mapping via:

```
GET https://www.papalencyclicals.net/wp-json/wp/v2/categories?per_page=100
```

### 2. Pope pages API (recent popes)

```
GET https://www.papalencyclicals.net/wp-json/wp/v2/pages?slug={slug}
```

Pages like `/leo14`, `/franc`, `/jp02` contain HTML lists under `<h3>` section headers. Recent encyclicals link to vatican.va HTML documents.

### 3. Pope registry

```
GET https://www.papalencyclicals.net/wp-json/wp/v2/pages?slug=popelist
```

~45 popes with name and index URL. URLs are either `/category/{slug}` or `/{slug}` page paths.

## Architecture

```
popelist (WP API)
    ├── category URL → paginate posts by category ID
    └── page URL     → parse HTML sections in page content
              ↓
         normalize rows
              ↓
         dedupe by link
              ↓
      data/encyclicals.csv
```

## Implementation

Script: `scripts/crawl_index.py`

- Caches raw API responses in `data/cache/`
- Rate-limits to ~1 req/sec
- Skips "Additional Links" sections (Vatican index pages, not individual documents)
- Dedupes overlapping entries (e.g. Paul VI appears in both category posts and pope page)

## Validation targets

| Pope | Expected docs (approx) |
|------|------------------------|
| Leo XIII | 88 |
| Pius XII | 60 |
| Francis | 4 encyclicals |
| Leo XIV | 1 encyclical |

## Output files

```
data/
├── popes.json          # pope registry from popelist
├── encyclicals.csv     # main index
├── summary.json        # per-pope counts and date ranges
└── cache/              # raw API responses (gitignored)
```

## Next step

Step 1 uses this CSV to download each document and convert to markdown.
