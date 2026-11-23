## Summary

<!-- Describe what this PR changes and why. -->

## Checklist

- [ ] Added or updated unit/integration tests (`uv run pytest -v`)
- [ ] Passed lint and type checks (`uv run ruff check src tests` & `uv run mypy src`)
- [ ] Updated documentation if behavior, CLI flags, or environment variables changed
- [ ] Verified no secrets, tokens, or real account numbers are included
- [ ] Verified default risk guardrails (`SCHWAB_DRY_RUN=true`, `confirm_live=True`) remain intact
