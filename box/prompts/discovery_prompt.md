Read AGENTS.md in this directory first and follow it, including the writing
style and data rules.

A product-discovery sweep proposed new sandbox products for this site. The
candidates, each with its match evidence and citation URLs, are in
{discovery_path}.

Decide for each candidate whether it belongs in the census. Accept it only
if the evidence shows all three:

1. It is a hosted product that provides isolated cloud compute for running
   AI agents' code. Not a library, not an isolation technology, not a blog
   post or a list of other products, not a general compute platform that
   merely could run agent code.
2. Sandboxes are created and controlled programmatically through an API,
   SDK, or CLI.
3. It has public documentation or a pricing page.

Judge from the evidence in the file. If the evidence cannot tell, reject:
a wrong row damages the site more than a missing one.

For each accepted candidate:

1. Append its {{"name", "slug", "website"}} entry to providers.json, keeping
   the existing entries unchanged.
2. Create data/providers/<slug>.json following the schema in
   box/providers.py. Write a short factual summary from the evidence, set
   last_verified to today, and put the evidence's citation URLs in sources.
   Fill only spec fields the evidence supports; leave every other field
   null.

Then write state/turn_summary.md. Its first line is a commit-style summary
under 72 characters. Below it, one short paragraph naming what you accepted
and what you rejected, with the reason for each rejection.

If you accept nothing, edit nothing else and say so in
state/turn_summary.md.
