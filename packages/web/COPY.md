# Copy rules

Words are a product surface. A feature people cannot understand is a feature
they do not use, and copy that reads machine-written costs us trust before
anyone reaches the product. Treat a string the way you treat a component.

Run `pnpm check:copy` before you push. It gates the message catalogue.

## The two hard rules

**1. No em dashes or en dashes. Ever, in anything a user sees.**

They are read as an AI tell. Use a period, a comma, a colon, or parentheses.
Almost every dash is hiding a sentence that wanted to be two sentences.

```
no    Typed memory — versioned, append-only, and yours.
yes   Typed memory, versioned and append-only.
yes   Memory is versioned and append-only. Nothing is silently overwritten.
```

Keep plain hyphens only where they are load-bearing: CLI flags, file names,
code identifiers, kebab-case values, and compound words that need them.

**2. No AI-slop vocabulary.**

Banned outright: seamless, empower, delve, supercharge, effortless, unlock,
unleash, elevate, revolutionary, cutting-edge, game-changing, best-in-class,
world-class, state-of-the-art, streamline, "transform your", "boost your",
"look no further", "dive into", "we've got you covered".

Also banned as constructions: "It's not just X, it's Y" and "whether you're X
or Y".

The problem is not that these words are overused. It is that each one occupies
the exact spot where a specific fact belonged.

```
no    Seamlessly sync your conversations.
yes   Conversations sync in under a second.

no    Unlock the power of typed memory.
yes   Your persona remembers what you told it three weeks ago, and can show
      you where that memory came from.
```

## How to write the string instead

**Lead with what the reader gets, not what the system does.**

```
no    The scheduler supports recurrence rule configuration.
yes   Set it once. It runs every Monday until you say stop.
```

**Be specific enough to be falsifiable.** If a competitor could paste your
sentence onto their page unchanged, it is not saying anything. "Fast" is a
claim about nothing. "Picks a cheaper model when the turn is easy" is a claim
about this product.

**Errors say what happened and what to do next.** No blame, no dead ends.

```
no    An error occurred.
yes   That upload was too large. Files need to be under 20 MB.
```

**Empty states invite the first action.** They are the most-read copy in any
app and usually the least considered.

```
no    No conversations found.
yes   No conversations yet. Start one and your persona begins remembering.
```

**Buttons are verbs.** "Save changes", not "Submit". "Start a call", not "OK".

**Write it out loud.** If you would not say it to someone at a desk next to
you, do not ship it. Contractions are good. Corporate register is not.

## Marketing copy has one extra rule

Every claim must be true and checkable in the product. An overstatement is
worse than a plain sentence, because the first person who tries it and finds
otherwise tells everyone else. When you are unsure whether something is true,
ask before writing it, and never upgrade a hedged claim into a confident one
to make a section feel stronger.

## Where the rules live

- `pnpm check:copy` gates the web catalogue (`src/i18n/messages/en.json`) for
  dashes, banned vocabulary, and the banned constructions.
- `bash scripts/check-no-em-dashes.sh` (repo root) gates the same dash rule
  across public markdown.
- Every user-visible string belongs in the catalogue, not inline in a
  component, so it can be reviewed and gated in one place.

Adding an exemption is allowed but it is per key and it has to state why. See
`ALLOWLIST` in `scripts/check-copy.mjs`. If a banned word genuinely belongs in
one string, that is a fact about that string, not a licence to reuse it.
