# Contributing

Thank you for helping improve accessible scholarly publishing. The project currently provides a demonstration front end; contributions should preserve its clear distinction between simulated and real functionality.

## Ways to help

- Improve translations, especially workflow and simulation notices, and request native-speaker review for scientific terminology.
- Report reproducible accessibility, layout, internationalisation, and functional defects using fictional data only.
- Propose focused code, documentation, or meaningful tests.

## Local workflow

1. Read the README, run the prototype on a local HTTP server, and make a focused change.
2. For English or Simplified Chinese base text, edit the corresponding `L('English','简体中文')` call in `dist/app.js`, then run `python3 scripts/sync-locales.py`. These two reference catalogues are generated; do not edit them by hand. For another language, update its JSON file in `dist/locales/` directly, using the exact English source as the key. Machine-generated translations need human review before production use.
3. Non-base locale files currently accept plain text only. Do not include `<`, `>`, `&`, or straight double quotes in values: the prototype rejects them to protect its HTML templates. Do not remove or soften notices that the workflow and AI check are simulated. Missing keys fall back to English.
4. Run `python3 scripts/sync-locales.py --check`, `node --check dist/app.js`, and `node --test tests/*.test.cjs`. Review affected workflows in a browser, including Arabic right-to-left layout where relevant.
5. Open an issue or pull request with a clear description, test results, and any relevant screenshots that contain only fictional data. By submitting a contribution, confirm that you have the rights to contribute it under the project license.

Never submit real manuscripts, unpublished research, personal details, keys, private hosting metadata, or third-party code or content without clear permission. Sample records must remain fictional. Journal names, marks, and content are not automatically licensed as software.
