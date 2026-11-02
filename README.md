# Intelligent Publishing Studio

Intelligent Publishing Studio is an open-source prototype for a multilingual, multi-journal scholarly publishing platform. SCIENTARA's journal group is the initial pilot scenario. This repository contains a static front-end prototype and a separate, local-development backend for testing submission, manuscript-specific editor and production assignments, editorial decisions, revisions, multiple review rounds, and default-off local article and issue publication simulations with synthetic data.

> **Development only.** The static front end is simulated and is not connected to the backend. The backend stores test accounts, metadata, reviews and PDF files locally, but lacks controls required for real manuscripts or deployment. An optional OpenAI metadata-only adapter is disabled by default and requires explicit author and operator actions; it has only been tested with a mock provider. Neither component sends email or connects to a DOI registration service or a production database. Do not enter real manuscripts, personal information, production credentials or unpublished research. Neither component is suitable for production use.

The backend is described in [backend/README.md](backend/README.md) ([简体中文快速开始](backend/README.zh-CN.md)). It supports two interface languages (English and Simplified Chinese); the static prototype has its separate 12-language selector.

## Explore the prototype

Requirements: Python 3 and a recent web browser. There are no JavaScript package dependencies and no build step.

```sh
python3 -m http.server 8080 -d dist
```

Open `http://localhost:8080`. Serve the files over HTTP so that browsers can load the language packs. Demonstration records are stored only in the current browser session; the language preference is stored in browser local storage. The source repository is <https://github.com/ziiyuu-lgtm/intelligent-publishing-studio>.

## Static prototype

- Four illustrative SCIENTARA journal pages: AIRES, INDUSTRICS, NOVAXIS, and VERSIONICS.
- A simulated four-step submission flow and editorial queue with fictional examples.
- A journal appearance preview, an illustrative AI check, and a VERSIONICS dual-track review example.
- A twelve-language selector with partial translation notices and English fallback.

These interfaces are demonstrations. They do not establish actual journal policies, indexing, article processing services, or third-party AI partnerships. Static prototype actions do not change backend records.

## Language support

The selector lists English, Simplified Chinese, Traditional Chinese, Spanish, French, Portuguese, Arabic, Russian, German, Japanese, Korean, and Hindi. English and Simplified Chinese are the base interface catalogues. The other ten packs cover common interface actions and key simulation notices, but are incomplete and have not yet been reviewed by native speakers. Untranslated strings fall back to English, and examples, journal names, and some descriptions remain in their original language. Arabic has initial right-to-left layout support and needs further native-speaker and device testing.

English source strings and Simplified Chinese equivalents currently live in `L('English','简体中文')` calls in `dist/app.js`. The English and Simplified Chinese reference catalogues are generated with `python3 scripts/sync-locales.py`. Other `dist/locales/*.json` files are maintained directly and use the English source string as their key. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Checks

Node.js 18 or later is needed for the test runner; Python 3 is used to validate the base translation catalogues.

```sh
node --check dist/app.js
python3 scripts/sync-locales.py --check
node --test tests/*.test.cjs
```

## Planned work

Possible future work includes secure accounts and access control, manuscript storage, peer-review workflows, publication metadata, third-party service integrations, an AI-provider interface with appropriate privacy controls, and deployment documentation. These are goals for future development, not features of this prototype.

## License, contributions, and security

The source code is available under the [Apache License, Version 2.0](LICENSE). Subject to that license's terms, commercial use and closed-source derivatives are permitted. The code license does not automatically grant rights to SCIENTARA trademarks, journal names, logos, manuscripts, personal information, or other third-party materials.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and translation instructions, and [SECURITY.md](SECURITY.md) for handling possible vulnerabilities. Do not post confidential information in public issues.
