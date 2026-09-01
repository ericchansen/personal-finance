"""Source-agnostic categorization of live Wealthfolio cash activity.

``importers/simplefin/categorization.py`` owns the sealed plan/rehearse/promote
safety machinery. This package supplies the *identity* and *evidence* layer that
machinery needs in order to work for every import path -- Monarch, mapped
CSV/OFX/QFX extracts, and SimpleFIN -- instead of SimpleFIN alone.

Nothing here contacts a network service, and nothing here ever writes merchant
text, amounts, or account numbers into this public repository. Merchants are
represented only by the keyed HMAC digest the plan already uses.

The one deliberate exception is :mod:`importers.categorize.ollama`, which sends
a merchant description to a **loopback-only** Ollama server so a local model can
suggest a category for a genuinely novel payee. That request body is the only
place raw merchant text exists outside process memory: every artifact, report
and cache entry the agent produces is keyed by the same HMAC digest.
"""
