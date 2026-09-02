---
name: web_research
description: Research a topic by decomposing the question, searching the web, fetching pages, and synthesising findings with citations.
when_to_use: >
  Use this skill when the user asks to research a topic, investigate
  something, gather evidence from multiple sources, produce a report drawn
  from web content, or SUMMARISE/CONDENSE researched material into a brief
  (summarisation is folded into this skill's synthesis step; there is no
  separate summarise skill). Do not use for single factual lookups. Call
  the web_search tool directly for those.
tools_required:
  - web_search
  - web_fetch
  - file_write
---

# Web Research

A multi-step research procedure. Use it when the user wants an answer
that benefits from triangulation across sources, when the question has
multiple plausible angles, or when the user explicitly asks for a
researched report.

## When to use

Activate this skill for questions like:

- "What's the current state of X?"
- "How do Y and Z compare?"
- "Research W and write a summary."
- "Find evidence for / against claim K."

Do not activate this skill for:

- Single factual lookups ("What's the capital of Norway?"): call
  `web_search` directly.
- Local file operations: use `file_read` / `file_write` directly.
- Questions that don't benefit from external sources.

## Procedure

Follow these steps in order. Skip steps only when they don't apply (e.g.,
no document was requested in step 6).

### Step 1: Decompose the question

Break the user's question into 2-4 sub-queries that cover distinct
angles. Aim for queries that:

- Are independent (one sub-query shouldn't subsume another).
- Each cover a specific aspect (history, current state, alternatives,
  counter-arguments, implementation details, etc.).
- Use terms the model expects to find in source material, not the user's
  exact phrasing if the user phrased it colloquially.

Example: user asks "Is Norwegian husleieloven friendly to tenants?"

Sub-queries:
1. "Norwegian husleieloven tenant protections overview 2026"
2. "husleieloven landlord obligations notice period rent increase"
3. "husleietvistutvalget tenant disputes statistics outcomes"
4. "Norway rental law comparison Sweden Denmark"

### Step 2: Search per sub-query

For each sub-query, call `web_search` with:

- `query`: the sub-query string verbatim.
- `max_results`: 5 (default; lower for narrow queries, higher for broad).

Skim the result snippets. Note titles, sources, and publication dates.

### Step 3: Select pages to fetch

From the search results, pick the 2-3 most relevant URLs **per sub-query**
to fetch in full. Prefer:

- Official / primary sources (legislation, court rulings, statistics
  agencies) over secondary commentary.
- Recent pages (check publication or update date in the snippet).
- Diverse domains: three different newspaper articles on the same event
  add less than one newspaper + one government page + one academic paper.

Avoid:

- Aggregator sites that just repackage other content.
- Pages behind hard paywalls (you'll get an error; spend the budget on
  fetchable pages).
- Pages from domains you can't evaluate (random WordPress blogs without
  author credentials).

### Step 4: Fetch and extract

Call `web_fetch` on each selected URL. The tool returns extracted text;
note that the extraction is best-effort and may include some navigation
noise on poorly-formatted pages.

If a fetch returns an error (HTTP 4xx/5xx, timeout, SSL issue), do not
retry; log the failure mentally and move to the next URL. The fetch
tool already handles redirects and timeouts; a failure means the page
isn't accessible.

If a fetch returns `truncated=True`, you have the leading portion of a
long page. For most research that's sufficient; if you need more (e.g.,
the relevant content is in the body of a long article), you can re-fetch
with a larger `max_chars` parameter.

### Step 5: Synthesise

Now you have 6-12 pages of source text. Synthesise, do not summarise
each source one by one.

Structure your synthesis around the **sub-queries** from step 1, not the
sources. For each sub-query:

1. Identify the consensus position across your sources.
2. Note disagreements explicitly. If two sources conflict, surface the
   conflict; don't smooth it over.
3. Cite the sources by URL inline. Format: "[example.com/page](url)"
   suffices; the model doesn't need full bibliographic formatting unless
   the user asked for it.
