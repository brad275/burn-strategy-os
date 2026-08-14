# BURN Strategy OS

Stage 2 foundation for BURN's versioned, evidence-led strategy workflow.

This milestone contains no model calls, UI, Edge integration, or live-vault writes. It provides:

- validated, versioned record contracts;
- tenant-safe filesystem paths;
- atomic canonical writes;
- immutable stage attempts and feedback events;
- explicit project and cycle lifecycles;
- quality findings and a visible ledger;
- memory proposals that require a human promotion decision.

Run the complete test suite:

```bash
python3 -m unittest discover -s tests -t . -v
```

The canonical storage root is supplied by the application. Tests use temporary directories only.
