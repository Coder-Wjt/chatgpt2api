# 0001: Account-scoped model discovery

Status: pending-verification

- Owner: project maintainer
- Created: 2026-09-17
- User scope: fetch upstream models by account capability, cache, fall back, build Docker image; deployment is manual.

## Outcome and scope

Replace the fixed default chat list with the union reported by eligible Upstream
Accounts. Keep explicit overrides, image aliases, and the existing Model Catalog
contract. Do not change text execution account selection, storage ownership, or
frontend interactions. Runtime details are owned by [the backend map](../maps/backend-map.md).

## Ownership and failure behavior

ModelCatalogService owns discovery, cache, refresh admission and cleanup.
AccountService owns eligibility and credential renewal; OpenAIBackendAPI owns
upstream transport. No new persistent store, dependencies, or ADR are required.
Failures retain bounded stale data or the built-in fallback without exposing
credentials. Application lifecycle starts/stops discovery. Existing client
transport consumes the same catalog projection.

## Acceptance

- Account results merge without duplicates; overrides and image aliases remain.
- Cache hits avoid network work; failures and empty results back off and fall back.
- Account removal, disabling and credential rotation invalidate contributions.
- Slow discovery has bounded concurrency and cold-request waiting.
- API and console return the same catalog; shutdown cleans up refresh work.
- Type checks, regression tests, and Docker build succeed.

Automated verification uses isolated accounts and mocked upstream responses.
A live account probe also verified proxy-based upstream discovery, and an isolated
container verified same-version runtime refresh with data preservation. Deployment
and user acceptance remain for the operator. The changed catalog/lifecycle modules
pass type checks; the upstream transport module retains pre-existing diagnostics.
Reverting the code restores the prior list; there is no data migration.