4. Distinguish facts ("the statute reads X") from opinions ("the
   commentator argues Y") explicitly.

### Step 6: Produce output

If the user asked for a document (report, summary, briefing memo), call
`file_write` to save the synthesised content. Default filename
`research-<topic>.md` unless the user specified.

If the user asked for an inline answer, write the synthesis directly in
your response, same structure, same citations.

## Quality checks

Before completing the task, verify:

- [ ] Each sub-query is covered.
- [ ] Each major claim has at least one URL citation.
- [ ] Conflicting sources are surfaced, not hidden.
- [ ] Facts and opinions are distinguished.
- [ ] Publication dates noted for time-sensitive claims.
- [ ] No fabricated citations: every URL is one you actually fetched.

If any check fails, return to the relevant step and fix before producing
final output.

## Failure modes to watch for

**The "single source" trap.** You fetch one page, find a clear answer,
and stop. Don't. Even when the first source seems definitive, fetch at
least one more to cross-check. Sources that contradict each other are
often more informative than sources that agree.

**The "summarise sources" trap.** You write "Source A says X. Source B
says Y. Source C says Z." That's a list, not a synthesis. The user wants
an answer to the question, supported by sources, not a tour of the
sources.

**The "stale data" trap.** A 2018 page about Norwegian tenancy law might
still be cited everywhere, but the law could have been amended in 2022.
Check publication dates; flag old sources when the topic is fast-moving.

**The "AI hallucination citation" trap.** Every URL in your final output
must be one you actually fetched. Never invent URLs to make claims look
sourced. If you can't find a citation for a claim, mark it as
"unsourced" or remove the claim.

**The "paywalled abstract" trap.** Many academic papers and news
articles are paywalled; `web_fetch` returns the abstract or paywall
prompt. Don't cite an abstract as if it were the full paper; abstracts
omit caveats and methodology. Note explicitly when you only have the
abstract.

**The "echo chamber" trap.** Three sources from the same news syndicate
or commentary network count as one source, not three. Check the
publisher / parent organisation when sources agree suspiciously much.

## Cost considerations

Each `web_search` + `web_fetch` round costs roughly 1-3 seconds of
latency and a small amount of API quota. Budget:

- Small questions (1 sub-query): 1-2 fetches.
- Medium questions (2-3 sub-queries): 4-6 fetches.
- Large questions (4 sub-queries): 8-12 fetches; consider whether the
  user wants a full report or a quick answer first.

Do not silently expand scope. If the user asks a small question and you
realise mid-research that the answer requires extensive sourcing, pause
and ask whether they want the larger investigation.

## Examples

### Example 1: small question

User: "Is mould a landlord responsibility in Norwegian rental law?"

Sub-queries:
1. "husleieloven mould landlord responsibility"
2. "Norway tenant rights mould health"

Fetches: 3-4 pages from husleielova.no, the Tenants' Union, and a
recent court ruling summary.

Output: 2-3 paragraphs, 4-5 citations.

### Example 2: medium question

User: "Research the current state of LLM evaluation harnesses."

Sub-queries:
1. "LLM evaluation benchmark MMLU HumanEval current 2026"
2. "LLM harness lm-eval Vellum DeepEval comparison"
3. "LLM eval drift contamination criticisms"

Fetches: 7-9 pages from Hugging Face, papers via arXiv, recent blog
posts from labs.

Output: structured report, 1500-2000 words, ~15 citations.

### Example 3: large question

User: "Write a briefing memo on AI agent skill systems."

Pause first: confirm scope. A briefing memo is a 2000-3000 word
document with structured sections. Estimate fetch count (10-15) and
ask the user whether they want the full memo or a shorter answer first.

If confirmed: proceed with 3-4 sub-queries covering definition,
implementations (Anthropic SKILL.md, OpenAI assistants, others), use
cases, and limitations. Output to a file via `file_write`.

## Source evaluation rubric

When ranking sources before fetching, apply these criteria in order:

**Primacy.** Does the source produce the information, or repeat it? An
official statute is primary; a news article describing the statute is
secondary; an aggregator post summarising the news article is tertiary.
Prefer primary when available; use secondary to interpret; use tertiary
only to find the others.

**Authority.** Is the author or organisation a credible voice on this
topic? A government statistics agency on demographics, a peer-reviewed
journal on biology, a recognised practitioner on engineering practice:
each carries weight in its domain. A random blogger on a topic outside
their stated expertise does not.

**Recency.** When was this published or last updated? For fast-moving
fields (AI, medicine, law in active jurisdictions, technology stacks)
treat anything older than 18-24 months with caution. Verify against a
recent source before citing as current.

**Disclosure.** Does the source declare its conflicts of interest or
funding? A company white paper on its own product is useful for technical
detail but biased on comparison. An academic paper that lists funders
is more trustworthy than one that doesn't. NGO advocacy pages have a
stated position, useful when you want their position, less useful for
balanced overview.

**Reproducibility.** Can you reach the source's underlying claims? A
paper that links to its dataset and methodology beats a paper that
asserts without evidence. A news article that links to the press release
beats one that paraphrases anonymously.

## Handling ambiguity in the user's question

Sometimes the user's question is ambiguous in a way that changes what
you should research. Don't paper over the ambiguity. Surface it.

For example: "Is X expensive?" Expensive compared to what? Per unit,
per outcome, per year of operation? Pause and ask, unless the context
makes the comparison obvious.

Other examples:

- "What's the best way to do Y?" Best by what metric? Speed, cost,
  reliability, simplicity? Each implies a different research path.
- "Is Z safe?" Safe for whom, under what conditions, by what standard?
  Regulatory bodies have specific definitions; the colloquial meaning
  may differ.
- "Compare A and B." On what dimensions? Performance, price, ecosystem,
  vendor lock-in? Ask, then research the requested dimensions.

If asking would slow the user down, make a reasonable assumption and
state it explicitly: "I'll research X assuming you mean Y; let me know
if you meant Z and I'll re-scope."

## Multilingual sources

If the question is about a non-English topic (Norwegian tenancy law, German
case law, French regulation), the most authoritative sources will often
be in the local language. The model can read most major European
languages. Don't avoid local sources just because they're not English:
they're frequently the primary sources. Cite them in their original
language and provide a brief translation of the relevant quote in your
synthesis.

## What "good output" looks like

A high-quality research synthesis has these properties:

1. **Answers the question directly in the first paragraph.** Don't make
   the reader wait through five paragraphs of background to find the
   answer. State the conclusion first, then support it.

2. **Each major claim is cited.** Inline links to URLs, formatted
   consistently. The reader can click through to verify any claim.

3. **Disagreements among sources are surfaced.** If two authoritative
   sources contradict, the synthesis says so explicitly: "Source A
   says X; source B says Y; the dispute appears to be about Z."

4. **Confidence is calibrated.** Strong claims ("the statute states
   that...") use definitive language. Weaker claims ("most observers
   think...") use hedging language. Don't sound certain when you're
   not, don't sound uncertain when you are.

5. **No padding.** A synthesis that could be 800 words shouldn't be
   1500. Cut sentences that don't add information.

6. **No fabrication.** Every fact and every URL is one you actually
   sourced. If you can't source something, mark it explicitly or
   remove it.
