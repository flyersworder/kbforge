# Examples

Worked examples of extending kbforge. Each is a self-contained package that installs
alongside kbforge and is discovered through an entry point — no changes to kbforge
core.

- [**github-issues-connector**](github-issues-connector/) — a complete credentialed
  connector (~135 lines) that syncs a repository's GitHub issues into OKF concepts,
  with token auth, pagination, and a real incremental cursor. The template for
  writing your own connector.
